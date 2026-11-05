// Граф, панель «Проект» и новая задача в headless Chrome на настоящем сервере.
// 13 задач на графе (цепочка и ромб), узлы без переполнения текста, клик открывает карточку, подсветка связей;
// предложения агента проекта: применение из панели и из карточки меняет граф вживую, отклонение уходит в историю,
// ошибка сервера показывается в блоке; новая задача с подсказкой ключа и проверкой полей; черновики ответа (U12).
// Запуск из корня репозитория: node ui-tests/graph.mjs
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
const home = fs.mkdtempSync(path.join(os.tmpdir(), 'ab-graph-'));
const env = { ...process.env, AGENT_BOARD_HOME: home, PYTHONPATH: path.join(repo, 'board'), AGENT_BOARD_PROJECT: 'pay', AGENT_BOARD_ACTOR: 'user' };
const cli = (...args) => execFileSync(python, ['-m', 'agent_board', ...args], { env, cwd: repo, encoding: 'utf-8', timeout: 20000 });
const VIEW = { width: 1440, height: 900 };
const sleep = ms => new Promise(r => setTimeout(r, ms));

function withTimeout(promise, ms, what) {
  return Promise.race([promise, new Promise((_, rej) => setTimeout(() => rej(new Error('таймаут: ' + what)), ms))]);
}

async function startServer() {
  const port = 20000 + Math.floor(Math.random() * 1000);
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
cli('init', 'pay', '--title', 'Перенос платежей на PostgreSQL');
const cfgPath = path.join(home, 'projects', 'pay', 'project.json');
const cfg = JSON.parse(fs.readFileSync(cfgPath, 'utf-8'));
Object.assign(cfg, {
  harness: { type: 'fake' },
  repos: [{ name: 'payments-api', path: home, base: 'master' }, { name: 'payments-worker', path: home, base: 'master' }],
});
fs.writeFileSync(cfgPath, JSON.stringify(cfg, null, 2));

const LONG = 'Переключить чтение платежей на PostgreSQL по проценту трафика с быстрым откатом и сверкой ПереключательЧтенияПлатежейБезПробелов';
const tasks = [
  ['PAY-101', 'Снять схему таблиц платежей с прод-реплики', [], 1, 'research'],
  ['PAY-102', 'Схема платежей в PostgreSQL и миграции', ['PAY-101'], 2],
  ['PAY-103', 'Слой репозиториев с переключателем MSSQL / PG', ['PAY-102'], 3],
  ['PAY-104', 'Мигратор исторических платежей с курсором', ['PAY-102'], 2],
  ['PAY-105', 'Теневая запись платежей в PostgreSQL', ['PAY-103'], 2],
  ['PAY-106', 'Джоба сверки MSSQL ↔ PG с отчётом расхождений', ['PAY-103', 'PAY-104'], 1],
  ['PAY-107', 'Метрики и алерты PG-ветки', ['PAY-106'], 1],
  ['PAY-108', 'Отчёты по выручке читают из PG', ['PAY-107'], 4],
  ['PAY-109', 'Выгрузка для бухгалтерии из PG', ['PAY-108'], 1],
  ['PAY-110', 'Нагрузочный прогон PG-ветки на копии прода', [], 1, 'research'],
  ['PAY-111', LONG, ['PAY-105', 'PAY-106'], 2],
  ['PAY-112', 'Отключить запись в MSSQL и убрать переключатель', ['PAY-111'], 1],
  ['PAY-113', 'Индексы и партиционирование таблицы операций', ['PAY-110'], 1],
  ['PAY-114', 'Перенести хранимые процедуры комиссий', [], 2],
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
const status = (key, to) => post('tasks/' + key + '/transition', { to, actor: 'user' });

let browser;
let page;
const results = {};
const shots = {};
try {
  for (const [key, title, deps, estimate, kind] of tasks) {
    cli('add-task', '--json', JSON.stringify({ key, title, deps, estimate, kind: kind || 'code', repo: key === 'PAY-104' ? 'payments-worker' : 'payments-api' }));
  }
  cli('stage', 'tracker');
  for (const k of ['PAY-101', 'PAY-102']) await status(k, 'done');
  for (const k of ['PAY-103', 'PAY-104']) await status(k, 'in_progress');
  await status('PAY-110', 'done');
  await status('PAY-113', 'in_review');
  await status('PAY-114', 'cancelled');
  await post('tasks/PAY-103/questions', { question: { text: 'Какой порог ошибок считать нормой?', options: ['0', '0,01%'], recommended: '0' } });
  await post('proposals', { kind: 'add_task', reason: 'Сверка нашла 3,2% старых платежей без ключа идемпотентности.',
    payload: { task: { key: 'PAY-115', title: 'Заполнить ключи идемпотентности', repo: 'payments-worker', kind: 'code', estimate: 1, deps: ['PAY-104'] } } });
  await post('proposals', { kind: 'set_deps', reason: 'Метрики нужны и для теневой записи.', payload: { key: 'PAY-107', deps: ['PAY-106', 'PAY-105'] } });
  await post('proposals', { kind: 'cancel_task', reason: 'Отключение MSSQL — отдельный проект.', payload: { key: 'PAY-112' } });

  browser = await puppeteer.launch({ headless: true, args: ['--no-sandbox'] });
  page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !m.text().includes('400')) errors.push(m.text()); });
  await page.setViewport(VIEW);
  await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);
  await page.goto(url + '#/p/pay', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.querySelectorAll('.dock .popen .proposal').length === 3, { timeout: 10000 });
  results.proposalTitles = await page.$$eval('.dock .popen .proposal .pt', els => els.map(e => e.textContent));
  results.projectBtnPressed = await page.$eval('.row1 .btn[aria-pressed]', el => el.getAttribute('aria-pressed'));
  results.chipOn107 = await page.$eval('.card[data-key="PAY-107"]', el => Boolean(el.querySelector('.chip.pa')));

  // граф вместо колонок
  await page.click('[data-view="graph"]');
  await page.waitForFunction(() => document.querySelectorAll('.graph-wrap g.node').length === 13, { timeout: 10000 });
  results.colsHidden = await page.$eval('.cols', el => el.hidden);
  results.cancelledOnGraph = await page.$('g.node[data-key="PAY-114"]') !== null;
  await sleep(300);
  results.overflow = await page.$$eval('.graph-wrap g.node', nodes => nodes.flatMap(n => {
    const box = n.querySelector('rect.bg').getBoundingClientRect();
    const inside = el => {
      const r = el.getBoundingClientRect();
      return r.left >= box.left - 0.5 && r.right <= box.right + 0.5 && r.top >= box.top - 0.5 && r.bottom <= box.bottom + 0.5;
    };
    const gn = n.querySelector('.gn');
    const gs = n.querySelector('.gs');
    const gt = n.querySelector('.gt');
    const lines = Math.round(gt.getBoundingClientRect().height / parseFloat(getComputedStyle(gt).lineHeight));
    const bad = [];
    if (gn.scrollWidth > gn.clientWidth || gn.scrollHeight > gn.clientHeight) bad.push('gn');
    if (gs.scrollWidth > gs.clientWidth) bad.push('status-cut');
    if (gs.getBoundingClientRect().height > 20) bad.push('status-wraps');
    for (const sel of ['.gk', '.gs', '.gt']) if (!inside(n.querySelector(sel))) bad.push(sel);
    if (lines > 2) bad.push('lines:' + lines);
    return bad.length ? [n.dataset.key + ':' + bad.join(',')] : [];
  }));
  // граф шире области при открытой панели: у правого края затемнение, прокрутка показывает последний слой
  results.panBefore = await page.evaluate(() => {
    const wrap = document.querySelector('.graph-wrap');
    const frame = document.querySelector('.graph-frame');
    return { wider: wrap.scrollWidth > wrap.clientWidth, right: frame.classList.contains('can-r'), left: frame.classList.contains('can-l') };
  });
  await page.$eval('.graph-wrap', el => { el.scrollLeft = el.scrollWidth; });
  await sleep(200);
  results.panAfter = await page.evaluate(() => {
    const wrap = document.querySelector('.graph-wrap');
    const frame = document.querySelector('.graph-frame');
    const box = wrap.getBoundingClientRect();
    const nodes = [...wrap.querySelectorAll('g.node')];
    const right = Math.max(...nodes.map(n => n.getBoundingClientRect().right));
    const last = nodes.filter(n => n.getBoundingClientRect().right === right).map(n => n.dataset.key);
    return { lastVisible: right <= box.right && right > box.left, last, right: frame.classList.contains('can-r'), left: frame.classList.contains('can-l') };
  });
  const panFile = path.join(out, 'graph-scrolled-light.png');
  await page.screenshot({ path: panFile });
  shots.graph_scrolled_light = panFile;
  await page.$eval('.graph-wrap', el => { el.scrollLeft = 0; });
  await sleep(200);
  results.longTitleLines = await page.$eval('g.node[data-key="PAY-111"] .gt', gt => Math.round(gt.getBoundingClientRect().height / parseFloat(getComputedStyle(gt).lineHeight)));
  results.critical = await page.$$eval('.graph-wrap path.edge.crit', els => els.map(e => e.parentNode.dataset.key));
  results.okEdges = await page.$$eval('.graph-wrap path.edge.ok', els => els.length);
  // ромб 102 → 103/104 → 106: 103 и 104 в одном слое, 106 правее обоих
  results.diamond = await page.evaluate(() => {
    const x = k => document.querySelector(`g.node[data-key="${k}"]`).getBoundingClientRect().left;
    return x('PAY-103') === x('PAY-104') && x('PAY-106') > x('PAY-103') && x('PAY-103') > x('PAY-102');
  });
  for (const theme of ['light', 'dark']) {
    await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: theme }]);
    await sleep(200);
    const file = path.join(out, `graph-${theme}.png`);
    await page.screenshot({ path: file });
    shots[`graph_${theme}`] = file;
  }
  await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);
  await page.hover('g.node[data-key="PAY-106"]');
  results.hoverEdges = await page.$$eval('.graph-wrap path.edge.hl', els => els.map(e => e.parentNode.dataset.key).sort());
  const hoverFile = path.join(out, 'graph-hover-light.png');
  await page.screenshot({ path: hoverFile });
  shots.graph_hover_light = hoverFile;
  await page.mouse.move(5, 5);

  // клик по узлу открывает карточку; в ней — предложение, затрагивающее задачу
  await page.click('g.node[data-key="PAY-107"]');
  await page.waitForSelector('.drawer[data-key="PAY-107"] .pa-note', { timeout: 10000 });
  results.hashAfterNode = await page.evaluate(() => location.hash);
  results.cardNote = await page.$eval('.drawer .pa-note', el => el.textContent);
  await sleep(500);
  const noteFile = path.join(out, 'card-proposal-light.png');
  await page.screenshot({ path: noteFile });
  shots.card_proposal_light = noteFile;
  // применение из карточки: блок исчезает, на графе появляется связь PAY-105 → PAY-107
  await page.click('.drawer .pa-note .btn.ok');
  results.cardApplyDisabled = await page.$eval('.drawer .pa-note .btn.ok', el => el.disabled).catch(() => true);
  await page.waitForFunction(() => !document.querySelector('.drawer .pa-note'), { timeout: 10000 });
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => !document.querySelector('.drawer'), { timeout: 5000 });
  await page.waitForSelector('.graph-wrap g[data-key="PAY-105>PAY-107"]', { timeout: 10000 });
  results.edgeFromCard = true;

  // применение из панели: новая задача PAY-115 появляется на графе вживую
  const applyIn = async text => {
    await page.evaluate(t => [...document.querySelectorAll('.dock .popen .proposal')].find(p => p.textContent.includes(t)).querySelector('.btn.ok').click(), text);
  };
  await applyIn('PAY-115');
  await page.waitForSelector('g.node[data-key="PAY-115"]', { timeout: 10000 });
  results.newNodeEdge = await page.$('.graph-wrap g[data-key="PAY-104>PAY-115"]') !== null;
  await page.waitForFunction(() => document.querySelectorAll('.dock .phist .proposal.applied').length === 2, { timeout: 10000 });

  // отклонение: предложение уходит в свёрнутую историю, граф не меняется
  await page.evaluate(() => [...document.querySelectorAll('.dock .popen .proposal')].find(p => p.textContent.includes('PAY-112'))
    .querySelector('.pa-act .btn:not(.ok)').click());
  await page.waitForFunction(() => document.querySelectorAll('.dock .popen .proposal').length === 0
    && document.querySelectorAll('.dock .phist .proposal.rejected').length === 1, { timeout: 10000 });
  results.historyOpen = await page.$eval('.dock .phistory', el => el.open);
  results.historySummary = await page.$eval('.dock .phistory summary', el => el.textContent);
  results.rejectedStillOnGraph = await page.$eval('g.node[data-key="PAY-112"]', el => !el.classList.contains('dim'));

  // ошибка сервера при применении — внутри блока, кнопки снова активны
  await post('proposals', { kind: 'add_task', reason: 'Профиль нагрузки после переключения.', payload: { task: { key: 'PAY-116', title: 'Профиль нагрузки', kind: 'research' } } });
  await page.waitForFunction(() => document.querySelectorAll('.dock .popen .proposal').length === 1, { timeout: 10000 });
  cli('add-task', '--json', JSON.stringify({ key: 'PAY-116', title: 'Уже заведена вручную' }));
  await applyIn('PAY-116');
  await page.waitForSelector('.dock .popen .proposal .ferr:not([hidden])', { timeout: 10000 });
  results.applyError = await page.$eval('.dock .popen .proposal .ferr', el => el.textContent);
  results.applyEnabledAfterError = await page.$eval('.dock .popen .proposal .btn.ok', el => !el.disabled);
  await page.$eval('.dock .phistory', el => { el.open = true; });
  for (const theme of ['light', 'dark']) {
    await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: theme }]);
    await sleep(200);
    const file = path.join(out, `project-panel-${theme}.png`);
    await page.screenshot({ path: file });
    shots[`project_panel_${theme}`] = file;
  }
  await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);

  // описание открытого предложения следует за текущими зависимостями задачи
  await post('proposals', { kind: 'set_deps', reason: 'Отчёты нужны сразу после теневой записи.', payload: { key: 'PAY-108', deps: ['PAY-107', 'PAY-105'] } });
  const block108 = () => page.evaluate(() => {
    const b = [...document.querySelectorAll('.dock .popen .proposal')].find(p => p.textContent.includes('PAY-108: зависимости'));
    return b ? b.textContent : '';
  });
  await page.waitForFunction(() => [...document.querySelectorAll('.dock .popen .proposal')].some(p => p.textContent.includes('PAY-108: зависимости')), { timeout: 10000 });
  results.staleBefore = await block108();
  await post('tasks/PAY-108/deps', { deps: ['PAY-107', 'PAY-104'], actor: 'user' });
  await page.waitForFunction(() => [...document.querySelectorAll('.dock .popen .proposal')].some(p => p.textContent.includes('больше не ждёт PAY-104')), { timeout: 10000 });
  results.staleAfter = await block108();
  await page.evaluate(() => [...document.querySelectorAll('.dock .popen .proposal')].find(p => p.textContent.includes('PAY-108: зависимости'))
    .querySelector('.pa-act .btn:not(.ok)').click());
  await page.waitForFunction(() => [...document.querySelectorAll('.dock .phist .proposal.rejected')].some(p => p.textContent.includes('PAY-108')), { timeout: 10000 });
  // в истории изменения остаются такими, какими их видели до обработки
  results.historyRows = await page.$$eval('.dock .phist .proposal', els => els.map(e => e.querySelector('.pt').textContent + ' | ' + [...e.querySelectorAll('.chg > div')].map(r => r.textContent).join(' / ')));

  // тред проекта: сообщение пользователя появляется в панели
  await page.type('.dock .foot textarea', 'Где сейчас узкое место проекта?');
  await page.keyboard.down('Meta');
  await page.keyboard.press('Enter');
  await page.keyboard.up('Meta');
  await page.waitForFunction(() => [...document.querySelectorAll('.dock .thread .msg.user')].some(m => m.textContent.includes('узкое место')), { timeout: 10000 });
  results.projectMessage = true;

  // новая задача: ключ предложен, пустой заголовок не отправляется, ошибка сервера — под своим полем
  await page.click('.toolbar .btn.primary');
  await page.waitForSelector('.modal.newtask', { timeout: 5000 });
  results.suggestedKey = await page.$eval('#nt-key', el => el.value);
  await page.click('.modal footer .btn.primary');
  results.titleError = await page.$eval('#nt-title ~ .ferr', el => !el.hidden && el.textContent);
  await page.type('#nt-title', 'Ночная сверка выгрузки для бухгалтерии');
  await page.type('#nt-description', 'Сравнить выгрузки из MSSQL и PG за месяц.');
  await page.type('#nt-estimate', '1,5');
  await page.type('.modal .pick-search', '109');
  results.pickerVisible = await page.$$eval('#nt-deps label', els => els.filter(e => !e.hidden).map(e => e.querySelector('.k').textContent));
  await page.click('#nt-deps label:not([hidden]) input');
  results.preview = await page.$eval('.modal .preview', el => el.textContent);
  for (const theme of ['light', 'dark']) {
    await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: theme }]);
    await sleep(200);
    const file = path.join(out, `new-task-${theme}.png`);
    await page.screenshot({ path: file });
    shots[`new_task_${theme}`] = file;
  }
  await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);
  await page.setRequestInterception(true);
  const fail = req => {
    if (req.method() === 'POST' && req.url().endsWith('/api/p/pay/tasks')) {
      req.respond({ status: 400, contentType: 'application/json', body: JSON.stringify({ error: 'Зависимость от неизвестной задачи: PAY-999' }) });
    } else req.continue();
  };
  page.on('request', fail);
  await page.click('.modal footer .btn.primary');
  await page.waitForSelector('#nt-deps ~ .ferr:not([hidden])', { timeout: 5000 });
  results.depsServerError = await page.$eval('#nt-deps ~ .ferr', el => el.textContent);
  page.off('request', fail);
  await page.setRequestInterception(false);
  await page.click('.modal footer .btn.primary');
  await page.waitForSelector('.drawer[data-key="' + results.suggestedKey + '"]', { timeout: 10000 });
  results.newTaskHash = await page.evaluate(() => location.hash);
  results.modalClosed = await page.$('.modal') === null;
  await page.keyboard.press('Escape');
  await page.waitForSelector(`.graph-wrap g[data-key="PAY-109>${results.suggestedKey}"]`, { timeout: 10000 });
  results.newTaskOnGraph = true;

  // U12: черновик ответа на вопрос, Esc при наборе через IME, пустой внешний блокер
  await page.click('g.node[data-key="PAY-103"]');
  await page.waitForSelector('.drawer[data-key="PAY-103"] .question', { timeout: 10000 });
  await page.click('.question .opt:nth-child(2) input');
  await page.type('.question textarea', 'Ноль, кроме последних минут суток');
  await page.evaluate(() => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', isComposing: true, bubbles: true })));
  results.imeEscKeepsCard = await page.$('.drawer') !== null;
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => !document.querySelector('.drawer'), { timeout: 5000 });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.graph-wrap g.node[data-key="PAY-103"]', { timeout: 10000 });
  results.viewAfterReload = await page.$eval('[data-view="graph"]', el => el.getAttribute('aria-pressed'));
  await page.click('g.node[data-key="PAY-103"]');
  await page.waitForSelector('.drawer[data-key="PAY-103"] .question', { timeout: 10000 });
  results.answerDraft = await page.$eval('.question textarea', el => el.value);
  results.choiceDraft = await page.$eval('.question .opt input:checked', el => el.value);
  await page.click('.drawer .dh .stbtn');
  await page.evaluate(() => [...document.querySelectorAll('.drawer .menu button')].find(b => b.textContent === 'Внешний блокер…').click());
  await page.waitForSelector('.modal #ext-blocker', { timeout: 5000 });
  await page.click('.modal footer .btn.primary');
  results.blockerEmpty = await page.$eval('.modal .ferr', el => !el.hidden && el.textContent);
  results.blockerMax = await page.$eval('#ext-blocker', el => el.maxLength);
  await page.evaluate(() => import('/static/js/ui.js').then(m => m.toast('Проверка тоста над окном')));
  results.toastAboveFooter = await page.evaluate(() => {
    const t = [...document.querySelectorAll('.toast')].pop().getBoundingClientRect();
    const f = document.querySelector('.modal footer').getBoundingClientRect();
    return t.bottom <= f.top;
  });
  await page.keyboard.press('Escape');
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => !document.querySelector('.drawer'), { timeout: 5000 });

  // панель сворачивается и остаётся свёрнутой после перезагрузки
  await page.click('.dock header .btn');
  results.focusAfterCollapse = await page.evaluate(() => document.activeElement === document.querySelector('.row1 .btn[aria-pressed]'));
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.graph-wrap g.node', { timeout: 10000 });
  results.dockAfterReload = await page.$eval('.dock', el => el.hidden);
  results.projectBtnAfterReload = await page.$eval('.row1 .btn[aria-pressed]', el => el.getAttribute('aria-pressed'));
  await page.click('.row1 .btn[aria-pressed]');
  results.dockReopened = await page.$eval('.dock', el => !el.hidden);

  // узкий экран: панель поверх доски закрывается по Esc и клику мимо
  // щелчок мимо панели — по легенде графа: узел открыл бы карточку, а щелчки по карточке панель не закрывают
  const clickOutside = async () => {
    const box = await (await page.$('.graphview .legend')).boundingBox();
    await page.mouse.click(box.x + 4, box.y + box.height / 2);
  };
  await page.setViewport({ width: 800, height: 900 });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.graph-wrap g.node', { timeout: 10000 });
  results.narrowDefaultHidden = await page.$eval('.dock', el => el.hidden);
  await page.click('.row1 .btn[aria-pressed]');
  results.narrowOverlay = await page.$eval('.dock', el => !el.hidden && getComputedStyle(el).position === 'fixed');
  await page.keyboard.press('Escape');
  results.narrowEscCloses = await page.$eval('.dock', el => el.hidden);
  results.narrowEscFocus = await page.evaluate(() => document.activeElement === document.querySelector('.row1 .btn[aria-pressed]'));
  await page.click('.row1 .btn[aria-pressed]');
  await clickOutside();
  results.narrowClickOutside = await page.$eval('.dock', el => el.hidden);
  results.noCardFromOutside = await page.$('.drawer') === null;
  // пункт «Ждут вас» открывает панель, и тот же щелчок её не закрывает; щелчок мимо — закрывает
  await page.evaluate(() => [...document.querySelectorAll('.attn .it.pa')][0].click());
  await sleep(100);
  results.narrowAttnOpens = await page.$eval('.dock', el => !el.hidden);
  await clickOutside();
  results.narrowAttnThenOutside = await page.$eval('.dock', el => el.hidden);
  await page.setViewport(VIEW);
  results.pageErrors = errors;

  // макет того же размера: граф, панель «Проект», новая задача
  const mock = await browser.newPage();
  await mock.setViewport(VIEW);
  await mock.goto(pathToFileURL(path.join(repo, 'docs', 'design', 'mockups', 'board.html')).href, { waitUntil: 'load', timeout: 20000 }).catch(() => {});
  for (const theme of ['light', 'dark']) {
    await mock.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: theme }]);
    await mock.click('[data-view="graph"]').catch(() => {});
    await sleep(300);
    let file = path.join(out, `mockup-graph-${theme}.png`);
    await mock.screenshot({ path: file });
    shots[`mockup_graph_${theme}`] = file;
    await mock.click('[data-view="board"]').catch(() => {});
    await mock.evaluate(() => { const d = document.querySelector('#dock'); if (d.hidden) document.querySelector('#dockBtn').click(); });
    await sleep(300);
    file = path.join(out, `mockup-project-panel-${theme}.png`);
    await mock.screenshot({ path: file });
    shots[`mockup_project_panel_${theme}`] = file;
    await mock.click('#newBtn').catch(() => {});
    await sleep(300);
    file = path.join(out, `mockup-new-task-${theme}.png`);
    await mock.screenshot({ path: file });
    shots[`mockup_new_task_${theme}`] = file;
    await mock.keyboard.press('Escape');
  }
} catch (e) {
  // снимок экрана в момент сбоя помогает понять, на каком шаге всё остановилось
  if (page) await page.screenshot({ path: path.join(out, 'graph-failure.png') }).catch(() => {});
  console.log(JSON.stringify(results, null, 2));
  throw e;
} finally {
  if (browser) await browser.close();
  proc.kill('SIGTERM');
  await withTimeout(new Promise(r => proc.once('exit', r)), 15000, 'остановка сервера').catch(() => proc.kill('SIGKILL'));
  fs.rmSync(home, { recursive: true, force: true });
}
console.log(JSON.stringify({ results, shots }, null, 2));
const ok = results.proposalTitles.length === 3 && results.projectBtnPressed === 'true' && results.chipOn107
  && results.panBefore.wider && results.panBefore.right && !results.panBefore.left
  && results.panAfter.lastVisible && results.panAfter.left && !results.panAfter.right
  && results.staleBefore.includes('будет ждать PAY-105') && !results.staleBefore.includes('больше не ждёт')
  && results.staleAfter.includes('будет ждать PAY-105') && results.staleAfter.includes('больше не ждёт PAY-104')
  && results.historyRows.some(r => r.startsWith('PAY-108: зависимости') && r.includes('больше не ждёт PAY-104'))
  && results.historyRows.some(r => r.startsWith('PAY-107: зависимости') && r.includes('будет ждать PAY-105'))
  && results.toastAboveFooter && results.focusAfterCollapse
  && results.narrowDefaultHidden && results.narrowOverlay && results.narrowEscCloses && results.narrowEscFocus && results.narrowClickOutside && results.noCardFromOutside && results.narrowAttnOpens && results.narrowAttnThenOutside
  && results.colsHidden && !results.cancelledOnGraph && results.overflow.length === 0 && results.longTitleLines === 2
  && results.critical.length > 0 && results.okEdges > 0 && results.diamond
  && results.hoverEdges.join() === 'PAY-103>PAY-106,PAY-104>PAY-106,PAY-106>PAY-107,PAY-106>PAY-111'
  && results.hashAfterNode === '#/p/pay?task=PAY-107' && results.cardNote.includes('PAY-107: зависимости → PAY-106, PAY-105')
  && results.edgeFromCard && results.newNodeEdge && !results.historyOpen && results.historySummary.endsWith('· 3') && results.rejectedStillOnGraph
  && results.applyError.includes('уже существует') && results.applyEnabledAfterError && results.projectMessage
  && results.suggestedKey === 'PAY-117' && results.titleError === 'Укажите заголовок' && results.pickerVisible.join() === 'PAY-109'
  && results.preview.includes('ждёт PAY-109') && results.depsServerError.includes('PAY-999')
  && results.newTaskHash === '#/p/pay?task=PAY-117' && results.modalClosed && results.newTaskOnGraph
  && results.imeEscKeepsCard && results.viewAfterReload === 'true'
  && results.answerDraft === 'Ноль, кроме последних минут суток' && results.choiceDraft === '0,01%'
  && results.blockerEmpty === 'Укажите причину блокера' && results.blockerMax === 500
  && results.dockAfterReload && results.projectBtnAfterReload === 'false' && results.dockReopened
  && results.pageErrors.length === 0;
process.exit(ok ? 0 : 1);
