// Доска проекта «Без плана»: в шапке «← Проекты» и чип типа без шкалы стадий, нет прогресса по плану,
// «Планирования», графа и волн; пустые «Не начата» и «Заблокирована» скрыты, «Отправлена» и «Готово» свёрнуты;
// панель «Проект» открыта с подсказкой про создание задач; задача агента проекта подписана. Проект «По плану»
// на той же доске выглядит как раньше. Скриншоты в светлой и тёмной теме — без ошибок консоли.
// Запуск из корня репозитория: node ui-tests/free.mjs
import fs from 'node:fs';
import path from 'node:path';
import puppeteer from 'puppeteer';
import { cleanupAll, createEnv, out, sleep } from './lib.mjs';

const VIEW = { width: 1440, height: 900 };

const env = createEnv({ prefix: 'ab-free-', slug: 'fix', title: 'Мелкие доработки', language: 'ru', config: { kind: 'free', stage: 'work' } });
const results = {};
let browser;
let ok = false;
const errors = [];

// второй проект — «По плану», для сравнения
env.cli('init', 'pay', '--title', 'Перенос платежей');
const payCfg = path.join(env.home, 'projects', 'pay', 'project.json');
fs.writeFileSync(payCfg, JSON.stringify({ ...JSON.parse(fs.readFileSync(payCfg, 'utf-8')), language: 'ru', harness: { type: 'fake' } }, null, 2));

// задачи в разных статусах: заводятся через доску, статус и автор правятся в файлах при остановленном сервере
const tasks = [
  ['FIX-1', 'Поднять таймаут ретраев оплаты', 'in_progress', 'project_agent'],
  ['FIX-2', 'Убрать дубли в истории', 'in_review', 'user'],
  ['FIX-3', 'Логи в выгрузке счетов', 'sent', 'user'],
  ['FIX-4', 'Починить флапающий тест', 'done', 'user'],
];
async function seed() {
  await env.start({ runner: false });
  for (const [key, title] of tasks) env.cli('add-task', '--json', JSON.stringify({ key, title, repo: '' }));
  await env.stop();
  for (const [key, , status, by] of tasks) {
    const file = path.join(env.home, 'projects', 'fix', 'tasks', key + '.json');
    fs.writeFileSync(file, JSON.stringify({ ...JSON.parse(fs.readFileSync(file, 'utf-8')), status, created_by: by }, null, 2));
  }
}

const visible = (page, sel) => page.evaluate(s => {
  const el = document.querySelector(s);
  return Boolean(el) && el.getClientRects().length > 0;
}, sel);

async function freeBoard(page) {
  return page.evaluate(() => {
    const col = id => document.querySelector('.col[data-st="' + id + '"]');
    const shown = el => Boolean(el) && el.getClientRects().length > 0;
    const card = document.querySelector('.card .key') && [...document.querySelectorAll('.card')]
      .find(c => c.querySelector('.key').textContent === 'FIX-1');
    return {
      crumb: shown(document.querySelector('.top .crumb')),
      kind: (document.querySelector('.top .ptl .kind') || {}).textContent,
      sub: (document.querySelector('.top .proj .sub') || {}).textContent,
      stepper: shown(document.querySelector('.stepper')),
      bar: shown(document.querySelector('.progress .bar')),
      pills: document.querySelectorAll('.progress .pill').length,
      planning: shown(document.querySelector('.artifacts')),
      graphToggle: shown(document.querySelector('[data-view="graph"]')),
      waves: shown(document.querySelector('[data-filter="wave"]')),
      notStarted: shown(col('not_started')),
      blocked: shown(col('blocked')),
      sentCollapsed: col('sent').classList.contains('collapsed'),
      doneCollapsed: col('done').classList.contains('collapsed'),
      workCollapsed: col('in_progress').classList.contains('collapsed'),
      dock: shown(document.querySelector('.dock')),
      placeholder: (document.querySelector('.dock textarea') || {}).placeholder,
      byAgent: card ? (card.querySelector('.by-agent') || {}).textContent : null,
      othersByAgent: document.querySelectorAll('.card .by-agent').length,
    };
  });
}

try {
  await seed();
  await env.start({ runner: false });

  browser = await puppeteer.launch({ headless: true, args: ['--no-sandbox'] });
  const page = await browser.newPage();
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);
  await page.setViewport(VIEW);
  const base = env.url.replace(/#.*$/, '');
  await page.goto(base + '#/p/fix', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.querySelectorAll('.card').length === 4, { timeout: 10000 });
  await sleep(300);

  const free = await freeBoard(page);
  results.free = free.crumb && free.kind === 'Без плана' && free.sub.endsWith('ревью перед коммитом')
    && !free.stepper && !free.bar && free.pills > 0 && !free.planning && !free.graphToggle && !free.waves
    && !free.notStarted && !free.blocked && free.sentCollapsed && free.doneCollapsed && !free.workCollapsed
    && free.dock && free.placeholder === 'Опишите, что сделать, — агент заведёт задачи…'
    && free.byAgent === 'Создана агентом проекта по вашей просьбе' && free.othersByAgent === 1 ? 'ok' : free;
  await page.screenshot({ path: path.join(out, 'free-light.png') });

  // подпись и в открытой карточке задачи
  await page.goto(base + '#/p/fix?task=FIX-1', { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.drawer .dh .by-agent', { timeout: 10000 });
  results.drawer = await page.$eval('.drawer .dh .by-agent', el => el.textContent) === 'Создана агентом проекта по вашей просьбе'
    ? 'ok' : 'нет подписи в карточке';
  await page.keyboard.press('Escape');

  // закрытая панель остаётся закрытой после перезагрузки
  await page.goto(base + '#/p/fix', { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.dock', { visible: true, timeout: 10000 });
  await page.click('.dock header button');
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.querySelectorAll('.card').length === 4, { timeout: 10000 });
  await sleep(300);
  results.dockRemembered = !(await visible(page, '.dock')) ? 'ok' : 'панель снова открыта';

  // проект «По плану» — как раньше: шкала стадий, прогресс, граф, все колонки
  await page.goto(base + '#/p/pay', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => (document.querySelector('.top h1') || {}).textContent === 'Перенос платежей', { timeout: 10000 });
  await sleep(300);
  const plan = await freeBoard(page);
  results.plan = !plan.crumb && plan.stepper && plan.bar && plan.planning && plan.graphToggle && plan.waves
    && plan.notStarted && plan.blocked && !plan.sentCollapsed && !plan.doneCollapsed
    && plan.placeholder === 'Спросить агента проекта…' ? 'ok' : plan;

  // тёмная тема
  await page.evaluate(() => {
    localStorage.setItem('ab-theme', 'dark');
    localStorage.removeItem('ab-dock:fix');
  });
  await page.goto(base + '#/p/fix', { waitUntil: 'domcontentloaded' });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.querySelectorAll('.card').length === 4, { timeout: 10000 });
  await page.waitForSelector('.dock', { visible: true, timeout: 10000 });
  await sleep(300);
  results.dark = await page.evaluate(() => document.documentElement.dataset.theme) === 'dark' ? 'ok' : 'no theme';
  await page.screenshot({ path: path.join(out, 'free-dark.png') });

  results.errors = errors;
  ok = ['free', 'drawer', 'dockRemembered', 'plan', 'dark'].every(k => results[k] === 'ok') && !errors.length;
} catch (e) {
  results.exception = String(e && e.stack || e);
  results.errors = errors;
} finally {
  if (browser) await browser.close();
  await cleanupAll();
}
console.log(JSON.stringify(results, null, 2));
console.log(ok ? 'FREE OK' : 'FREE FAILED');
process.exit(ok ? 0 : 1);
