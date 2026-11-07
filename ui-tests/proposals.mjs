// Предложения агента проекта о действиях пользователя в панели «Проект»: описание того, что изменится,
// применение set_config и set_status от имени пользователя, отклонение и ошибка устаревшего предложения у блока.
// Запуск из корня репозитория: node ui-tests/proposals.mjs
import path from 'node:path';
import puppeteer from 'puppeteer';
import { cleanupAll, createEnv, out } from './lib.mjs';

const VIEW = { width: 1440, height: 900 };

const env = createEnv({
  prefix: 'ab-proposals-', slug: 'pay', title: 'Платежи', language: 'ru',
  config: { repos: [{ name: 'payments-api', path: '/tmp/payments-api', base: 'master' }] },
});
const results = {};
let browser;
let ok = false;
const configGet = () => JSON.parse(env.cli('config', 'get', '--json'));
const propose = (kind, payload, reason) => env.post('proposals', { kind, payload, reason });

// Текст открытого предложения, в котором встречается needle: заголовок, строки изменений и причина.
const openText = (page, needle) => page.evaluate(n => {
  const block = [...document.querySelectorAll('.dock .popen .proposal')].find(p => p.textContent.includes(n));
  return block ? [...block.querySelectorAll('.pt, .chg > div, .why')].map(e => e.textContent) : null;
}, needle);

async function press(page, needle, button) {
  await page.evaluate((n, b) => [...document.querySelectorAll('.dock .popen .proposal')]
    .find(p => p.textContent.includes(n)).querySelector(b).click(), needle, button);
}

const openCount = page => page.$$eval('.dock .popen .proposal', els => els.length);

try {
  await env.start({ runner: false });
  env.cli('add-task', '--json', JSON.stringify({ key: 'PAY-1', title: 'Схема PostgreSQL', repo: 'payments-api' }));
  env.cli('stage', 'tracker');
  env.cli('status', 'PAY-1', 'in_review');
  browser = await puppeteer.launch({ headless: true, args: ['--no-sandbox'] });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !m.text().includes('400')) errors.push(m.text()); });
  await page.setViewport(VIEW);
  await page.goto(env.url.replace(/#.*$/, '') + '#/p/pay', { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.dock', { timeout: 10000 });

  await propose('set_config', { fields: { max_agents: 5 } }, 'Пользователь попросил больше параллельности');
  await propose('set_status', { key: 'PAY-1', to: 'approved', from: 'in_review' }, 'Пользователь попросил одобрить PAY-1');
  await propose('stage', { stage: 'work' }, 'Пользователь попросил начать работу');
  await page.waitForFunction(() => document.querySelectorAll('.dock .popen .proposal').length === 3, { timeout: 10000 });

  // описание: поля настроек со старым и новым значением, переход статуса, переход стадии и причина
  results.configText = await openText(page, 'max_agents');
  results.statusText = await openText(page, 'PAY-1');
  results.stageText = await openText(page, 'стадию');
  const expected = {
    configText: ['Изменить настройки проекта', '~ настройкаАгентов в проекте (max_agents): 3 → 5', 'Почему: Пользователь попросил больше параллельности'],
    statusText: ['Сменить статус задачи PAY-1 «Схема PostgreSQL»', '~ статусНа ревью → Одобрена', 'Почему: Пользователь попросил одобрить PAY-1'],
    stageText: ['Перевести проект на стадию «Работа»', '~ стадияТрекер → Работа', 'Почему: Пользователь попросил начать работу'],
  };
  const described = Object.keys(expected).every(k => JSON.stringify(results[k]) === JSON.stringify(expected[k]));
  const file = path.join(out, 'proposals-actions-light.png');
  await (await page.$('.dock')).screenshot({ path: file });
  results.screenshot = file;

  // применение set_config: настройки меняются, в истории — итог без слов о графе
  await press(page, 'max_agents', '.btn.ok');
  await page.waitForFunction(() => document.querySelectorAll('.dock .popen .proposal').length === 2, { timeout: 10000 });
  results.configApplied = configGet().max_agents === 5 ? 'ok' : configGet().max_agents;

  // применение set_status: задача одобрена от имени пользователя
  await press(page, 'PAY-1', '.btn.ok');
  await page.waitForFunction(() => document.querySelectorAll('.dock .popen .proposal').length === 1, { timeout: 10000 });
  results.statusApplied = (await env.task('PAY-1')).status === 'approved' ? 'ok' : (await env.task('PAY-1')).status;

  // отклонение: стадия не меняется
  await press(page, 'стадию', '.btn:not(.ok)');
  await page.waitForFunction(() => document.querySelectorAll('.dock .popen .proposal').length === 0, { timeout: 10000 });
  results.stageRejected = configGet().stage === 'tracker' ? 'ok' : configGet().stage;
  await page.$eval('.dock .phistory', el => { el.open = true; });
  results.history = await page.$$eval('.dock .phist .proposal .res', els => els.map(e => e.textContent).sort());

  // устаревшее предложение: настройку поменяли после него — ошибка видна у блока, кнопки снова активны
  await propose('set_config', { fields: { max_agents: 2 } }, 'Пользователь попросил меньше агентов');
  await page.waitForFunction(() => document.querySelectorAll('.dock .popen .proposal').length === 1, { timeout: 10000 });
  env.cli('config', 'set', '--json', JSON.stringify({ max_agents: 4 }));
  await press(page, 'max_agents', '.btn.ok');
  await page.waitForSelector('.dock .popen .proposal .ferr:not([hidden])', { timeout: 10000 });
  results.staleError = await page.$eval('.dock .popen .proposal .ferr', el => el.textContent);
  results.staleEnabled = await page.$eval('.dock .popen .proposal .btn.ok', el => !el.disabled);
  results.staleOpen = await openCount(page);
  results.staleConfig = configGet().max_agents;
  await page.$eval('.dock .popen .proposal', el => el.scrollIntoView({ block: 'center' }));
  const errFile = path.join(out, 'proposals-stale-light.png');
  await (await page.$('.dock')).screenshot({ path: errFile });
  results.errorScreenshot = errFile;

  results.errors = errors;
  ok = described && results.configApplied === 'ok' && results.statusApplied === 'ok' && results.stageRejected === 'ok'
    && JSON.stringify(results.history) === JSON.stringify(['Отклонено — ничего не изменилось', 'Применено', 'Применено'])
    && /^Не удалось применить: .*устарело.*max_agents/.test(results.staleError) && results.staleEnabled
    && results.staleOpen === 1 && results.staleConfig === 4 && !errors.length;
} catch (e) {
  results.exception = String(e && e.stack || e);
} finally {
  if (browser) await browser.close();
  await cleanupAll();
}
console.log(JSON.stringify(results, null, 2));
process.exit(ok ? 0 : 1);
