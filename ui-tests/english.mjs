// Английский интерфейс: проект с language=en и английскими данными, на основных экранах не должно быть кириллицы —
// список проектов, разбивка, доска, карточка (все вкладки), меню статуса, граф, панель «Проект», окна новой задачи
// и настроек проекта.
// Запуск из корня репозитория: node ui-tests/english.mjs
import path from 'node:path';
import puppeteer from 'puppeteer';
import { cleanupAll, createEnv, out } from './lib.mjs';

const CYRILLIC = /[А-Яа-яЁё]/;
const VIEW = { width: 1440, height: 900 };

// Видимый текст и подписи в атрибутах, где есть кириллица.
async function cyrillicOn(page) {
  return page.evaluate(src => {
    const re = new RegExp(src);
    const found = [];
    if (re.test(document.body.innerText)) {
      found.push(...document.body.innerText.split('\n').filter(line => re.test(line)).map(line => 'text: ' + line));
    }
    for (const el of document.querySelectorAll('[title], [aria-label], [placeholder], [data-cur]')) {
      for (const name of ['title', 'aria-label', 'placeholder', 'data-cur']) {
        const value = el.getAttribute(name);
        if (value && re.test(value)) found.push(name + ': ' + value);
      }
    }
    if (re.test(document.title)) found.push('document.title: ' + document.title);
    return found;
  }, CYRILLIC.source);
}

const env = createEnv({ prefix: 'ab-en-', slug: 'pay', title: 'Payments migration', language: 'en' });
const results = {};
let browser;
let ok = false;
try {
  await env.start({ runner: false });
  const post = env.post;
  const tasks = [
    { key: 'PAY-1', title: 'Payment tables schema', estimate: 1, description: 'Create the tables.', done_when: 'Migrations apply.' },
    { key: 'PAY-2', title: 'Shadow writes', estimate: 2, deps: ['PAY-1'], description: 'Write payments to both stores.', done_when: 'Data matches.' },
    { key: 'PAY-3', title: 'Load test', kind: 'research', estimate: 1, description: 'Measure latency.', done_when: 'Report with p99.' },
    { key: 'PAY-4', title: 'Switch reads', estimate: 1.5, deps: ['PAY-2'] },
  ];
  for (const task of tasks) await post('tasks', { actor: 'user', task });
  await post('tasks/PAY-4/update', { actor: 'user', fields: { rule_violations: [{ rule: 'One repo per task', text: 'Touches two services' }] } });
  await post('threads/_project/messages', { author: 'project_agent', text: 'The breakdown is ready for review.' });

  browser = await puppeteer.launch({ headless: true, args: ['--no-sandbox'] });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  await page.setViewport(VIEW);
  const check = async name => {
    results[name] = await cyrillicOn(page);
    await page.screenshot({ path: path.join(out, 'english-' + name + '.png') });
  };

  // список проектов: язык берётся из проектов
  await page.goto(env.url.replace(/#.*$/, '') + '#/', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.querySelector('.scr') && document.documentElement.lang === 'en', { timeout: 10000 });
  await check('projects');

  // разбивка
  await page.evaluate(() => { window.location.hash = '#/p/pay/decomposition'; });
  await page.waitForFunction(() => document.querySelectorAll('[data-key]').length >= 4 && document.documentElement.lang === 'en', { timeout: 10000 });
  await check('decomposition');

  // работа: вопрос, ревью, предложение, внешний блокер
  env.cli('stage', 'tracker');
  env.cli('stage', 'work');
  await post('tasks/PAY-1/transition', { to: 'in_progress', actor: 'agent' });
  await post('tasks/PAY-1/update', { actor: 'agent', fields: { summary: 'Tables are created.', todo: [{ text: 'Indexes', done: false }] } });
  await post('tasks/PAY-1/questions', { question: { text: 'Keep the old column?', options: ['Keep', 'Drop'], context: 'It is still read by reports.', recommended: 'Keep' } });
  await post('threads/PAY-1/messages', { author: 'agent', text: 'Started with the schema.' });
  await post('tasks/PAY-3/transition', { to: 'in_progress', actor: 'agent' });
  await post('tasks/PAY-3/transition', { to: 'in_review', actor: 'agent' });
  await post('tasks/PAY-4/update', { actor: 'user', fields: { external_blocker: 'Waiting for DBA access' } });
  await post('proposals', { kind: 'add_task', payload: { task: { key: 'PAY-5', title: 'Cleanup', deps: ['PAY-4'] } }, reason: 'Old tables must go.' });

  await page.evaluate(() => { window.location.hash = '#/p/pay'; });
  await page.waitForFunction(() => document.querySelectorAll('.card[data-key]').length === 4, { timeout: 10000 });
  await page.waitForFunction(() => document.querySelector('.sync') && document.querySelector('.sync').textContent === 'Online', { timeout: 10000 });
  await check('board');

  // меню статуса
  const box = await (await page.$('.card[data-key="PAY-1"]')).boundingBox();
  await page.mouse.click(box.x + 60, box.y + 30, { button: 'right' });
  await page.waitForSelector('.stmenu.floating .menu', { timeout: 5000 });
  await check('statusMenu');
  await page.keyboard.press('Escape');

  // карточка: все вкладки
  for (const key of ['PAY-1', 'PAY-3', 'PAY-4']) {
    await page.evaluate(k => { window.location.hash = '#/p/pay?task=' + k; }, key);
    await page.waitForSelector(`.drawer[data-key="${key}"]`, { timeout: 10000 });
    const tabs = await page.$$eval('.tabs button', els => els.map(e => e.dataset.tab));
    for (const tab of tabs) {
      await page.click(`.tabs [data-tab="${tab}"]`);
      await new Promise(r => setTimeout(r, 400));
      await check(`card-${key}-${tab}`);
    }
  }
  await page.evaluate(() => { window.location.hash = '#/p/pay'; });
  await page.waitForFunction(() => !document.querySelector('.drawer'), { timeout: 10000 });

  // панель «Проект» с предложением
  await page.evaluate(() => {
    if (document.querySelector('aside.dock[hidden]')) [...document.querySelectorAll('button')].find(b => b.textContent.startsWith('Project')).click();
  });
  await page.waitForSelector('aside.dock:not([hidden]) .proposal, aside.dock:not([hidden]) .popen > *', { timeout: 10000 });
  await check('projectPanel');

  // граф
  await page.click('[data-view="graph"]');
  await new Promise(r => setTimeout(r, 600));
  await check('graph');
  await page.click('[data-view="board"]');

  // окно новой задачи
  await page.evaluate(() => [...document.querySelectorAll('button')].find(b => b.textContent === 'New task').click());
  await page.waitForSelector('.modal input', { timeout: 10000 });
  await check('newTask');
  await page.keyboard.press('Escape');

  // окно настроек проекта
  await page.click('[data-action="settings"]');
  await page.waitForSelector('#st-max_agents', { timeout: 10000 });
  await check('settings');

  results.errors = errors;
  ok = Object.entries(results).every(([, v]) => Array.isArray(v) && v.length === 0);
} catch (e) {
  results.exception = String(e && e.stack || e);
} finally {
  if (browser) await browser.close();
  await cleanupAll();
}
console.log(JSON.stringify(results, null, 2));
console.log(ok ? 'ENGLISH OK' : 'ENGLISH FAILED');
process.exit(ok ? 0 : 1);
