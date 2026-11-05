// Согласование разбивки в headless Chrome на настоящем сервере с fake-агентами.
// 8 задач, 2 с нарушениями правил: сводка нарушений, граф по шагам с «параллельно: N», раскрытие задачи кликом по узлу;
// комментарий к задаче будит агента проекта и виден в треде задачи; утверждение переводит проект в work,
// страница становится только для чтения, на доске стартуют агенты; «Блокирует» в окне новой задачи.
// Пункт «Ждут вас» о нарушении ведёт на разбивку к задаче; пропущенный трекер на шкале стадий; текст агентов не исполняется.
// Запуск из корня репозитория: node ui-tests/decomposition.mjs
import { spawn, execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import puppeteer from 'puppeteer';

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '..');
const out = path.join(here, 'out');
const python = path.join(repo, '.venv', 'bin', 'python');
const home = fs.mkdtempSync(path.join(os.tmpdir(), 'ab-decomp-'));
const env = { ...process.env, AGENT_BOARD_HOME: home, PYTHONPATH: path.join(repo, 'board'), AGENT_BOARD_PROJECT: 'pay', AGENT_BOARD_ACTOR: 'user' };
const cli = (...args) => execFileSync(python, ['-m', 'agent_board', ...args], { env, cwd: repo, encoding: 'utf-8', timeout: 20000 });
const VIEW = { width: 1440, height: 900 };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const XSS = '<img src=x onerror=window.__pwned=1>';

function withTimeout(promise, ms, what) {
  return Promise.race([promise, new Promise((_, rej) => setTimeout(() => rej(new Error('таймаут: ' + what)), ms))]);
}

async function startServer() {
  const port = 21000 + Math.floor(Math.random() * 1000);
  const proc = spawn(python, ['-m', 'agent_board', 'serve', '--port', String(port)], { env, cwd: repo });
  proc.stderr.on('data', d => process.stderr.write(d));
  const url = await withTimeout(new Promise((resolve, reject) => {
    let buf = '';
    proc.stdout.on('data', d => { buf += d; const m = buf.match(/http:\/\/\S+/); if (m) resolve(m[0]); });
    proc.on('exit', code => reject(new Error('сервер завершился: ' + code)));
  }), 15000, 'старт сервера');
  return { proc, url };
}

function gitRepo(name) {
  const dir = path.join(home, name);
  const git = (...args) => execFileSync('git', args, { cwd: dir, encoding: 'utf-8', timeout: 20000 });
  fs.mkdirSync(dir);
  git('init', '-q', '-b', 'master');
  git('config', 'user.email', 'ui@test');
  git('config', 'user.name', 'ui');
  fs.writeFileSync(path.join(dir, 'README.md'), '# ' + name + '\n');
  git('add', '.');
  git('commit', '-q', '-m', 'init');
  return dir;
}

fs.mkdirSync(out, { recursive: true });
cli('init', 'pay', '--title', 'Перенос платежей на PostgreSQL');
const cfgPath = path.join(home, 'projects', 'pay', 'project.json');
const cfg = JSON.parse(fs.readFileSync(cfgPath, 'utf-8'));
Object.assign(cfg, {
  harness: { type: 'fake' },
  repos: [{ name: 'payments-api', path: gitRepo('payments-api'), base: 'master' },
    { name: 'payments-worker', path: gitRepo('payments-worker'), base: 'master' }],
  agent_rules: 'Одна задача = один деплой.\n\nОценка задачи — не больше 3 дней.\n\nУ каждой задачи есть проверяемый критерий готовности.',
});
fs.writeFileSync(cfgPath, JSON.stringify(cfg, null, 2));

const tasks = [
  ['T-1', 'Снять фактическую схему MSSQL и описать расхождения', [], 1, 'research', 'payments-api',
    'Схема таблиц платежей с прод-реплики: типы, вычисляемые колонки, триггеры, индексы.', 'Отчёт с расхождениями и решением по каждому.'],
  ['T-2', 'Схема PostgreSQL и миграции для таблиц платежей', ['T-1'], 2, 'code', 'payments-api',
    'Таблицы payments, operations, refunds; вычисляемые колонки — как generated stored.', 'Миграции применяются на чистую БД и копию стенда.'],
  ['T-3', 'Слой репозиториев с переключателем MSSQL / PG', ['T-2'], 3, 'code', 'payments-api',
    'Интерфейсы репозиториев и две реализации с выбором по переключателю.', 'Все тесты проходят на обеих реализациях.'],
  ['T-4', 'Мигратор исторических платежей с курсором и докачкой', ['T-2'], 5, 'code', 'payments-worker',
    'Переливка батчами с курсором, остановка и продолжение, докачка новых записей, сверка количества.', 'Полная переливка копии прода без дублей.'],
  ['T-5', 'Теневая запись платежей в PostgreSQL', ['T-3'], 2, 'code', 'payments-api',
    'Каждая запись платежа дублируется в PG, сбои PG не влияют на оплату.', 'Данные в PG совпадают с MSSQL по сверке T-6.'],
  ['T-6', 'Сверка MSSQL ↔ PG и алерт на расхождения', ['T-4'], 2, 'code', 'payments-worker',
    'Ночная джоба сверки в payments-worker и новый алерт на метрику расхождений в payments-api.', 'Отчёт за сутки прода меньше 10 минут.'],
  ['T-7', 'Индексы и партиционирование таблицы операций', ['T-2'], 2, 'code', 'payments-api',
    'Индексы под основные запросы и помесячные партиции. ' + XSS, ''],
  ['T-8', 'Переключить чтение платежей на PG', ['T-5', 'T-6'], 2, 'code', 'payments-api',
    'Чтение переключается по проценту трафика с быстрым откатом.', 'Неделя на 100% трафика без расхождений.'],
];

const { proc, url } = await startServer();
const origin = new URL(url).origin;
const token = new URL(url).searchParams.get('token');
async function api(method, p, body) {
  const resp = await fetch(origin + '/api/p/pay/' + p, {
    method, headers: { 'Content-Type': 'application/json', 'X-Board-Token': token }, body: body ? JSON.stringify(body) : undefined,
  });
  const data = await resp.json();
  if (!resp.ok) throw new Error(p + ': ' + data.error);
  return data;
}
const post = (p, body) => api('POST', p, body);
const events = async () => (await api('GET', 'events?since=0')).events;
async function waitFor(fn, what, ms = 15000) {
  const deadline = Date.now() + ms;
  for (;;) {
    const v = await fn();
    if (v) return v;
    if (Date.now() > deadline) throw new Error('не дождались: ' + what);
    await sleep(150);
  }
}

let browser;
let page;
const results = {};
const shots = {};
try {
  for (const [key, title, deps, estimate, kind, repoName, description, doneWhen] of tasks) {
    cli('add-task', '--json', JSON.stringify({ key, title, deps, estimate, kind, repo: repoName, description, done_when: doneWhen }));
  }
  await post('tasks/T-4/update', { actor: 'user', fields: { rule_violations: [
    { rule: 'Оценка задачи — не больше 3 дней', text: 'Предлагаю разбить: «переливка с курсором» (3 д) и «докачка новых записей» (2 д).' }] } });
  await post('tasks/T-6/update', { actor: 'user', fields: { rule_violations: [
    { rule: 'Одна задача = один деплой ' + XSS, text: 'Задача затрагивает payments-worker и payments-api: вынести алерт в отдельную задачу. ' + XSS }] } });
  await post('threads/_project/messages', { author: 'project_agent', text: 'Собрал разбивку из плана: 8 задач. T-4 и T-6 нарушают правила — предлагаю разбить.' });
  await post('threads/T-4/messages', { author: 'project_agent', text: 'Оценка 5 дней — больше лимита, предлагаю разбить задачу.' });

  browser = await puppeteer.launch({ headless: true, args: ['--no-sandbox'] });
  page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !m.text().includes('400')) errors.push(m.text()); });
  await page.setViewport(VIEW);
  await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);
  await page.goto(url + '#/p/pay/decomposition', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.querySelectorAll('.decomp .task').length === 8, { timeout: 10000 });
  await page.waitForFunction(() => document.querySelectorAll('.decomp g.node').length === 8, { timeout: 10000 });
  await page.waitForFunction(() => document.querySelector('.gthread .msg.pa'), { timeout: 10000 });
  results.violatedTasks = await page.$$eval('.decomp .task.viol', els => els.map(e => e.dataset.key));
  results.summaryRows = await page.$$eval('.decomp .vsum .rule', els => els.map(e => e.textContent));
  results.stats = await page.$$eval('.decomp .stat', els => els.map(e => e.textContent));
  results.lanes = await page.$$eval('.decomp svg .lane-sub', els => els.map(e => e.textContent));
  results.stepperBefore = await page.$$eval('.stepper .st', els => els.map(e => e.className + ':' + e.textContent));
  // шапка в одну строку: кнопка темы не переносится
  results.headerOneRow = await page.evaluate(() => document.querySelector('.decomp .row1').getBoundingClientRect().height < 50);
  results.critNodes = await page.$$eval('.decomp g.node.crit', els => els.map(e => e.dataset.key));
  results.violNodes = await page.$$eval('.decomp g.node.viol', els => els.map(e => e.dataset.key));
  results.rulesText = await page.$eval('.decomp .rtext', el => el.textContent);
  results.noDod = await page.$eval('.decomp .task[data-key="T-7"]', el => el.classList.contains('warnb') && el.querySelector('.f.missing') !== null);
  results.stepper = await page.$eval('.stepper .st.cur', el => el.textContent);
  let file = path.join(out, 'decomposition-light.png');
  await page.screenshot({ path: file });
  shots.light = file;

  // клик по узлу графа раскрывает задачу в списке и подсвечивает её
  await page.click('.decomp g.node[data-key="T-5"]');
  await sleep(700);
  results.nodeExpands = await page.$eval('.decomp .task[data-key="T-5"]', el => el.classList.contains('open') && el.classList.contains('hl')
    && !el.querySelector('.comments').hidden);
  results.hashAfterNode = await page.evaluate(() => window.location.hash);
  // клик по ключу в сводке нарушений ведёт к задаче
  await page.evaluate(() => document.querySelector('.decomp .vsum .rule .lnk').click());
  await sleep(700);
  results.summaryExpands = await page.$eval('.decomp .task[data-key="T-4"]', el => el.classList.contains('open'));
  await page.waitForFunction(() => document.querySelector('.decomp .task[data-key="T-4"] .comments .msg.pa'), { timeout: 10000 });

  // комментарий к задаче: виден в её треде, будит агента проекта с ключом задачи
  const area = '.decomp .task[data-key="T-4"] .comments textarea';
  await page.type(area, 'Согласен разбить. Докачку можно делать параллельно с теневой записью?');
  await page.click('.decomp .task[data-key="T-4"] .comments .composer .btn.primary');
  await page.waitForFunction(() => [...document.querySelectorAll('.decomp .task[data-key="T-4"] .comments .msg.user')]
    .some(m => m.textContent.includes('Докачку можно')), { timeout: 10000 });
  results.commentVisible = true;
  const wake = await waitFor(async () => (await events()).find(e => e.type === 'agent_wake' && e.data.key === '_project' && e.data.task === 'T-4'), 'agent_wake агента проекта');
  results.wake = wake.data;
  results.taskWokenBeforeWork = (await events()).some(e => e.type === 'agent_wake' && e.data.key === 'T-4' && e.data.reason === 'message' && e.data.by === 'user');
  await waitFor(async () => (await events()).some(e => e.type === 'turn_finished' && e.data.key === '_project'), 'ход агента проекта');
  results.projectAgentChanged = (await events()).filter(e => e.type === 'project_agent_changed').map(e => e.data.state);
  results.taskTurnsBeforeWork = (await events()).filter(e => e.type === 'turn_started' && e.data.key !== '_project').length;
  results.commentButton = await page.$eval('.decomp .task[data-key="T-4"] .tf .btn', el => el.textContent);
  await page.evaluate(() => document.querySelector('.decomp .task[data-key="T-4"]').scrollIntoView({ block: 'start' }));
  await sleep(200);
  file = path.join(out, 'decomposition-comments-light.png');
  await page.screenshot({ path: file });
  shots.comments = file;

  await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'dark' }]);
  await page.evaluate(() => document.querySelector('.dscroll').scrollTo(0, 0));
  await sleep(300);
  file = path.join(out, 'decomposition-dark.png');
  await page.screenshot({ path: file });
  shots.dark = file;
  await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);

  // «Ждут вас» на доске: нарушение ведёт на разбивку, задача раскрыта и в центре экрана
  await page.goto(url + '#/p/pay', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => [...document.querySelectorAll('.attn .it')].some(b => b.textContent.includes('T-6')), { timeout: 10000 });
  await page.evaluate(() => [...document.querySelectorAll('.attn .it')].find(b => b.textContent.includes('T-6')).click());
  await page.waitForFunction(() => document.querySelector('.decomp .task[data-key="T-6"].open'), { timeout: 10000 });
  await sleep(700);
  results.attnNav = await page.evaluate(() => {
    const r = document.querySelector('.decomp .task[data-key="T-6"]').getBoundingClientRect();
    return { hash: window.location.hash, card: Boolean(document.querySelector('.drawer')), inView: r.top >= 0 && r.top < window.innerHeight };
  });
  file = path.join(out, 'decomposition-from-attn-light.png');
  await page.screenshot({ path: file });
  shots.fromAttn = file;

  // утверждение: подтверждение с нарушениями → стадия work, страница только для чтения
  await page.click('.decomp .footbar .btn.primary');
  await page.waitForSelector('.modal', { timeout: 5000 });
  results.modalText = await page.$eval('.modal', el => el.textContent);
  await sleep(400);
  file = path.join(out, 'decomposition-confirm-light.png');
  await page.screenshot({ path: file });
  shots.confirm = file;
  await page.evaluate(() => [...document.querySelectorAll('.modal footer .btn')].find(b => b.textContent.includes('как есть')).click());
  await page.waitForFunction(() => !document.querySelector('.decomp .done-banner').hidden, { timeout: 10000 });
  results.stage = (await api('GET', 'state')).config.stage;
  results.readOnly = await page.evaluate(() => ({
    approveDisabled: document.querySelector('.decomp .footbar .btn.primary').disabled,
    generalDisabled: document.querySelector('.decomp .gthread textarea').disabled,
    taskDisabled: document.querySelector('.decomp .task[data-key="T-6"] .comments textarea').disabled,
    banner: document.querySelector('.decomp .done-banner').textContent,
  }));
  results.stepperAfter = await page.$$eval('.stepper .st', els => els.map(e => e.className + ':' + e.textContent));
  await sleep(600);
  file = path.join(out, 'decomposition-approved-light.png');
  await page.screenshot({ path: file });
  shots.approved = file;

  // доска: агенты задач стартуют, ссылка «Разбивка» остаётся в шапке
  await waitFor(async () => (await events()).some(e => e.type === 'turn_started' && e.data.key === 'T-1'), 'старт агента T-1');
  await page.goto(url + '#/p/pay', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.querySelectorAll('.card').length === 8, { timeout: 10000 });
  results.boardDecompLink = await page.$eval('.artifacts a[href$="/decomposition"]', el => el.textContent);
  results.taskTurns = [...new Set((await events()).filter(e => e.type === 'turn_started' && e.data.key !== '_project').map(e => e.data.key))];
  await sleep(400);
  file = path.join(out, 'decomposition-board-after-light.png');
  await page.screenshot({ path: file });
  shots.boardAfter = file;

  // «Блокирует»: новая задача становится зависимостью выбранной
  await page.click('.toolbar .btn.primary');
  await page.waitForSelector('.modal.newtask', { timeout: 5000 });
  await page.type('#nt-title', 'Нагрузочный прогон PG-ветки');
  await page.evaluate(() => document.querySelector('#nt-blocks input[value="T-8"]').click());
  await page.type('.field:has(#nt-blocks) .pick-search', 'T-8');
  await page.evaluate(() => document.querySelector('#nt-blocks').scrollIntoView({ block: 'center' }));
  await sleep(400);
  file = path.join(out, 'decomposition-newtask-blocks-light.png');
  await page.screenshot({ path: file });
  shots.newTaskBlocks = file;
  const newKey = await page.$eval('#nt-key', el => el.value);
  // пока окно открыто, зависимости T-8 меняются: новая задача добавляется к свежим, а не к старым
  await post('tasks/T-8/deps', { deps: ['T-5', 'T-6', 'T-7'], actor: 'user' });
  await page.click('.modal footer .btn.primary');
  await waitFor(async () => (await api('GET', 'state')).tasks.find(t => t.key === 'T-8').deps.includes(newKey), 'T-8 ждёт новую задачу');
  results.blocks = { newKey, deps: (await api('GET', 'state')).tasks.find(t => t.key === 'T-8').deps };

  // ошибка одной связи: T-1 не может ждать задачу, которая сама ждёт T-4 (а через неё и T-1), — тост об ошибке, T-7 обновлена
  // после создания открывается карточка новой задачи — закрываем её
  await page.waitForFunction(key => window.location.hash.endsWith('?task=' + key), { timeout: 10000 }, newKey);
  await page.evaluate(() => { window.location.hash = '#/p/pay'; });
  await page.waitForFunction(() => !document.querySelector('.drawer'), { timeout: 5000 });
  await page.click('.toolbar .btn.primary');
  await page.waitForSelector('.modal.newtask', { timeout: 5000 });
  await page.type('#nt-title', 'Проверка после переключения');
  const secondKey = await page.$eval('#nt-key', el => el.value);
  await page.evaluate(() => {
    document.querySelector('#nt-deps input[value="T-4"]').click();
    document.querySelector('#nt-blocks input[value="T-1"]').click();
    document.querySelector('#nt-blocks input[value="T-7"]').click();
  });
  await page.click('.modal footer .btn.primary');
  await page.waitForFunction(() => [...document.querySelectorAll('.toast.err')].some(t => t.textContent.includes('в зависимости T-1')), { timeout: 10000 });
  results.blockError = await page.$$eval('.toast.err', els => els.map(e => e.textContent));
  await sleep(300);
  file = path.join(out, 'decomposition-blocks-error-light.png');
  await page.screenshot({ path: file });
  shots.blocksError = file;
  const fresh = (await api('GET', 'state')).tasks;
  results.blocksPartial = { secondKey, t1: fresh.find(t => t.key === 'T-1').deps, t7: fresh.find(t => t.key === 'T-7').deps };

  // текст агентов и пользователя выводится только текстом
  await page.goto(url + '#/p/pay/decomposition', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.querySelectorAll('.decomp .task').length === 10, { timeout: 10000 });
  await sleep(300);
  results.xss = await page.evaluate(() => ({
    pwned: window.__pwned === 1,
    imgs: document.querySelectorAll('.decomp img').length,
    shown: document.querySelector('.decomp .vsum').textContent.includes('<img src=x'),
    description: document.querySelector('.decomp .task[data-key="T-7"] .what').textContent.includes('<img src=x'),
  }));
  results.pageErrors = errors;

  // макет того же размера
  const mock = await browser.newPage();
  await mock.setViewport(VIEW);
  await mock.goto(pathToFileURL(path.join(repo, 'docs', 'design', 'mockups', 'decomposition.html')).href, { waitUntil: 'load', timeout: 20000 }).catch(() => {});
  for (const theme of ['light', 'dark']) {
    await mock.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: theme }]);
    await sleep(300);
    file = path.join(out, `mockup-decomposition-${theme}.png`);
    await mock.screenshot({ path: file });
    shots[`mockup_${theme}`] = file;
  }
} catch (e) {
  if (page) await page.screenshot({ path: path.join(out, 'decomposition-failure.png') }).catch(() => {});
  console.log(JSON.stringify(results, null, 2));
  throw e;
} finally {
  if (browser) await browser.close();
  proc.kill('SIGTERM');
  await withTimeout(new Promise(r => proc.once('exit', r)), 15000, 'остановка сервера').catch(() => proc.kill('SIGKILL'));
  fs.rmSync(home, { recursive: true, force: true });
}
console.log(JSON.stringify({ results, shots }, null, 2));
const ok = results.violatedTasks.join() === 'T-4,T-6' && results.summaryRows.length === 2
  && results.summaryRows.some(r => r.includes('Оценка задачи') && r.includes('T-4'))
  && results.lanes.some(l => l.startsWith('параллельно: ')) && results.critNodes.length > 0 && results.violNodes.join() === 'T-4,T-6'
  && results.rulesText.includes('один деплой') && results.noDod && results.stepper.includes('Разбивка')
  && results.nodeExpands && results.hashAfterNode === '#/p/pay/decomposition' && results.summaryExpands
  && results.commentVisible && results.wake.reason === 'message' && results.wake.by === 'user' && !results.taskWokenBeforeWork
  && results.projectAgentChanged.includes('running') && results.projectAgentChanged.includes('idle')
  && results.taskTurnsBeforeWork === 0
  && results.modalText.includes('Не исправлено 2') && results.stage === 'work'
  && results.readOnly.approveDisabled && results.readOnly.generalDisabled && results.readOnly.taskDisabled
  && results.readOnly.banner.includes('только для чтения')
  && results.boardDecompLink === 'Разбивка' && results.taskTurns.includes('T-1')
  && results.blocks.deps.join() === 'T-5,T-6,T-7,' + results.blocks.newKey
  && results.blockError.length === 1 && !results.blocksPartial.t1.includes(results.blocksPartial.secondKey)
  && results.blocksPartial.t7.includes(results.blocksPartial.secondKey)
  && results.attnNav.hash === '#/p/pay/decomposition?focus=T-6' && !results.attnNav.card && results.attnNav.inView
  && results.stepperBefore[1].startsWith('st skipped') && results.stepperBefore[1].includes('пропущен')
  && results.stepperAfter.map(s => s.split(':')[0]).join() === 'st done,st skipped,st cur,st todo'
  && results.headerOneRow && !results.xss.pwned && results.xss.imgs === 0 && results.xss.shown && results.xss.description
  && results.pageErrors.length === 0;
process.exit(ok ? 0 : 1);
