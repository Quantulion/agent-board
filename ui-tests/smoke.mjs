// Сквозной смоук интерфейса — единственный скрипт, который гоняют перед релизом.
// Сценарий 1 (сервер с раннером и fake-агентом): колонки, «Ждут вас», ответ на вопрос, сообщение, вкладки без пересоздания панели,
// XSS из текста агента (Review Focus 1), черновик/фокус/прокрутка под потоком событий (3), запрещённый перенос (5),
// перезапуск сервера с переподключением без перезагрузки (2). Сценарий 2 (60 задач, 200 событий): нет долгих задач > 200 мс (4).
// Запуск: cd ui-tests && node smoke.mjs   (Node нужен только для разработки)
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import puppeteer from 'puppeteer';
import { createEnv, out, sleep, script, until, withTimeout } from './lib.mjs';

const VIEW = { width: 1440, height: 900 };
const checks = [];
const check = (name, ok, detail) => {
  checks.push({ name, ok: Boolean(ok), detail });
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok || detail === undefined ? '' : ' → ' + JSON.stringify(detail)));
};
const shots = {};
const XSS_TEXT = '<img src=x onerror="window.__xss=1"> <script>window.__xss=3</script> [клик](javascript:window.__xss=2)';

function attach(page, errors) {
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
}

async function drop(page, key, st) {
  await page.evaluate((k, s) => {
    const card = document.querySelector(`.card[data-key="${k}"]`);
    const col = document.querySelector(`.col[data-st="${s}"]`);
    const dt = new DataTransfer();
    card.dispatchEvent(new DragEvent('dragstart', { bubbles: true, dataTransfer: dt }));
    col.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer: dt }));
    col.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: dt }));
    card.dispatchEvent(new DragEvent('dragend', { bubbles: true, dataTransfer: dt }));
  }, key, st);
}
const columnOf = (page, key) => page.$eval(`.card[data-key="${key}"]`, el => el.closest('.col').dataset.st);
const shot = async (page, name) => {
  const file = path.join(out, `smoke-${name}.png`);
  await page.screenshot({ path: file });
  shots[name] = file;
};

// ---------- сценарий 1: основной ----------
async function main(browser) {
  const env = createEnv({ prefix: 'ab-smoke-', slug: 'pay', title: 'Перенос платежей на PostgreSQL' });
  const gitRepo = path.join(env.home, 'payments-api');
  const git = (...args) => execFileSync('git', args, { cwd: gitRepo, encoding: 'utf-8', timeout: 20000 });
  fs.mkdirSync(gitRepo);
  git('init', '-q', '-b', 'master');
  git('config', 'user.email', 'ui@test');
  git('config', 'user.name', 'ui');
  fs.writeFileSync(path.join(gitRepo, 'README.md'), '# payments\n');
  git('add', '.');
  git('commit', '-q', '-m', 'init');
  const cfgPath = path.join(env.home, 'projects', 'pay', 'project.json');
  const cfg = JSON.parse(fs.readFileSync(cfgPath, 'utf-8'));
  fs.writeFileSync(cfgPath, JSON.stringify({ ...cfg, repos: [{ name: 'payments-api', path: gitRepo, base: 'master' }] }, null, 2));

  const QUESTION = 'Что делать, если запись в PG не удалась после всех ретраев?';
  const steps = [
    { cli: ['update', 'PAY-1', '--summary', XSS_TEXT] },
    { cli: ['say', 'PAY-1', '--text', XSS_TEXT] },
    { cli: ['ask', 'PAY-1', '--text', QUESTION, '--option', 'Пропускать', '--option', 'Складывать в outbox', '--recommended', 'Складывать в outbox'] },
  ];
  await env.start();
  const errors = [];
  let page;
  try {
    env.cli('add-task', '--json', JSON.stringify({ key: 'PAY-1', title: 'Теневая запись платежей в PostgreSQL', repo: 'payments-api', estimate: 2, description: 'Дублировать запись в PG.\n\n' + script(steps) }));
    env.cli('add-task', '--json', JSON.stringify({ key: 'PAY-2', title: 'Переключить чтение на PG', repo: 'payments-api', estimate: 1, deps: ['PAY-1'] }));
    env.cli('stage', 'tracker');
    env.cli('stage', 'work');
    await until(async () => (await env.task('PAY-1')).open_questions === 1, 30000, 'вопрос PAY-1');

    page = await browser.newPage();
    attach(page, errors);
    await page.setViewport(VIEW);
    await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);
    await page.goto(env.url + '#/p/pay', { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => document.querySelectorAll('.card[data-key]').length === 2, { timeout: 10000 });
    await page.evaluate(() => { window.__noReload = 1; });

    // карточки в нужных колонках, «Ждут вас» показывает вопрос
    await page.waitForSelector('.attn .it.q', { timeout: 10000 });
    const col1 = await columnOf(page, 'PAY-1');
    check('карточка PAY-1 в «Ждёт ответа»', col1 === 'waiting_answer', col1);
    check('карточка PAY-2 в «Заблокирована»', (await columnOf(page, 'PAY-2')) === 'blocked');
    check('«Ждут вас» показывает вопрос', (await page.$eval('.attn .it.q', el => el.textContent)).includes('вопрос агента'));

    // Review Focus 1: текст агента в резюме на доске и в карточке — только текст
    await page.waitForFunction(() => document.querySelector('.card[data-key="PAY-1"]')?.textContent.includes('<img src=x'), { timeout: 10000 });
    await shot(page, 'board');
    await page.click('.attn .it.q');
    await page.waitForSelector('.drawer[data-key="PAY-1"] .question', { timeout: 10000 });
    await page.waitForFunction(() => document.querySelector('.drawer')?.textContent.includes('<img src=x'), { timeout: 10000 });
    await page.click('.tabs [data-tab="thread"]');
    await page.waitForFunction(() => [...document.querySelectorAll('.drawer .thread .msg')].some(m => m.textContent.includes('<img src=x')), { timeout: 10000 });
    const safe = await page.evaluate(() => ({
      img: document.querySelectorAll('img').length,
      script: document.querySelectorAll('.drawer script, .board script, .card script').length,
      jsLinks: document.querySelectorAll('a[href^="javascript" i]').length,
      flag: window.__xss,
    }));
    check('XSS: резюме и тред агента выводятся текстом, ничего не выполняется', !safe.img && !safe.script && !safe.jsLinks && safe.flag === undefined, safe);
    await page.click('.tabs [data-tab="overview"]');

    // ответ через карточку: вопрос закрыт, агент продолжил
    await page.type('.question textarea', script([{ cli: ['say', 'PAY-1', '--text', 'Продолжаю по вашему ответу'] }]));
    await page.click('.question .btn.primary');
    await page.waitForFunction(() => !document.querySelector('.drawer .question'), { timeout: 15000 });
    await until(async () => (await env.task('PAY-1')).open_questions === 0, 15000, 'вопрос закрыт');
    await page.click('.tabs [data-tab="thread"]');
    await page.waitForFunction(() => [...document.querySelectorAll('.drawer .thread .msg')].some(m => m.textContent.includes('Продолжаю по вашему ответу')), { timeout: 30000 });
    check('ответ закрыл вопрос, агент продолжил', true);
    check('вопрос ушёл из «Ждут вас»', await page.evaluate(() => !document.querySelector('.attn .it.q')));
    await shot(page, 'card');

    // сообщение из треда (Ctrl+Enter), панель не пересоздаётся при смене вкладок
    await page.evaluate(() => { window.__drawer = document.querySelector('.drawer'); window.__body = document.querySelector('.drawer .dbody'); });
    await page.click('.drawer .composer textarea');
    await page.type('.drawer .composer textarea', 'Спасибо, проверь функтесты');
    await page.keyboard.down('Control');
    await page.keyboard.press('Enter');
    await page.keyboard.up('Control');
    await page.waitForFunction(() => [...document.querySelectorAll('.drawer .thread .msg.user')].some(m => m.textContent.includes('Спасибо, проверь функтесты')), { timeout: 10000 });
    check('сообщение пользователя появилось в треде', true);
    for (const tab of ['changes', 'overview', 'thread']) await page.click(`.tabs [data-tab="${tab}"]`);
    check('вкладки не пересоздают панель', await page.evaluate(() => window.__drawer === document.querySelector('.drawer') && window.__drawer.isConnected && window.__body === document.querySelector('.drawer .dbody')));

    // Review Focus 3: черновик, фокус и прокрутка под потоком событий
    for (let i = 0; i < 30; i++) await env.post('threads/PAY-1/messages', { author: 'agent', text: 'Заметка ' + (i + 1) + ' — длинный текст, чтобы тред прокручивался. '.repeat(3) });
    await page.waitForFunction(() => [...document.querySelectorAll('.drawer .thread .msg')].some(m => m.textContent.includes('Заметка 30')), { timeout: 15000 });
    await page.click('.drawer .composer textarea');
    await page.type('.drawer .composer textarea', 'Черновик под потоком');
    await page.$eval('.drawer .dbody', el => { el.scrollTop = 150; });
    const scrollBefore = await page.$eval('.drawer .dbody', el => el.scrollTop);
    const burst = [];
    for (let i = 0; i < 60; i++) {
      burst.push(i % 3 === 0
        ? env.post('tasks/PAY-1/update', { actor: 'agent', fields: { summary: 'Событие ' + i } })
        : env.post('threads/PAY-1/messages', { author: 'agent', text: 'Событие ' + i }));
      if (i % 10 === 9) await Promise.all(burst.splice(0));
    }
    await Promise.all(burst);
    await page.waitForFunction(() => [...document.querySelectorAll('.drawer .thread .msg')].some(m => m.textContent.includes('Событие 59')), { timeout: 15000 });
    await page.waitForFunction(() => document.querySelector('.card[data-key="PAY-1"]')?.textContent.includes('Событие 57'), { timeout: 15000 });
    const after = await page.evaluate(() => ({
      focus: document.activeElement === document.querySelector('.drawer .composer textarea'),
      draft: document.querySelector('.drawer .composer textarea').value,
      scroll: document.querySelector('.drawer .dbody').scrollTop,
      same: window.__drawer === document.querySelector('.drawer'),
    }));
    check('черновик сохранён под потоком событий', after.draft === 'Черновик под потоком', after);
    check('фокус остался в поле ответа', after.focus && after.same, after);
    check('прокрутка треда не сбилась', Math.abs(after.scroll - scrollBefore) <= 3, { before: scrollBefore, after: after.scroll });
    await page.keyboard.press('Escape');
    await page.waitForFunction(() => !document.querySelector('.drawer'), { timeout: 5000 });

    check('нет ошибок страницы (сценарий 1)', errors.length === 0, errors);

    // Review Focus 5: запрещённый переход — тост с ошибкой сервера, карточка на месте
    const colBefore = await columnOf(page, 'PAY-1');
    await page.setRequestInterception(true);
    const forbid = req => {
      // сервер получает запрос на «blocked» и сам отвечает ошибкой
      if (req.method() === 'POST' && req.url().endsWith('/tasks/PAY-1/transition')) {
        req.continue({ postData: JSON.stringify({ to: 'blocked', actor: 'user' }) });
      } else req.continue();
    };
    page.on('request', forbid);
    await drop(page, 'PAY-1', 'approved');
    await page.waitForSelector('.toast.err', { timeout: 10000 });
    const toast = await page.$eval('.toast.err', el => ({ text: el.textContent, role: el.getAttribute('role') }));
    check('запрещённый переход: тост с ошибкой сервера', toast.role === 'alert' && toast.text.includes('blocked'), toast);
    check('запрещённый переход: карточка осталась на месте', (await columnOf(page, 'PAY-1')) === colBefore);
    page.off('request', forbid);
    await page.setRequestInterception(false);
    errors.length = 0; // 400 на запрещённом переходе — ожидаемый
    // «Заблокирована» не статус для переноса: открывается диалог внешнего блокера, карточка не двигается
    await drop(page, 'PAY-1', 'blocked');
    await page.waitForSelector('.modal input', { timeout: 10000 });
    await page.keyboard.press('Escape');
    check('перенос в «Заблокирована»: диалог, карточка на месте', (await columnOf(page, 'PAY-1')) === colBefore);

    // граф и разбивка
    await page.click('[data-view="graph"]');
    await page.waitForFunction(() => document.querySelectorAll('.graph-wrap g.node').length === 2, { timeout: 10000 });
    await shot(page, 'graph');
    await page.click('[data-view="board"]').catch(() => {});
    await page.goto(env.url + '#/p/pay/decomposition', { waitUntil: 'domcontentloaded' });
    await sleep(800);
    await shot(page, 'decomposition');
    await page.goto(env.url + '#/p/pay', { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => document.querySelectorAll('.card[data-key]').length === 2, { timeout: 10000 });
    await page.evaluate(() => { window.__noReload = 1; });

    // Review Focus 2: перезапуск сервера при открытой вкладке (ошибки сети при простое сервера ожидаемы)
    await env.stop();
    const down = await page.waitForFunction(() => {
      const t = document.querySelector('.sync')?.textContent || '';
      return /Переподключение|Ошибка загрузки/.test(t) ? t : false;
    }, { timeout: 20000 }).then(h => h.jsonValue()).catch(() => null);
    check('индикатор вышел из «В сети» при остановке сервера', down, down);
    // вкладка «офлайн» на время запуска сервера: изменение случается, пока она отключена
    await page.setOfflineMode(true);
    await env.start();
    env.cli('add-task', '--json', JSON.stringify({ key: 'PAY-3', title: 'Добавлена, пока вкладка была отключена', repo: 'payments-api' }));
    check('пока вкладка отключена, новой карточки нет', await page.evaluate(() => !document.querySelector('.card[data-key="PAY-3"]')));
    await page.setOfflineMode(false);
    await page.waitForFunction(() => /В сети/.test(document.querySelector('.sync')?.textContent || ''), { timeout: 40000 });
    check('индикатор вернулся в «В сети»', true);
    await page.waitForFunction(() => document.querySelector('.card[data-key="PAY-3"]'), { timeout: 15000 });
    check('пропущенная задача появилась без перезагрузки', await page.evaluate(() => window.__noReload === 1));
    env.cli('add-task', '--json', JSON.stringify({ key: 'PAY-4', title: 'Живое обновление после переподключения', repo: 'payments-api' }));
    await page.waitForFunction(() => document.querySelector('.card[data-key="PAY-4"]'), { timeout: 15000 });
    check('живое обновление после переподключения', await page.evaluate(() => window.__noReload === 1));
  } finally {
    if (page) await page.close();
    await env.cleanup();
  }
}

// ---------- сценарий 2: нагрузка ----------
async function load(browser) {
  const env = createEnv({ prefix: 'ab-load-', slug: 'load', title: 'Нагрузка', config: { max_agents: 1 } });
  await env.start({ runner: false });
  const errors = [];
  let page;
  try {
    for (let i = 1; i <= 60; i++) env.cli('add-task', '--json', JSON.stringify({ key: 'L-' + i, title: 'Задача нагрузочного прогона ' + i, estimate: 1 + (i % 3) }));
    page = await browser.newPage();
    attach(page, errors);
    await page.setViewport(VIEW);
    await page.evaluateOnNewDocument(() => {
      window.__long = [];
      try {
        new PerformanceObserver(l => { for (const e of l.getEntries()) window.__long.push(e.duration); }).observe({ type: 'longtask' });
      } catch (e) { window.__longUnsupported = true; }
    });
    await page.goto(env.url + '#/p/load', { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => document.querySelectorAll('.card[data-key]').length === 60, { timeout: 20000 });
    await sleep(1500);
    await page.evaluate(() => { window.__long.length = 0; });

    const started = Date.now();
    let sent = 0;
    for (let batch = 0; batch < 20; batch++) {
      const tick = Date.now();
      const reqs = [];
      for (let j = 0; j < 10; j++, sent++) {
        const key = 'L-' + ((sent % 60) + 1);
        reqs.push(sent % 2 === 0
          ? env.post(`threads/${key}/messages`, { author: 'agent', text: 'Сообщение ' + sent })
          : env.post(`tasks/${key}/update`, { actor: 'agent', fields: { summary: sent === 199 ? 'ФИНАЛ' : 'Резюме ' + sent } }));
      }
      await Promise.all(reqs);
      await sleep(Math.max(0, 250 - (Date.now() - tick)));
    }
    const elapsed = Date.now() - started;
    await page.waitForFunction(() => document.querySelector('.card[data-key="L-20"]')?.textContent.includes('ФИНАЛ'), { timeout: 15000 });
    const stat = await page.evaluate(() => ({ unsupported: Boolean(window.__longUnsupported), long: window.__long.slice(), cards: document.querySelectorAll('.card[data-key]').length }));
    const worst = Math.max(0, ...stat.long);
    check('нагрузка: 200 событий за 5 с доставлены', sent === 200 && elapsed < 7000, { sent, elapsed });
    check('нагрузка: все 60 карточек на месте', stat.cards === 60, stat);
    check('нагрузка: нет долгих задач > 200 мс', !stat.unsupported && worst <= 200, { worst, count: stat.long.length, unsupported: stat.unsupported });
    check('нет ошибок страницы (сценарий 2)', errors.length === 0, errors);
  } finally {
    if (page) await page.close();
    await env.cleanup();
  }
}

fs.mkdirSync(out, { recursive: true });
let browser;
let crashed = null;
try {
  browser = await puppeteer.launch({ headless: true, args: ['--no-sandbox'] });
  await withTimeout(main(browser), 240000, 'сценарий 1');
  await withTimeout(load(browser), 120000, 'сценарий 2');
} catch (e) {
  crashed = e;
  console.error('FAIL сценарий прерван: ' + (e && e.stack || e));
} finally {
  if (browser) await browser.close();
}
const failed = checks.filter(c => !c.ok);
console.log(JSON.stringify({ passed: checks.length - failed.length, failed: failed.length, shots }, null, 2));
process.exit(crashed || failed.length ? 1 : 0);
