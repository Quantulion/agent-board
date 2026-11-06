// Настройки проекта: окно открывается из шапки, простое и составное поля сохраняются и видны в `config get`,
// ошибка проверки на сервере показывается у поля, а введённое остаётся в форме и ничего не записывается.
// Запуск из корня репозитория: node ui-tests/settings.mjs
import path from 'node:path';
import puppeteer from 'puppeteer';
import { cleanupAll, createEnv, out } from './lib.mjs';

const VIEW = { width: 1440, height: 900 };
const LINKS = [{ title: 'Spec', href: 'https://example.com/spec' }];

const env = createEnv({ prefix: 'ab-settings-', slug: 'pay', title: 'Платежи' });
const results = {};
let browser;
let ok = false;
const configGet = () => JSON.parse(env.cli('config', 'get', '--json'));

// Заменяет значение поля и сообщает форме о вводе.
async function typeInto(page, selector, text) {
  await page.focus(selector);
  await page.evaluate((sel, value) => {
    const el = document.querySelector(sel);
    el.value = value;
    el.dispatchEvent(new Event('input', { bubbles: true }));
  }, selector, text);
}

const fieldError = (page, name) => page.evaluate(n => {
  const err = document.querySelector('.settings [data-field="' + n + '"] .ferr');
  return err && !err.hidden ? err.textContent : null;
}, name);

async function openSettings(page) {
  await page.click('[data-action="settings"]');
  await page.waitForSelector('.modal.settingsmodal #st-max_agents', { timeout: 10000 });
}

const saveButton = page => page.evaluateHandle(() => document.querySelector('.modal.settingsmodal footer .btn.primary'));

try {
  await env.start({ runner: false });
  browser = await puppeteer.launch({ headless: true, args: ['--no-sandbox'] });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  await page.setViewport(VIEW);
  await page.goto(env.url.replace(/#.*$/, '') + '#/p/pay', { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('[data-action="settings"]', { timeout: 10000 });

  // окно: все поля схемы, slug и стадия только для чтения
  await openSettings(page);
  const form = await page.evaluate(() => ({
    fields: [...document.querySelectorAll('.settings .field[data-field]')].map(f => f.dataset.field),
    slug: document.querySelector('.settings [data-field="slug"]').textContent,
    slugEditable: Boolean(document.querySelector('#st-slug, #st-stage')),
    maxAgents: document.querySelector('#st-max_agents').value,
    reposIsJson: document.querySelector('#st-repos').tagName === 'TEXTAREA',
  }));
  const schemaNames = JSON.parse(env.cli('config', 'schema', '--json')).fields.map(f => f.name);
  results.form = form.slug === 'pay' && !form.slugEditable && form.maxAgents === '3' && form.reposIsJson
    && form.fields.length === schemaNames.length && schemaNames.every(n => form.fields.includes(n)) ? 'ok' : form;
  await page.screenshot({ path: path.join(out, 'settings-form.png') });

  // сохранение простого и составного поля
  await typeInto(page, '#st-max_agents', '5');
  await typeInto(page, '#st-links', JSON.stringify(LINKS, null, 2));
  await (await saveButton(page)).click();
  await page.waitForFunction(() => !document.querySelector('.modal.settingsmodal'), { timeout: 10000 });
  const saved = configGet();
  results.saved = saved.max_agents === 5 && JSON.stringify(saved.links) === JSON.stringify(LINKS) ? 'ok' : saved;

  // ошибка JSON ловится до отправки
  await openSettings(page);
  results.reopened = (await page.$eval('#st-max_agents', el => el.value)) === '5' ? 'ok' : 'stale value';
  await typeInto(page, '#st-tracker', '{"type": ');
  await (await saveButton(page)).click();
  const jsonError = await fieldError(page, 'tracker');
  results.jsonError = jsonError && /JSON/.test(jsonError) ? 'ok' : jsonError;

  // ошибка проверки на сервере: у поля, ввод не теряется, ничего не сохранено
  const badRepos = JSON.stringify([{ name: 'app', path: '/nonexistent/agent-board-repo' }]);
  await typeInto(page, '#st-tracker', '{"type": "none"}');
  await typeInto(page, '#st-title', 'Новое название');
  await typeInto(page, '#st-repos', badRepos);
  await (await saveButton(page)).click();
  await page.waitForFunction(() => {
    const err = document.querySelector('.settings [data-field="repos"] .ferr');
    return err && !err.hidden;
  }, { timeout: 10000 });
  const after = await page.evaluate(() => ({
    open: Boolean(document.querySelector('.modal.settingsmodal')),
    title: document.querySelector('#st-title').value,
    repos: document.querySelector('#st-repos').value,
    invalid: document.querySelector('#st-repos').getAttribute('aria-invalid'),
  }));
  await page.screenshot({ path: path.join(out, 'settings-error.png') });
  const unchanged = configGet();
  results.serverError = after.open && after.title === 'Новое название' && after.repos === badRepos && after.invalid === 'true'
    && unchanged.title === 'Платежи' && unchanged.repos.length === 0 ? 'ok' : { after, unchanged };
  results.serverErrorText = await fieldError(page, 'repos');

  results.errors = errors;
  ok = ['form', 'saved', 'reopened', 'jsonError', 'serverError'].every(k => results[k] === 'ok') && !errors.length;
} catch (e) {
  results.exception = String(e && e.stack || e);
} finally {
  if (browser) await browser.close();
  await cleanupAll();
}
console.log(JSON.stringify(results, null, 2));
console.log(ok ? 'SETTINGS OK' : 'SETTINGS FAILED');
process.exit(ok ? 0 : 1);
