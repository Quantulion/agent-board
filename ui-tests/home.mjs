// Главный экран — доска проектов: проект с вопросом агента в «Ждут вас», пункт общей полосы ведёт на карточку
// задачи, проект переезжает между колонками по живым событиям, «В архив» переносит его в свёрнутый «Архив»,
// а пункты архивного проекта остаются в общей полосе с меткой на колонке. Окно «Новый проект» создаёт проект без
// плана и открывает его доску, агенты при этом не стартуют. Тёмная тема — без ошибок консоли.
// Запуск из корня репозитория: node ui-tests/home.mjs
import fs from 'node:fs';
import path from 'node:path';
import puppeteer from 'puppeteer';
import { cleanupAll, createEnv, out, sleep, until } from './lib.mjs';

const VIEW = { width: 1440, height: 900 };

const env = createEnv({ prefix: 'ab-home-', slug: 'pay', title: 'Перенос платежей', language: 'ru' });
const results = {};
let browser;
let ok = false;
const errors = [];

// второй проект — «Без плана», на той же доске
env.cli('init', 'fix', '--kind', 'free', '--title', 'Мелкие доработки');
const fixCfg = path.join(env.home, 'projects', 'fix', 'project.json');
fs.writeFileSync(fixCfg, JSON.stringify({ ...JSON.parse(fs.readFileSync(fixCfg, 'utf-8')), language: 'ru', harness: { type: 'fake' } }, null, 2));

async function call(slug, p, body) {
  const resp = await fetch(`${env.origin}/api/p/${slug}/${p}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Board-Token': env.token }, body: JSON.stringify(body),
  });
  const data = await resp.json();
  if (!resp.ok) throw new Error(p + ': ' + data.error);
  return data;
}

const columnOf = (page, slug) => page.evaluate(s => {
  const card = document.querySelector('.pcard[data-slug="' + s + '"]');
  return card ? card.closest('.col').dataset.col : null;
}, slug);
const waitColumn = (page, slug, col, what) =>
  until(async () => (await columnOf(page, slug)) === col, 10000, what);

try {
  await env.start({ runner: false });
  env.cli('add-task', '--json', JSON.stringify({ key: 'PAY-1', title: 'Теневая запись', repo: '' }));
  await call('pay', 'tasks/PAY-1/questions', { question: { text: 'Какой допуск?', options: ['0', '1%'] } });
  await call('fix', 'tasks', { actor: 'user', task: { key: 'FIX-1', title: 'Поднять таймаут', repo: '' } });
  await call('fix', 'tasks/FIX-1/update', { actor: 'system', fields: { agent_state: 'running' } });

  browser = await puppeteer.launch({ headless: true, args: ['--no-sandbox'] });
  const page = await browser.newPage();
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);
  await page.setViewport(VIEW);
  await page.goto(env.url, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.pcard[data-slug="pay"]', { timeout: 10000 });

  // раскладка: вопрос агента — «Ждут вас», работающий агент — «Агенты работают», архив свёрнут
  const layout = {
    pay: await columnOf(page, 'pay'),
    fix: await columnOf(page, 'fix'),
    archiveCollapsed: await page.$eval('.col[data-col="archive"]', el => el.classList.contains('collapsed')),
    summary: await page.$eval('.row1 .sub', el => el.textContent),
    fixKind: await page.$eval('.pcard[data-slug="fix"] .kind', el => el.textContent),
  };
  results.layout = layout.pay === 'waiting' && layout.fix === 'working' && layout.archiveCollapsed
    && layout.summary.startsWith('2 активных · 1 агент работает') && layout.fixKind === 'Без плана' ? 'ok' : layout;
  await page.screenshot({ path: path.join(out, 'home-light.png') });

  // живое обновление: агент закончил ход — проект уходит в «Тихо» без перезагрузки
  await call('fix', 'tasks/FIX-1/update', { actor: 'system', fields: { agent_state: 'idle' } });
  results.live = await waitColumn(page, 'fix', 'quiet', 'fix в «Тихо»').then(() => 'ok', e => String(e));

  // пункт общей полосы ведёт на карточку задачи в проекте
  const item = await page.$eval('.attn a.it', el => ({ href: el.getAttribute('href'), text: el.textContent }));
  await page.click('.attn a.it');
  await page.waitForFunction(() => window.location.hash === '#/p/pay?task=PAY-1', { timeout: 10000 });
  results.attnLink = item.href === '#/p/pay?task=PAY-1' && item.text.includes('PAY') ? 'ok' : item;
  await page.goto(env.url.replace(/[?#].*$/, '') + '#/', { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.pcard[data-slug="fix"]', { timeout: 10000 });

  // «В архив» из меню карточки
  await page.click('.pcard[data-slug="fix"] .more');
  await page.click('.stmenu [data-action="archive"]');
  await waitColumn(page, 'fix', 'archive', 'fix в архиве').catch(() => {});
  const fixArchived = JSON.parse(env.cli('--project', 'fix', 'config', 'get', '--json')).archived;
  results.archive = (await columnOf(page, 'fix')) === 'archive' && fixArchived === true ? 'ok' : { col: await columnOf(page, 'fix'), fixArchived };

  // архивный проект с вопросом: остаётся в архиве, пункт в полосе, метка на свёрнутой колонке
  await call('pay', 'config', { actor: 'user', fields: { archived: true } });
  await waitColumn(page, 'pay', 'archive', 'pay в архиве').catch(() => {});
  const archived = await page.evaluate(() => ({
    items: [...document.querySelectorAll('.attn a.it')].map(a => a.getAttribute('href')),
    flag: (f => (f && !f.hidden ? f.textContent : null))(document.querySelector('.col[data-col="archive"] .flag')),
  }));
  results.archivedAttention = (await columnOf(page, 'pay')) === 'archive' && archived.items.includes('#/p/pay?task=PAY-1')
    && archived.flag === 'ждут вас: 1' ? 'ok' : archived;
  await page.screenshot({ path: path.join(out, 'home-archive.png') });

  // «Вернуть из архива»: свёрнутую колонку разворачивают кликом
  await page.click('.col[data-col="archive"]');
  await page.click('.pcard[data-slug="fix"] .more');
  await page.click('.stmenu [data-action="unarchive"]');
  results.unarchive = await waitColumn(page, 'fix', 'quiet', 'fix вернулся').then(() => 'ok', e => String(e));

  // «Новый проект»: ключ из названия, подсказка про занятый ключ, создание открывает доску проекта
  await page.click('[data-action="new-project"]');
  await page.waitForSelector('.modal.newproject #np-title', { timeout: 5000 });
  await page.type('#np-title', 'Мелкие доработки биллинга');
  const fromTitle = await page.$eval('#np-key', el => el.value);
  await page.$eval('#np-key', el => { el.focus(); el.select(); });
  await page.type('#np-key', 'fix');
  const hint = await page.$eval('#np-key-hint', el => el.textContent);
  const planDisabled = await page.$eval('.ptype[data-kind="plan"]', el => el.disabled);
  await page.click('.np-adv summary');
  await sleep(300);
  await page.screenshot({ path: path.join(out, 'home-newproject.png') });
  await page.click('[data-action="create-project"]');
  await page.waitForFunction(() => window.location.hash === '#/p/fix2', { timeout: 10000 }).catch(() => {});
  await page.waitForFunction(() => (document.querySelector('.top h1') || {}).textContent === 'Мелкие доработки биллинга',
    { timeout: 10000 }).catch(() => {});
  const created = await page.evaluate(() => ({ hash: window.location.hash, h1: (document.querySelector('.top h1') || {}).textContent }));
  const state = await (await fetch(`${env.origin}/api/p/fix2/state`, { headers: { 'X-Board-Token': env.token } })).json();
  const pa = state.project_agent || {};
  results.newProject = fromTitle === 'MDB' && hint === 'FIX уже занят, проект получит FIX2.' && planDisabled
    && created.hash === '#/p/fix2' && created.h1 === 'Мелкие доработки биллинга'
    && state.config.kind === 'free' && state.config.stage === 'work' && state.config.auto_approve === false
    && state.tasks.length === 0 && pa.state === 'idle' && !pa.session_id
    ? 'ok' : { fromTitle, hint, planDisabled, created, state: state.config ? state.config.kind : state, pa };

  // тёмная тема
  await page.goto(env.url.replace(/[?#].*$/, '') + '#/', { waitUntil: 'domcontentloaded' });
  await page.evaluate(() => localStorage.setItem('ab-theme', 'dark'));
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.pcard[data-slug="pay"]', { timeout: 10000 });
  results.dark = await page.evaluate(() => document.documentElement.dataset.theme) === 'dark' ? 'ok' : 'no theme';
  await page.screenshot({ path: path.join(out, 'home-dark.png') });
  await page.click('[data-action="new-project"]');
  await page.waitForSelector('.modal.newproject', { timeout: 5000 });
  await sleep(300);
  await page.screenshot({ path: path.join(out, 'home-newproject-dark.png') });
  await page.keyboard.press('Escape');

  results.errors = errors;
  ok = ['layout', 'live', 'attnLink', 'archive', 'archivedAttention', 'unarchive', 'newProject', 'dark'].every(k => results[k] === 'ok') && !errors.length;
} catch (e) {
  results.exception = String(e && e.stack || e);
  results.errors = errors;
} finally {
  if (browser) await browser.close();
  await cleanupAll();
}
console.log(JSON.stringify(results, null, 2));
console.log(ok ? 'HOME OK' : 'HOME FAILED');
process.exit(ok ? 0 : 1);
