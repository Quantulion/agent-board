// Настройки доски: шестерёнка на главном экране открывает окно, сохранённые язык и лимит сразу меняют язык
// интерфейса и шапку, изменение из CLI приходит по общему потоку событий. В настройках проекта агент показан
// «как у доски», своё значение помечается и сбрасывается к значению доски. Окно — в светлой и тёмной темах.
// Запуск из корня репозитория: node ui-tests/boardsettings.mjs
import path from 'node:path';
import puppeteer from 'puppeteer';
import { cleanupAll, createEnv, out, until } from './lib.mjs';

const VIEW = { width: 1440, height: 900 };

const env = createEnv({ prefix: 'ab-boardsettings-', slug: 'pay', title: 'Платежи', language: 'ru' });
const results = {};
let browser;
let ok = false;
const errors = [];
const settingsGet = () => JSON.parse(env.cli('settings', 'get', '--json'));
const configGet = () => JSON.parse(env.cli('config', 'get', '--json'));

async function typeInto(page, selector, text) {
  await page.focus(selector);
  await page.evaluate((sel, value) => {
    const el = document.querySelector(sel);
    el.value = value;
    el.dispatchEvent(new Event(el.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }));
  }, selector, text);
}

const summary = page => page.$eval('.app.home .proj .sub', el => el.textContent);
const closed = (page, cls) => page.waitForFunction(c => !document.querySelector('.modal.' + c), { timeout: 10000 }, cls);

async function openBoardSettings(page) {
  await page.click('[data-action="board-settings"]');
  await page.waitForSelector('.modal.boardsettingsmodal #bs-global_max_agents', { timeout: 10000 });
}

async function openProjectSettings(page) {
  await page.click('[data-action="settings"]');
  await page.waitForSelector('.modal.settingsmodal #st-harness', { timeout: 10000 });
}

const harnessRow = page => page.evaluate(() => {
  const inh = document.querySelector('.settings [data-field="harness"] .inh');
  const reset = inh.querySelector('[data-action="reset-to-board"]');
  return { state: inh.dataset.state, badge: inh.querySelector('.inh-badge').textContent, reset: !reset.hidden };
});

try {
  await env.start({ runner: false });
  browser = await puppeteer.launch({ headless: true, args: ['--no-sandbox'] });
  const page = await browser.newPage();
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  await page.setViewport(VIEW);
  await page.goto(env.url.replace(/#.*$/, '') + '#/', { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('[data-action="board-settings"]', { timeout: 10000 });
  await until(async () => /из 6/.test(await summary(page)), 10000, 'лимит доски в шапке');

  // окно: текущие значения доски
  await openBoardSettings(page);
  const form = await page.evaluate(() => ({
    language: document.querySelector('#bs-language').value,
    limit: document.querySelector('#bs-global_max_agents').value,
    type: document.querySelector('#bs-harness-type [aria-pressed="true"]').dataset.v,
    tool: document.querySelector('#bs-review-tool').value,
  }));
  results.form = form.language === 'ru' && form.limit === '6' && form.type === 'fake' && form.tool === 'crit' ? 'ok' : form;
  await page.screenshot({ path: path.join(out, 'board-settings-light.png') });
  await page.evaluate(() => { document.documentElement.dataset.theme = 'dark'; });
  await page.screenshot({ path: path.join(out, 'board-settings-dark.png') });
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });

  // неверный лимит — ошибка у поля, ничего не сохранено
  await typeInto(page, '#bs-global_max_agents', '0');
  await page.click('[data-action="save-board-settings"]');
  const limitError = await page.evaluate(() => {
    const err = document.querySelector('.boardsettings [data-field="global_max_agents"] .ferr');
    return err && !err.hidden ? err.textContent : null;
  });
  results.limitError = limitError && settingsGet().global_max_agents === 6 ? 'ok' : limitError;

  // сохранение: язык и лимит доски сразу меняют интерфейс и шапку
  await typeInto(page, '#bs-global_max_agents', '9');
  await typeInto(page, '#bs-language', 'en');
  await typeInto(page, '#bs-model', 'm1');
  await page.click('[data-action="save-board-settings"]');
  await closed(page, 'boardsettingsmodal');
  const saved = settingsGet();
  results.saved = saved.language === 'en' && saved.global_max_agents === 9 && saved.harness.type === 'fake'
    && saved.harness.model === 'm1' ? 'ok' : saved;
  await until(async () => /of 9/.test(await summary(page).catch(() => '')), 10000, 'английский интерфейс и лимит 9');
  results.language = (await page.evaluate(() => document.documentElement.lang)) === 'en' ? 'ok' : 'not en';

  // изменение из CLI приходит по общему потоку событий
  env.cli('settings', 'set', '--json', JSON.stringify({ language: 'ru', global_max_agents: 4 }));
  await until(async () => /из 4/.test(await summary(page).catch(() => '')), 10000, 'настройки из CLI');
  results.stream = 'ok';
  await page.screenshot({ path: path.join(out, 'board-settings-home.png') });

  // настройки проекта: агент как у доски, своё значение и сброс к доске
  await page.goto(env.url.replace(/#.*$/, '') + '#/p/pay', { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('[data-action="settings"]', { timeout: 10000 });
  await openProjectSettings(page);
  const noLanguage = await page.evaluate(() => !document.querySelector('#st-language, #st-global_max_agents'));
  const inherited = await harnessRow(page);
  await typeInto(page, '#st-harness', JSON.stringify({ type: 'fake', model: 'own' }));
  const edited = await harnessRow(page);
  results.inherited = noLanguage && inherited.state === 'board' && inherited.badge === 'Как у доски' && !inherited.reset
    && edited.state === 'own' && edited.reset ? 'ok' : { noLanguage, inherited, edited };
  await page.click('.modal.settingsmodal footer .btn.primary');
  await closed(page, 'settingsmodal');
  const own = configGet();
  results.override = own.overrides.includes('harness') && own.harness.model === 'own' ? 'ok' : own;

  await openProjectSettings(page);
  const reopened = await harnessRow(page);
  await page.evaluate(() => document.querySelector('.settings [data-field="harness"] .inh').scrollIntoView({ block: 'center' }));
  await page.screenshot({ path: path.join(out, 'board-settings-project-own.png') });
  await page.click('.settings [data-field="harness"] [data-action="reset-to-board"]');
  const reset = await harnessRow(page);
  const resetValue = await page.$eval('#st-harness', el => JSON.parse(el.value));
  await page.click('.modal.settingsmodal footer .btn.primary');
  await closed(page, 'settingsmodal');
  const back = configGet();
  results.reset = reopened.state === 'own' && reopened.badge === 'Своё значение' && reopened.reset
    && reset.state === 'reset' && !reset.reset && resetValue.model === 'm1'
    && !back.overrides.includes('harness') && back.harness.model === 'm1' ? 'ok' : { reopened, reset, resetValue, back };

  // после сброса проект снова следует за доской
  env.cli('settings', 'set', '--json', JSON.stringify({ harness: { type: 'fake', model: 'm2' } }));
  results.follows = configGet().harness.model === 'm2' ? 'ok' : configGet().harness;

  results.errors = errors;
  ok = ['form', 'limitError', 'saved', 'language', 'stream', 'inherited', 'override', 'reset', 'follows']
    .every(k => results[k] === 'ok') && !errors.length;
} catch (e) {
  results.exception = String(e && e.stack || e);
  results.errors = errors;
} finally {
  if (browser) await browser.close();
  await cleanupAll();
}
console.log(JSON.stringify(results, null, 2));
console.log(ok ? 'BOARD SETTINGS OK' : 'BOARD SETTINGS FAILED');
process.exit(ok ? 0 : 1);
