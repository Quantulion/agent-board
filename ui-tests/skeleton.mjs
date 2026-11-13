// Каркас интерфейса в headless Chrome: настоящий сервер на временном каталоге с fake-агентом,
// скриншоты светлой и тёмной темы, живое обновление доски и проверка безопасного markdown.
// Запуск из корня репозитория: node ui-tests/skeleton.mjs
import { spawn, execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer';
import { python } from './python.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '..');
const out = path.join(here, 'out');
// Язык проекта задаётся явно: ожидания текстов ниже рассчитаны на него, а не на язык по умолчанию.
const LANG = 'ru';
const home = fs.mkdtempSync(path.join(os.tmpdir(), 'ab-ui-'));
const env = { ...process.env, AGENT_BOARD_HOME: home, PYTHONPATH: path.join(repo, 'board'), AGENT_BOARD_PROJECT: 'pay', AGENT_BOARD_ACTOR: 'user', AGENT_BOARD_LANG: LANG };
const cli = (...args) => execFileSync(python, ['-m', 'agent_board', ...args], { env, cwd: repo, encoding: 'utf-8', timeout: 20000 });

function withTimeout(promise, ms, what) {
  return Promise.race([promise, new Promise((_, rej) => setTimeout(() => rej(new Error('таймаут: ' + what)), ms))]);
}

async function startServer() {
  const port = 18000 + Math.floor(Math.random() * 1000);
  const proc = spawn(python, ['-m', 'agent_board', 'serve', '--port', String(port)], { env, cwd: repo });
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
cfg.harness = { type: 'fake' };
cfg.language = LANG;
fs.writeFileSync(cfgPath, JSON.stringify(cfg, null, 2));

const { proc, url } = await startServer();
let browser;
const results = {};
try {
  const tasks = [
    { key: 'PAY-101', title: 'Схема таблиц платежей в PG' },
    { key: 'PAY-102', title: 'Теневая запись в PG', deps: ['PAY-101'] },
    { key: 'PAY-103', title: 'Мигратор исторических данных', deps: ['PAY-101'] },
  ];
  for (const t of tasks) cli('add-task', '--json', JSON.stringify(t));

  browser = await puppeteer.launch({ headless: true, args: ['--no-sandbox'] });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  await page.setViewport({ width: 1280, height: 720 });
  // первое чтение снимка падает с 500: страница должна сама восстановиться без перезагрузки
  let failState = 1;
  let failedServed = 0;
  await page.setRequestInterception(true);
  page.on('request', req => {
    if (req.url().includes('/api/p/pay/state') && failState > 0) {
      failState--;
      failedServed++;
      req.respond({ status: 500, contentType: 'application/json', body: '{"error":"сбой сервера"}' });
    } else {
      req.continue();
    }
  });
  await page.evaluateOnNewDocument(() => {
    new MutationObserver(() => {
      if (document.querySelector('.sync.err') && document.querySelector('.banner-slot:not([hidden]) .notice.err')) window.__sawError = true;
    }).observe(document, { subtree: true, childList: true, attributes: true });
  });

  // вход по ссылке с токеном: токен уходит из адреса, hash сохраняется
  await page.goto(url + '#/p/pay', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.querySelectorAll('.card[data-key]').length === 3, { timeout: 10000 });
  await page.waitForFunction(() => document.querySelector('.banner-slot').hidden && !document.querySelector('.sync.err'), { timeout: 10000 });
  results.recoveredAfter500 = { failedServed, sawError: await page.evaluate(() => window.__sawError === true) };
  results.syncCursor = await page.$eval('.sync', el => getComputedStyle(el).cursor);
  results.addressAfterLogin = await page.evaluate(() => location.pathname + location.search + location.hash);
  await page.waitForFunction(() => document.querySelector('.sync').textContent === 'В сети', { timeout: 10000 });

  for (const theme of ['light', 'dark']) {
    await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: theme }]);
    await new Promise(r => setTimeout(r, 500));
    const file = path.join(out, `skeleton-board-${theme}.png`);
    await page.screenshot({ path: file });
    results[`board_${theme}`] = file;
  }

  // живое обновление: задача, добавленная извне, появляется без перезагрузки
  const firstRow = await page.$('.card[data-key="PAY-101"]');
  cli('add-task', '--json', JSON.stringify({ key: 'PAY-104', title: 'Переключение чтения на PG' }));
  cli('status', 'PAY-101', 'in_progress');
  await page.waitForFunction(() => document.querySelectorAll('.card[data-key]').length === 4, { timeout: 10000 });
  await page.waitForFunction(() => document.querySelector('.col[data-st="in_progress"] .card[data-key="PAY-101"]'), { timeout: 10000 });
  results.liveCards = await page.$$eval('.card[data-key]', els => els.length);
  results.rowNodeKept = await page.evaluate(el => el.isConnected, firstRow);

  // сбой чтения посреди работы: ошибка видна, данные остаются; следующий снимок её снимает
  failState = 1;
  cli('status', 'PAY-104', 'cancelled');
  await page.waitForFunction(() => document.querySelector('.sync.err'), { timeout: 10000 });
  results.laterFailure = await page.evaluate(() => ({
    banner: !document.querySelector('.banner-slot').hidden,
    rows: document.querySelectorAll('.card[data-key]').length,
  }));
  const errFile = path.join(out, 'skeleton-board-error-light.png');
  await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);
  await new Promise(r => setTimeout(r, 500));
  await page.screenshot({ path: errFile });
  results.board_error_light = errFile;
  cli('status', 'PAY-104', 'not_started');
  await page.waitForFunction(() => document.querySelector('.banner-slot').hidden && !document.querySelector('.sync.err'), { timeout: 10000 });
  results.laterFailure.recovered = true;

  // безопасный markdown: разметка из текста не исполняется
  results.markdown = await page.evaluate(async () => {
    const { safeMarkdown } = await import('/static/js/dom.js');
    const host = document.createElement('div');
    host.className = 'md';
    host.append(safeMarkdown('<img src=x onerror="window.__pwned=1"> и https://example.com\n```\n<script>window.__pwned=2</script>\n```'));
    document.body.append(host);
    await new Promise(r => setTimeout(r, 300));
    const found = { pwned: window.__pwned ?? null, img: host.querySelectorAll('img').length, script: host.querySelectorAll('script').length, rel: host.querySelector('a').rel };
    host.remove();
    return found;
  });

  // главный экран: доска проектов
  await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);
  await page.goto(url.replace(/\?.*$/, '') + '#/', { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.pcard[data-slug="pay"]', { timeout: 10000 });
  const projFile = path.join(out, 'skeleton-projects-light.png');
  await page.screenshot({ path: projFile });
  results.projects_light = projFile;

  // переключатель темы перекрывает системную
  await page.click('.app.home .row1 > .btn.icon:last-child');
  await page.click('.app.home .row1 > .btn.icon:last-child');
  results.themeAfterToggle = await page.evaluate(() => document.documentElement.dataset.theme);
  const projDark = path.join(out, 'skeleton-projects-dark.png');
  await page.screenshot({ path: projDark });
  results.projects_dark = projDark;

  results.pageErrors = errors.filter(e => !e.includes('status of 500'));

  // без куки: 401, ошибка на месте содержимого, поток не переподключается
  const anon = await browser.createBrowserContext();
  const anonPage = await anon.newPage();
  let streams = 0;
  anonPage.on('request', req => { if (req.url().includes('/stream')) streams++; });
  await anonPage.goto(url.replace(/\?.*$/, '') + '#/p/pay', { waitUntil: 'domcontentloaded' });
  await anonPage.waitForSelector('.banner-slot:not([hidden]) .notice.err', { timeout: 10000 });
  await new Promise(r => setTimeout(r, 2000));
  results.unauthorized = {
    title: await anonPage.$eval('.banner-slot h2', el => el.textContent),
    sync: await anonPage.$eval('.sync', el => el.className + ' / ' + el.textContent),
    streams,
  };
  await anon.close();
} finally {
  if (browser) await browser.close();
  proc.kill('SIGTERM');
  await withTimeout(new Promise(r => proc.once('exit', r)), 10000, 'остановка сервера').catch(() => proc.kill('SIGKILL'));
  fs.rmSync(home, { recursive: true, force: true });
}
console.log(JSON.stringify(results, null, 2));
const ok = results.markdown.pwned === null && results.markdown.img === 0 && results.markdown.script === 0
  && results.rowNodeKept && results.addressAfterLogin === '/#/p/pay' && results.pageErrors.length === 0
  && results.themeAfterToggle === 'dark'
  && results.recoveredAfter500.failedServed === 1 && results.recoveredAfter500.sawError
  && results.laterFailure.banner && results.laterFailure.rows === 4 && results.laterFailure.recovered
  && results.unauthorized.title === 'Нет доступа' && results.unauthorized.streams === 0
  && results.syncCursor === 'default';
process.exit(ok ? 0 : 1);
