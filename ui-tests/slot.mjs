// Ожидание свободного слота: карточка на доске, открытая карточка и панель агента проекта показывают,
// какой лимит одновременных ходов упёрся, и подсказывают, где его поднять; на русском и английском.
// Запуск из корня репозитория: node ui-tests/slot.mjs
import fs from 'node:fs';
import path from 'node:path';
import puppeteer from 'puppeteer';
import { cleanupAll, createEnv, out, sleep } from './lib.mjs';

const VIEW = { width: 1440, height: 900 };
const results = {};
let browser;
let ok = false;

// Проект на стадии работы: одна задача ждёт слот из-за лимита проекта, агент проекта — из-за общего лимита.
async function waitingProject(language) {
  const env = createEnv({ prefix: 'ab-slot-', slug: 'pay', title: 'Слоты', language, config: { max_agents: 2, global_max_agents: 6 } });
  const metaPath = path.join(env.home, 'projects', 'pay', 'meta.json');
  const meta = fs.existsSync(metaPath) ? JSON.parse(fs.readFileSync(metaPath, 'utf-8')) : {};
  fs.writeFileSync(metaPath, JSON.stringify({ ...meta, project_agent_state: 'waiting_turn', project_wait_reason: 'global' }));
  // без раннера состояние ожидания никто не сбросит
  await env.start({ runner: false });
  env.cli('stage', 'tracker');
  env.cli('stage', 'work');
  await env.post('tasks', { actor: 'user', task: { key: 'PAY-1', title: 'Схема таблиц', description: 'Создать таблицы.' } });
  await env.post('tasks/PAY-1/transition', { to: 'in_progress', actor: 'agent' });
  await env.post('tasks/PAY-1/update', { actor: 'system', fields: { agent_state: 'waiting_turn', wait_reason: 'project' } });
  return env;
}

async function scenario(page, language, texts) {
  const env = await waitingProject(language);
  const shot = name => page.screenshot({ path: path.join(out, `slot-${language}-${name}.png`) });
  const r = {};

  await page.goto(env.url.replace(/#.*$/, '') + '#/p/pay', { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.card[data-key="PAY-1"] .qline.turnwait', { timeout: 10000 });
  r.card = await page.$eval('.card[data-key="PAY-1"] .qline.turnwait', el => ({ text: el.textContent, title: el.title }));
  await (await page.$('.card[data-key="PAY-1"]')).screenshot({ path: path.join(out, `slot-${language}-card.png`) });
  await shot('board');

  await page.evaluate(() => { window.location.hash = '#/p/pay?task=PAY-1'; });
  await page.waitForSelector('.drawer[data-key="PAY-1"] .slot-note', { timeout: 10000 });
  r.drawer = await page.$eval('.drawer[data-key="PAY-1"] .slot-note', el => el.textContent);
  // карточка выезжает с анимацией
  await sleep(600);
  await shot('drawer');
  await page.evaluate(() => { window.location.hash = '#/p/pay'; });
  await page.waitForFunction(() => !document.querySelector('.drawer'), { timeout: 10000 });

  await page.evaluate(label => {
    if (document.querySelector('aside.dock[hidden]')) [...document.querySelectorAll('button')].find(b => b.textContent.startsWith(label)).click();
  }, texts.projectButton);
  await page.waitForSelector('aside.dock:not([hidden]) .slot-note', { timeout: 10000 });
  r.panel = await page.$eval('aside.dock .slot-note', el => el.textContent);
  r.panelState = await page.$eval('aside.dock .pstate', el => el.textContent);
  await shot('project');
  // страница уходит с доски раньше остановки сервера, чтобы не ловить ошибки переподключения
  await page.goto('about:blank');
  await env.cleanup();

  const failures = [];
  const expect = (cond, what) => { if (!cond) failures.push(what); };
  expect(r.card.text.includes(texts.cardChip), 'чип на карточке: ' + r.card.text);
  expect(r.card.title.includes('"max_agents": 3'), 'подсказка на карточке: ' + r.card.title);
  expect(r.drawer.includes(texts.title) && r.drawer.includes(texts.projectLimit) && r.drawer.includes('"max_agents": 3'),
    'плашка в открытой карточке: ' + r.drawer);
  expect(r.panel.includes(texts.title) && r.panel.includes(texts.globalLimit) && r.panel.includes('"global_max_agents": 7'),
    'плашка в панели проекта: ' + r.panel);
  expect(r.panelState.includes(texts.panelState), 'состояние агента проекта: ' + r.panelState);
  return { ...r, failures };
}

try {
  browser = await puppeteer.launch({ headless: true, args: ['--no-sandbox'] });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  await page.setViewport(VIEW);
  results.ru = await scenario(page, 'ru', {
    projectButton: 'Проект', cardChip: 'ждёт слот · лимит проекта', title: 'Ждёт свободный слот',
    projectLimit: 'лимит проекта', globalLimit: 'общий лимит', panelState: 'ждёт свободный слот',
  });
  results.en = await scenario(page, 'en', {
    projectButton: 'Project', cardChip: 'waiting for a slot · project limit', title: 'Waiting for a free slot',
    projectLimit: 'project limit', globalLimit: 'server limit', panelState: 'waiting for a free slot',
  });
  results.errors = errors;
  ok = !errors.length && !results.ru.failures.length && !results.en.failures.length;
} catch (e) {
  results.exception = String(e && e.stack || e);
} finally {
  if (browser) await browser.close();
  await cleanupAll();
}
console.log(JSON.stringify(results, null, 2));
console.log(ok ? 'SLOT OK' : 'SLOT FAILED');
process.exit(ok ? 0 : 1);
