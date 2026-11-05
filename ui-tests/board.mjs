// Доска в headless Chrome: настоящий сервер на временном каталоге, проект из 14 задач во всех статусах.
// Скриншоты светлой и тёмной темы, меню статуса, тосты реакции и ошибки, точечная перерисовка карточек;
// для сравнения — снимки макета docs/design/mockups/board.html того же размера.
// Запуск из корня репозитория: node ui-tests/board.mjs
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
const home = fs.mkdtempSync(path.join(os.tmpdir(), 'ab-ui-'));
const env = { ...process.env, AGENT_BOARD_HOME: home, PYTHONPATH: path.join(repo, 'board'), AGENT_BOARD_PROJECT: 'pay', AGENT_BOARD_ACTOR: 'user' };
const cli = (...args) => execFileSync(python, ['-m', 'agent_board', ...args], { env, cwd: repo, encoding: 'utf-8', timeout: 20000 });
const VIEW = { width: 1440, height: 900 };
const LOOP = 'Агенты переписываются без участия пользователя — остановлено; ответьте в треде, чтобы продолжить';

function withTimeout(promise, ms, what) {
  return Promise.race([promise, new Promise((_, rej) => setTimeout(() => rej(new Error('таймаут: ' + what)), ms))]);
}

async function startServer() {
  const port = 18000 + Math.floor(Math.random() * 1000);
  // без раннера: состояния агентов задаёт сам тест, чтобы снимок был воспроизводимым
  const proc = spawn(python, ['-m', 'agent_board', 'serve', '--port', String(port), '--no-runner'], { env, cwd: repo });
  const url = await withTimeout(new Promise((resolve, reject) => {
    let buf = '';
    proc.stdout.on('data', d => { buf += d; const m = buf.match(/http:\/\/\S+/); if (m) resolve(m[0]); });
    proc.on('exit', code => reject(new Error('сервер завершился: ' + code)));
  }), 15000, 'старт сервера');
  return { proc, url };
}

fs.mkdirSync(out, { recursive: true });
cli('init', 'pay', '--title', 'Перенос платежей на PostgreSQL');
const cfgPath = path.join(home, 'projects', 'pay', 'project.json');
const cfg = JSON.parse(fs.readFileSync(cfgPath, 'utf-8'));
Object.assign(cfg, {
  harness: { type: 'fake' },
  max_agents: 4,
  repos: [
    { name: 'payments-api', path: '/tmp/payments-api', base: 'master' },
    { name: 'payments-worker', path: '/tmp/payments-worker', base: 'master' },
    { name: 'ledger-reports', path: '/tmp/ledger-reports', base: 'master' },
  ],
  resources: [{ name: 'функтесты', description: 'общий стенд функциональных тестов' }],
  links: [
    { title: 'План переноса', href: 'https://wiki.example.local/pay-pg' },
    { title: 'Спека схемы PG', href: 'docs/spec.md' },
  ],
});
fs.writeFileSync(cfgPath, JSON.stringify(cfg, null, 2));

const { proc, url } = await startServer();
const origin = new URL(url).origin;
const token = new URL(url).searchParams.get('token');
async function post(p, body) {
  const resp = await fetch(origin + '/api/p/pay/' + p, {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Board-Token': token }, body: JSON.stringify(body),
  });
  const data = await resp.json();
  if (!resp.ok) throw new Error(p + ': ' + data.error);
  return data;
}
const status = (key, to) => post(`tasks/${key}/transition`, { to, actor: 'user' });
const update = (key, actor, fields) => post(`tasks/${key}/update`, { actor, fields });

let browser;
const results = {};
try {
  const tasks = [
    { key: 'PAY-101', title: 'Снять фактическую схему MSSQL и описать расхождения с моделью', repo: 'payments-api', kind: 'research', estimate: 1 },
    { key: 'PAY-102', title: 'Схема PostgreSQL и миграции для таблиц платежей', repo: 'payments-api', estimate: 2, deps: ['PAY-101'] },
    { key: 'PAY-103', title: 'Слой репозиториев с переключателем MSSQL / PG', repo: 'payments-api', estimate: 3, deps: ['PAY-102'] },
    { key: 'PAY-104', title: 'Мигратор исторических платежей с курсором и докачкой', repo: 'payments-worker', estimate: 3, deps: ['PAY-102'] },
    { key: 'PAY-105', title: 'Теневая запись платежей в PostgreSQL', repo: 'payments-api', estimate: 2, deps: ['PAY-103'] },
    { key: 'PAY-106', title: 'Джоба сверки MSSQL ↔ PG с отчётом расхождений', repo: 'payments-worker', estimate: 2, deps: ['PAY-102'] },
    { key: 'PAY-107', title: 'Метрики и алерты для PG-ветки', repo: 'payments-api', estimate: 1, deps: ['PAY-102'] },
    { key: 'PAY-108', title: 'Перевести отчёты по выручке на чтение из PG', repo: 'ledger-reports', estimate: 2, deps: ['PAY-104', 'PAY-106'] },
    { key: 'PAY-109', title: 'Выгрузка для бухгалтерии из PG', repo: 'ledger-reports', estimate: 1, deps: ['PAY-108'] },
    { key: 'PAY-110', title: 'Нагрузочный прогон PG-ветки на копии прода', repo: 'payments-api', kind: 'research', estimate: 1, deps: ['PAY-103'] },
    { key: 'PAY-111', title: 'Переключить чтение платежей на PG', repo: 'payments-api', estimate: 2, deps: ['PAY-105', 'PAY-106'] },
    { key: 'PAY-112', title: 'Отключить запись в MSSQL и убрать переключатель', repo: 'payments-worker', estimate: 1, deps: ['PAY-111', 'PAY-109'] },
    { key: 'PAY-113', title: 'Индексы и партиционирование таблицы операций', repo: 'payments-api', estimate: 2, deps: ['PAY-102'] },
    { key: 'PAY-114', title: 'Перенести хранимые процедуры в функции PG', repo: 'payments-api', estimate: 3, deps: ['PAY-102'] },
  ];
  for (const t of tasks) cli('add-task', '--json', JSON.stringify(t));
  await update('PAY-112', 'user', { rule_violations: [{ rule: 'одна задача — один репозиторий', text: 'затрагивает payments-api и payments-worker' }] });

  browser = await puppeteer.launch({ headless: true, args: ['--no-sandbox'] });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  await page.setViewport(VIEW);
  await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);
  await page.goto(url + '#/p/pay', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.querySelectorAll('.card[data-key]').length === 14, { timeout: 10000 });

  // стадия разбивки: нарушение правил в «Ждут вас»
  await page.waitForFunction(() => document.querySelector('.attn .it.er'), { timeout: 10000 });
  results.decompositionAttn = await page.$$eval('.attn .it', els => els.map(e => e.textContent));
  const decFile = path.join(out, 'board-decomposition-light.png');
  await page.screenshot({ path: decFile });
  results.board_decomposition_light = decFile;

  // переходим в работу и раскладываем задачи по статусам
  cli('stage', 'tracker');
  cli('stage', 'work');
  for (const k of ['PAY-101', 'PAY-102']) await status(k, 'done');
  await status('PAY-103', 'sent');
  await status('PAY-104', 'in_progress');
  await status('PAY-105', 'in_progress');
  await status('PAY-107', 'approved');
  await status('PAY-113', 'in_review');
  await status('PAY-114', 'cancelled');
  await post('tasks/PAY-106/questions', { question: { text: 'Какой допуск по расхождениям считать нормой для алерта?', options: ['0', '0,01%'] } });
  await post('tasks/PAY-105/questions', { question: { text: 'Что делать, если запись в PG не удалась после всех ретраев?', options: ['пропускать', 'outbox'] } });
  await status('PAY-105', 'in_progress');
  await post('locks/функтесты/acquire', { task: 'PAY-104', pid: process.pid });
  await post('locks/функтесты/acquire', { task: 'PAY-105', pid: process.pid });
  await update('PAY-104', 'system', { agent_state: 'running' });
  await update('PAY-104', 'agent', { summary: 'Пишет переливку батчами по 5 000, держит очередь «функтесты»' });
  await update('PAY-105', 'system', { base_branch: 'PAY-103' });
  await update('PAY-105', 'agent', { summary: 'Правки по раунду 2 внесены, ждёт функтесты' });
  await update('PAY-106', 'agent', { summary: 'Сверка готова на 70%, ждёт решения по допуску' });
  await update('PAY-107', 'agent', { summary: 'Ревью одобрено в раунде 2' });
  await update('PAY-110', 'system', { agent_state: 'error', turn_error: 'claude: процесс завершился с кодом 1' });
  await update('PAY-111', 'user', { external_blocker: 'Нет доступа к PG-реплике в проде — заявка у DBA' });
  await post('threads/PAY-113/messages', { author: 'agent', text: 'Раунд 2 открыт, 3 новых комментария от вас' });
  await post('threads/PAY-113/messages', { author: 'agent', text: 'Жду ревью' });
  await post('proposals', { kind: 'set_deps', payload: { key: 'PAY-111', deps: ['PAY-105', 'PAY-106', 'PAY-104'] }, reason: 'нужны ключи идемпотентности' });
  // остановка переписки агентов раннер пишет системным сообщением в тред задачи
  const threadFile = path.join(home, 'projects', 'pay', 'threads', 'PAY-106.jsonl');
  results.loopSeeded = fs.existsSync(path.dirname(threadFile));
  if (results.loopSeeded) {
    fs.appendFileSync(threadFile, JSON.stringify({ id: 'm-loop', author: 'system', text: LOOP, at: new Date().toISOString() }) + '\n');
    await update('PAY-106', 'agent', { summary: 'Сверка готова на 70%, ждёт решения по допуску ' });
  }

  await page.waitForFunction(() => document.querySelector('.card[data-key="PAY-110"] .chip.err')
    && document.querySelector('.card[data-key="PAY-105"] .qline')
    && document.querySelector('.card[data-key="PAY-111"] .chip.pa'), { timeout: 10000 });
  await new Promise(r => setTimeout(r, 1500));
  results.columns = await page.$$eval('.col', cols => Object.fromEntries(cols.map(c => [c.dataset.st, [...c.querySelectorAll('.card')].map(x => x.dataset.key)])));
  results.attn = await page.$$eval('.attn .it', els => els.map(e => e.textContent));
  for (const theme of ['light', 'dark']) {
    await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: theme }]);
    // переходы цвета колонок и кнопок должны закончиться до снимка
    await new Promise(r => setTimeout(r, 500));
    const file = path.join(out, `board-${theme}.png`);
    await page.screenshot({ path: file });
    results[`board_${theme}`] = file;
  }
  await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);

  // точечная перерисовка: несвязанная карточка остаётся тем же узлом с тем же содержимым
  await status('PAY-104', 'approved');
  await page.waitForFunction(() => document.querySelector('.col[data-st="approved"] .card[data-key="PAY-104"]'), { timeout: 10000 });
  await new Promise(r => setTimeout(r, 1300));
  const untouched = await page.$('.card[data-key="PAY-110"] .ttl');
  await page.$eval('.card[data-key="PAY-106"]', el => { el.dataset.probe = '1'; });

  // меню статуса по правой кнопке; одобрение PAY-106 разблокирует PAY-108 — тост с реакцией
  const card106 = await page.$('.card[data-key="PAY-106"]');
  await card106.evaluate(el => el.scrollIntoView({ block: 'nearest', inline: 'nearest' }));
  const box = await card106.boundingBox();
  await page.mouse.click(box.x + 60, box.y + 30, { button: 'right' });
  await page.waitForSelector('.stmenu.floating .menu', { timeout: 5000 });
  const menuFile = path.join(out, 'board-status-menu-light.png');
  await page.screenshot({ path: menuFile });
  results.status_menu_light = menuFile;
  results.menuItems = await page.$$eval('.stmenu.floating .menu button', els => els.map(e => e.textContent));
  await page.evaluate(() => [...document.querySelectorAll('.stmenu.floating .menu button')].find(b => b.textContent.startsWith('Одобрена')).click());
  await page.waitForFunction(() => document.querySelector('.col[data-st="approved"] .card[data-key="PAY-106"]')
    && document.querySelector('.col[data-st="not_started"] .card[data-key="PAY-108"]'), { timeout: 10000 });
  results.reactionToast = await page.$eval('.toast:last-child', el => el.textContent);
  results.movedNodeKept = await page.$eval('.card[data-key="PAY-106"]', el => el.dataset.probe === '1' && el.classList.contains('flash'));
  results.untouchedKept = await page.evaluate(el => el.isConnected, untouched);
  await new Promise(r => setTimeout(r, 300));
  const reactFile = path.join(out, 'board-reaction-toast-light.png');
  await page.screenshot({ path: reactFile });
  results.reaction_toast_light = reactFile;

  // перетаскивание в «Заблокирована»: сервер отказывает, тост с его текстом, карточка на месте
  await new Promise(r => setTimeout(r, 4200));
  await page.evaluate(() => {
    const card = document.querySelector('.card[data-key="PAY-110"]');
    const col = document.querySelector('.col[data-st="blocked"]');
    const dt = new DataTransfer();
    card.dispatchEvent(new DragEvent('dragstart', { bubbles: true, dataTransfer: dt }));
    col.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer: dt }));
    col.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: dt }));
    card.dispatchEvent(new DragEvent('dragend', { bubbles: true, dataTransfer: dt }));
  });
  await page.waitForSelector('.toast.err', { timeout: 10000 });
  results.errorToast = await page.$eval('.toast.err', el => el.textContent);
  results.cardStayed = await page.$eval('.card[data-key="PAY-110"]', el => el.closest('.col').dataset.st);
  await new Promise(r => setTimeout(r, 500));
  const errFile = path.join(out, 'board-error-toast-light.png');
  await page.screenshot({ path: errFile });
  results.error_toast_light = errFile;

  // фильтры переживают перезагрузку
  await page.select('.toolbar select', 'ledger-reports');
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.querySelectorAll('.card[data-key]').length === 2, { timeout: 10000 });
  results.filterPersisted = await page.$eval('.toolbar select', el => el.value);
  await page.select('.toolbar select', '');

  // тёмная тема из переключателя применяется до стилей: без мигания при загрузке
  await page.evaluate(() => localStorage.setItem('ab-theme', 'dark'));
  await page.reload({ waitUntil: 'domcontentloaded' });
  results.themeBeforeModules = await page.evaluate(() => document.documentElement.dataset.theme);
  await page.evaluate(() => localStorage.removeItem('ab-theme'));

  results.pageErrors = errors.filter(e => !e.includes('status of 400'));

  // макет того же размера для сравнения
  const mock = await browser.newPage();
  await mock.setViewport(VIEW);
  for (const theme of ['light', 'dark']) {
    await mock.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: theme }]);
    await mock.goto(pathToFileURL(path.join(repo, 'docs', 'design', 'mockups', 'board.html')).href, { waitUntil: 'load', timeout: 20000 }).catch(() => {});
    await new Promise(r => setTimeout(r, 500));
    const file = path.join(out, `mockup-board-${theme}.png`);
    await mock.screenshot({ path: file });
    results[`mockup_${theme}`] = file;
  }
} finally {
  if (browser) await browser.close();
  proc.kill('SIGTERM');
  await withTimeout(new Promise(r => proc.once('exit', r)), 10000, 'остановка сервера').catch(() => proc.kill('SIGKILL'));
  fs.rmSync(home, { recursive: true, force: true });
}
console.log(JSON.stringify(results, null, 2));
const ok = results.columns.blocked.join() === 'PAY-108,PAY-109,PAY-111,PAY-112'
  && results.reactionToast === 'PAY-106 → Одобрена. Разблокирована PAY-108. Разбужен агент PAY-108.'
  && results.movedNodeKept && results.untouchedKept
  && results.errorToast.includes('blocked') && results.cardStayed === 'not_started'
  && results.filterPersisted === 'ledger-reports' && results.themeBeforeModules === 'dark'
  && results.decompositionAttn.some(t => t.includes('нарушает правила'))
  && results.pageErrors.length === 0;
process.exit(ok ? 0 : 1);
