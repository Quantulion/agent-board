// Карточка задачи в headless Chrome: настоящий сервер с раннером и fake-агентом.
// Ответ на вопрос агента, черновик под потоком событий, экранирование, дифф, ресайз, Esc, внешний блокер;
// скриншоты каждой вкладки в светлой и тёмной теме и снимки макета docs/design/mockups/card.html того же размера.
// Запуск из корня репозитория: node ui-tests/card.mjs
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
const home = fs.mkdtempSync(path.join(os.tmpdir(), 'ab-card-'));
const gitRepo = path.join(home, 'payments-api');
const env = { ...process.env, AGENT_BOARD_HOME: home, PYTHONPATH: path.join(repo, 'board'), AGENT_BOARD_PROJECT: 'pay', AGENT_BOARD_ACTOR: 'user' };
const cli = (...args) => execFileSync(python, ['-m', 'agent_board', ...args], { env, cwd: repo, encoding: 'utf-8', timeout: 20000 });
const git = (...args) => execFileSync('git', args, { cwd: gitRepo, encoding: 'utf-8', timeout: 20000 });
const VIEW = { width: 1440, height: 900 };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const script = steps => 'FAKE-SCRIPT: ' + JSON.stringify(steps);

function withTimeout(promise, ms, what) {
  return Promise.race([promise, new Promise((_, rej) => setTimeout(() => rej(new Error('таймаут: ' + what)), ms))]);
}

async function startServer() {
  const port = 19000 + Math.floor(Math.random() * 1000);
  const proc = spawn(python, ['-m', 'agent_board', 'serve', '--port', String(port)], { env, cwd: repo });
  proc.stderr.on('data', d => process.stderr.write(d));
  const url = await withTimeout(new Promise((resolve, reject) => {
    let buf = '';
    proc.stdout.on('data', d => { buf += d; const m = buf.match(/http:\/\/\S+/); if (m) resolve(m[0]); });
    proc.on('exit', code => reject(new Error('сервер завершился: ' + code)));
  }), 15000, 'старт сервера');
  return { proc, url };
}

fs.mkdirSync(out, { recursive: true });
fs.mkdirSync(gitRepo);
git('init', '-q', '-b', 'master');
git('config', 'user.email', 'ui@test');
git('config', 'user.name', 'ui');
fs.writeFileSync(path.join(gitRepo, 'README.md'), '# payments\n');
git('add', '.');
git('commit', '-q', '-m', 'init');

cli('init', 'pay', '--title', 'Перенос платежей на PostgreSQL');
const cfgPath = path.join(home, 'projects', 'pay', 'project.json');
const cfg = JSON.parse(fs.readFileSync(cfgPath, 'utf-8'));
Object.assign(cfg, { harness: { type: 'fake' }, repos: [{ name: 'payments-api', path: gitRepo, base: 'master' }] });
fs.writeFileSync(cfgPath, JSON.stringify(cfg, null, 2));

const DESC105 = 'При каждой записи платежа в MSSQL дублировать запись в PG, не влияя на основной поток. Ошибки PG не должны ронять оплату.';
const QUESTION = 'Что делать, если запись в PG не удалась после всех ретраев?';
const steps105 = [
  { write: { path: 'src/shadow_writer.py', text: 'def write(payment):\n    retries = 3\n    return pg.insert(payment)\n' } },
  { write: { path: 'README.md', text: '# payments\n\nТеневая запись в PG включается настройкой `ShadowWriteEnabled`.\n' } },
  { cli: ['update', 'PAY-105', '--summary', 'Теневая запись встроена в репозиторий записи платежей; ретраи с джиттером.',
    '--todo-json', JSON.stringify([{ text: 'Развилка записи MSSQL → PG', done: true }, { text: 'Ретраи с джиттером', done: true }, { text: 'Функтесты на теневую запись', done: false }])] },
  { cli: ['say', 'PAY-105', '--text', 'Сделал развилку записи и ретраи. Файл `src/shadow_writer.py`, подробности: https://wiki.example.local/pay-pg.'] },
  { cli: ['ask', 'PAY-105', '--text', QUESTION,
    '--option', 'Логировать и пропускать', '--option', 'Складывать в outbox и дописывать фоново', '--option', 'Возвращать ошибку и откатывать оплату',
    '--recommended', 'Складывать в outbox и дописывать фоново',
    '--context', 'Теневая запись — копия каждой оплаты в новую базу, пока старая остаётся основной.\n\nПосле трёх ретраев запись в PG может всё равно не пройти.'] },
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
const state = () => api('GET', 'state');
const taskOf = async key => (await state()).tasks.find(t => t.key === key);
async function until(fn, ms, what) {
  const deadline = Date.now() + ms;
  for (;;) {
    const v = await fn();
    if (v) return v;
    if (Date.now() > deadline) throw new Error('не дождались: ' + what);
    await sleep(200);
  }
}

let browser;
const results = {};
const shots = {};
try {
  const tasks = [
    { key: 'PAY-103', title: 'Слой репозиториев с переключателем MSSQL / PG', repo: 'payments-api', estimate: 3,
      description: 'Выделить интерфейсы репозиториев.', done_when: 'Все тесты проходят на обеих реализациях.' },
    { key: 'PAY-105', title: 'Теневая запись платежей в PostgreSQL', repo: 'payments-api', estimate: 2, deps: ['PAY-103'],
      description: DESC105 + '\n\n' + script(steps105),
      done_when: 'Данные в PG совпадают с MSSQL по сверке; ошибки PG видны в метриках и не меняют ответ API.' },
    { key: 'PAY-110', title: 'Нагрузочный прогон PG-ветки на копии прода', repo: 'payments-api', kind: 'research', estimate: 1,
      description: 'Прогнать нагрузку уровня пикового дня на копии прода и снять профиль.', done_when: 'Отчёт: задержки p50/p99 и узкие места.' },
    { key: 'PAY-111', title: 'Переключить чтение платежей на PG', repo: 'payments-api', estimate: 2, deps: ['PAY-105', 'PAY-103'],
      description: 'Чтение платежей переключается на PG по проценту трафика.' },
  ];
  for (const t of tasks) cli('add-task', '--json', JSON.stringify(t));
  await post('tasks/PAY-103/transition', { to: 'done', actor: 'user' });
  cli('stage', 'tracker');
  cli('stage', 'work');
  // fake-агент PAY-105 пишет файл, резюме и задаёт вопрос
  await until(async () => (await taskOf('PAY-105')).open_questions === 1, 30000, 'вопрос PAY-105');
  await until(async () => (await taskOf('PAY-110')).agent_state === 'idle' && (await taskOf('PAY-110')).session_id, 30000, 'ход PAY-110');
  // сценарий fake-агента уже прочитан: в описании остаётся только текст задачи
  await post('tasks/PAY-105/update', { actor: 'user', fields: { description: DESC105 } });
  await post('threads/PAY-105/messages', { author: 'project_agent', text: 'PAY-106 сверяет записи по времени изменения — бери время из исходной записи MSSQL.' });
  await until(async () => (await taskOf('PAY-105')).agent_state === 'idle', 30000, 'ход PAY-105 после сообщения');
  await post('threads/PAY-110/messages', { author: 'agent', text: 'Прогон на 2× пиковой нагрузке: p50 18 мс, p99 140 мс. Узкое место — индекс по `created_at`.' });

  browser = await puppeteer.launch({ headless: true, args: ['--no-sandbox'] });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  await page.setViewport(VIEW);
  await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);
  await page.goto(url + '#/p/pay', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.querySelectorAll('.card[data-key]').length === 4, { timeout: 10000 });

  // открытие из «Ждут вас»: карточка открывается на «Обзоре» с вопросом, адрес получает ?task=
  await page.waitForSelector('.attn .it.q', { timeout: 10000 });
  await page.click('.attn .it.q');
  await page.waitForSelector('.drawer[data-key="PAY-105"] .question', { timeout: 10000 });
  results.hashOnOpen = await page.evaluate(() => location.hash);
  results.openTab = await page.$eval('.tabs [aria-selected="true"]', el => el.dataset.tab);
  results.tabs = await page.$$eval('.tabs button', els => els.map(e => e.textContent));
  await page.waitForFunction(() => document.querySelector('.drawer .todolist'), { timeout: 10000 });
  await page.$eval('.question details', el => { el.open = true; });

  async function shotTabs(prefix, ids) {
    for (const theme of ['light', 'dark']) {
      await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: theme }]);
      for (const id of ids) {
        await page.click(`.tabs [data-tab="${id}"]`);
        if (id === 'changes') await page.waitForSelector('.drawer .files, .drawer .dbody .sec p.muted:not(:empty)', { timeout: 10000 });
        if (id === 'thread') await page.waitForSelector('.drawer .thread .msg', { timeout: 10000 });
        await sleep(400);
        const file = path.join(out, `${prefix}-${id}-${theme}.png`);
        await page.screenshot({ path: file });
        shots[`${prefix}_${id}_${theme}`] = file;
      }
    }
    await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);
    await page.click('.tabs [data-tab="overview"]');
  }
  await shotTabs('card', ['overview', 'thread', 'review', 'changes']);

  // дифф показывает файл, который записал fake-агент
  await page.click('.tabs [data-tab="changes"]');
  await page.waitForSelector('.drawer .file[data-path="src/shadow_writer.py"]', { timeout: 10000 });
  results.diffFile = await page.$eval('.drawer .file[data-path="src/shadow_writer.py"]', el => el.textContent);
  await page.click('.drawer .file[data-path="src/shadow_writer.py"]');
  await page.waitForSelector('.drawer .file[data-path="src/shadow_writer.py"] + .diff', { timeout: 5000 });
  results.diffAdded = await page.$$eval('.drawer .file[data-path="src/shadow_writer.py"] + .diff .l.a', els => els.map(e => e.textContent));
  results.diffFiles = await page.$$eval('.drawer .file', els => els.map(e => e.querySelector('.fs').textContent + ' ' + e.dataset.path));
  const bothFile = path.join(out, 'card-changes-two-files-light.png');
  await page.screenshot({ path: bothFile });
  shots.card_changes_two_files_light = bothFile;
  await page.click('.tabs [data-tab="overview"]');

  // ответ на вопрос: fake-агент продолжает ход по комментарию и переводит задачу на ревью
  const opts = await page.$$eval('.question .opt', els => els.map(e => [e.textContent, e.querySelector('input').checked]));
  results.recommendedChecked = opts.find(o => o[1])?.[0];
  await page.type('.question textarea', script([{ cli: ['status', 'PAY-105', 'in_review'] }]));
  await page.click('.question .btn.primary');
  results.answerDisabled = await page.$eval('.question .btn.primary', el => el.disabled);
  await page.waitForFunction(() => !document.querySelector('.drawer .question'), { timeout: 15000 });
  await page.waitForFunction(() => document.querySelector('.drawer .dh .spill')?.textContent === 'На ревью', { timeout: 30000 });
  results.statusAfterAnswer = await page.$eval('.drawer .dh .spill', el => el.textContent);

  // черновик и фокус переживают 50 событий и переключение вкладок; панель — тот же узел, анимация не перезапускается
  await page.evaluate(() => {
    window.__drawer = document.querySelector('.drawer');
    window.__anims = 0;
    document.addEventListener('animationstart', e => { if (e.target.closest && e.target.closest('.drawer, .dscrim')) window.__anims++; }, true);
  });
  await page.click('.tabs [data-tab="thread"]');
  await page.click('.drawer .composer textarea');
  await page.type('.drawer .composer textarea', 'Черновик второго сообщения');
  for (let i = 0; i < 50; i++) await post('threads/PAY-105/messages', { author: 'agent', text: 'Событие ' + (i + 1) });
  await page.waitForFunction(() => [...document.querySelectorAll('.drawer .thread .msg')].some(m => m.textContent.includes('Событие 50')), { timeout: 15000 });
  results.focusAfterEvents = await page.evaluate(() => document.activeElement === document.querySelector('.drawer .composer textarea'));
  await page.click('.tabs [data-tab="changes"]');
  await page.click('.tabs [data-tab="overview"]');
  await page.click('.tabs [data-tab="thread"]');
  results.draftAfterTabs = await page.$eval('.drawer .composer textarea', el => el.value);
  results.sameDrawer = await page.evaluate(() => window.__drawer === document.querySelector('.drawer') && window.__drawer.isConnected);
  results.animationRestarts = await page.evaluate(() => window.__anims);
  await page.focus('.drawer .composer textarea');
  await page.type('.drawer .composer textarea', '!');
  results.focusAfterTabs = await page.evaluate(() => document.activeElement === document.querySelector('.drawer .composer textarea'));
  results.draftTyped = await page.$eval('.drawer .composer textarea', el => el.value);
  await page.waitForFunction(() => !document.querySelector('.tabs [data-tab="thread"] .tb:not([hidden])'), { timeout: 10000 });
  results.unreadAfterRead = (await taskOf('PAY-105')).unread;

  // ревью с историей раундов (формат зафиксирует план 4)
  await post('tasks/PAY-105/update', { actor: 'agent', fields: { review: { round: 2, state: 'open', url: 'http://localhost:4317/s/pay-105-r2',
    history: [{ round: 1, state: 'changes', text: '6 комментариев → все исправлены' }, { round: 2, state: 'open', text: '3 комментария ждут вас' }] } } });
  await page.click('.tabs [data-tab="review"]');
  await page.waitForSelector('.drawer .rounds .round.cur', { timeout: 10000 });
  results.reviewTab = await page.$eval('.tabs [data-tab="review"]', el => el.textContent);
  for (const theme of ['light', 'dark']) {
    await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: theme }]);
    await sleep(300);
    const file = path.join(out, `card-review-rounds-${theme}.png`);
    await page.screenshot({ path: file });
    shots[`card_review_rounds_${theme}`] = file;
  }
  await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);
  await page.click('.tabs [data-tab="thread"]');

  // текст с разметкой выводится как текст
  await post('threads/PAY-105/messages', { author: 'agent', text: '<img src=x onerror="window.__xss=1"> и <b>жирный</b>' });
  await page.waitForFunction(() => [...document.querySelectorAll('.drawer .thread .msg')].some(m => m.textContent.includes('<img')), { timeout: 10000 });
  results.xssEscaped = await page.evaluate(() => !document.querySelector('.drawer .thread img, .drawer .thread b:not(.mh b)') && !window.__xss);
  results.stickToBottom = await page.$eval('.drawer .dbody', el => el.scrollHeight - el.scrollTop - el.clientHeight < 40);

  // отправка сообщения из треда: черновик очищается, сообщение появляется
  await page.focus('.drawer .composer textarea');
  results.draftKeptAfterReview = await page.$eval('.drawer .composer textarea', el => el.value);
  await page.keyboard.down('Meta');
  await page.keyboard.press('Enter');
  await page.keyboard.up('Meta');
  await page.waitForFunction(() => [...document.querySelectorAll('.drawer .thread .msg.user')].some(m => m.textContent.includes('Черновик второго сообщения!')), { timeout: 10000 });
  results.composerCleared = await page.$eval('.drawer .composer textarea', el => el.value === '');

  // ресайз за левый край сохраняется после перезагрузки
  const hb = await (await page.$('.drawer .dhandle')).boundingBox();
  await page.mouse.move(hb.x + hb.width / 2, hb.y + 300);
  await page.mouse.down();
  await page.mouse.move(600, hb.y + 300, { steps: 8 });
  await page.mouse.up();
  results.widthAfterDrag = await page.$eval('.drawer', el => Math.round(el.getBoundingClientRect().width));
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.drawer[data-key="PAY-105"]', { timeout: 10000 });
  results.widthAfterReload = await page.$eval('.drawer', el => Math.round(el.getBoundingClientRect().width));
  results.draftAfterReload = await page.evaluate(() => sessionStorage.getItem('ab-draft:pay:PAY-105'));
  await page.evaluate(() => localStorage.removeItem('ab-dw'));

  // Esc закрывает карточку и убирает её из адреса
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => !document.querySelector('.drawer'), { timeout: 5000 });
  results.hashAfterEsc = await page.evaluate(() => location.hash);

  // несуществующая задача — тост и никакой панели
  await page.evaluate(() => { location.hash = '#/p/pay?task=PAY-404'; });
  await page.waitForFunction(() => [...document.querySelectorAll('.toast')].some(t => t.textContent.includes('PAY-404')), { timeout: 5000 });
  results.missingNoDrawer = await page.evaluate(() => !document.querySelector('.drawer'));
  await page.waitForFunction(() => !location.hash.includes('task='), { timeout: 5000 });

  // исследование: только «Обзор» и «Тред»
  await page.evaluate(() => { location.hash = '#/p/pay?task=PAY-110'; });
  await page.waitForSelector('.drawer[data-key="PAY-110"]', { timeout: 5000 });
  results.researchTabs = await page.$$eval('.tabs button', els => els.map(e => e.dataset.tab));
  await shotTabs('card-research', ['overview', 'thread']);

  // меню статуса: «Внешний блокер…» открывает диалог и пишет причину
  await page.evaluate(() => { location.hash = '#/p/pay?task=PAY-111'; });
  await page.waitForSelector('.drawer[data-key="PAY-111"]', { timeout: 5000 });
  await page.click('.drawer .stbtn');
  results.cardMenu = await page.$$eval('.drawer .stmenu .menu button', els => els.map(e => e.textContent));
  await page.evaluate(() => [...document.querySelectorAll('.drawer .stmenu .menu button')].find(b => b.textContent === 'Внешний блокер…').click());
  await page.waitForSelector('.modal input', { timeout: 5000 });
  await page.type('.modal input', 'Нет доступа к PG-реплике — заявка у DBA');
  const blockerFile = path.join(out, 'card-blocker-dialog-light.png');
  await page.screenshot({ path: blockerFile });
  shots.card_blocker_dialog_light = blockerFile;
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => document.querySelector('.drawer .banner.blocked')?.textContent.includes('DBA'), { timeout: 10000 });
  results.blockerBanner = await page.$eval('.drawer .banner.blocked', el => el.textContent);
  results.blockerSaved = (await taskOf('PAY-111')).external_blocker;
  results.attnBlocker = await page.$$eval('.attn .it', els => els.map(e => e.textContent).filter(t => t.includes('внешний блокер')));
  const blockedFile = path.join(out, 'card-blocked-light.png');
  await page.screenshot({ path: blockedFile });
  shots.card_blocked_light = blockedFile;
  // Esc в диалоге закрывает только диалог
  await page.click('.drawer .xblock .btn');
  await page.waitForSelector('.modal', { timeout: 5000 });
  await page.keyboard.press('Escape');
  await sleep(200);
  results.escClosesOnlyModal = await page.evaluate(() => !document.querySelector('.modal') && Boolean(document.querySelector('.drawer')));

  results.pageErrors = errors;

  // макет того же размера: каждая вкладка в светлой и тёмной теме
  const mock = await browser.newPage();
  await mock.setViewport(VIEW);
  await mock.goto(pathToFileURL(path.join(repo, 'docs', 'design', 'mockups', 'card.html')).href, { waitUntil: 'load', timeout: 20000 }).catch(() => {});
  for (const theme of ['light', 'dark']) {
    await mock.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: theme }]);
    for (const id of ['overview', 'thread', 'review', 'changes']) {
      await mock.click(`[data-tab="${id}"]`).catch(() => {});
      await sleep(300);
      const file = path.join(out, `mockup-card-${id}-${theme}.png`);
      await mock.screenshot({ path: file });
      shots[`mockup_${id}_${theme}`] = file;
    }
  }
} finally {
  if (browser) await browser.close();
  proc.kill('SIGTERM');
  await withTimeout(new Promise(r => proc.once('exit', r)), 15000, 'остановка сервера').catch(() => proc.kill('SIGKILL'));
  fs.rmSync(home, { recursive: true, force: true });
}
console.log(JSON.stringify({ results, shots }, null, 2));
const ok = results.hashOnOpen === '#/p/pay?task=PAY-105' && results.openTab === 'overview'
  && results.recommendedChecked && results.recommendedChecked.startsWith('Складывать в outbox')
  && results.answerDisabled && results.statusAfterAnswer === 'На ревью'
  && results.diffFile.includes('src/shadow_writer.py') && results.reviewTab === 'Ревьюр2' && results.draftKeptAfterReview === 'Черновик второго сообщения!' && results.diffAdded.some(l => l.includes('retries = 3'))
  && results.diffFiles.join() === 'M README.md,A src/shadow_writer.py'
  && results.focusAfterEvents && results.draftAfterTabs === 'Черновик второго сообщения'
  && results.sameDrawer && results.animationRestarts === 0 && results.focusAfterTabs
  && results.draftTyped === 'Черновик второго сообщения!' && results.unreadAfterRead === 0
  && results.xssEscaped && results.stickToBottom && results.composerCleared
  && Math.abs(results.widthAfterDrag - (VIEW.width - 600)) <= 2 && results.widthAfterReload === results.widthAfterDrag
  && results.hashAfterEsc === '#/p/pay' && results.missingNoDrawer
  && results.researchTabs.join() === 'overview,thread'
  && results.cardMenu.includes('Внешний блокер…') && !results.cardMenu.includes('Заблокирована')
  && results.blockerSaved === 'Нет доступа к PG-реплике — заявка у DBA' && results.blockerBanner.includes('DBA')
  && results.attnBlocker.length === 1 && results.escClosesOnlyModal
  && results.pageErrors.length === 0;
process.exit(ok ? 0 : 1);
