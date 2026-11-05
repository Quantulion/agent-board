// Каркас интерфейса в headless Chrome: настоящий сервер на временном каталоге с fake-агентом,
// скриншоты светлой и тёмной темы, живое обновление шапки и проверка безопасного markdown.
// Запуск из корня репозитория: node ui-tests/skeleton.mjs
import { spawn, execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer';

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '..');
const out = path.join(here, 'out');
const python = path.join(repo, '.venv', 'bin', 'python');
const home = fs.mkdtempSync(path.join(os.tmpdir(), 'ab-ui-'));
const env = { ...process.env, AGENT_BOARD_HOME: home, PYTHONPATH: path.join(repo, 'board'), AGENT_BOARD_PROJECT: 'pay', AGENT_BOARD_ACTOR: 'user' };
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

  // вход по ссылке с токеном: токен уходит из адреса, hash сохраняется
  await page.goto(url + '#/p/pay', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.querySelectorAll('.tasklist .row[data-key]').length === 3, { timeout: 10000 });
  results.addressAfterLogin = await page.evaluate(() => location.pathname + location.search + location.hash);
  await page.waitForFunction(() => document.querySelector('.sync').textContent === 'В сети', { timeout: 10000 });

  for (const theme of ['light', 'dark']) {
    await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: theme }]);
    const file = path.join(out, `skeleton-board-${theme}.png`);
    await page.screenshot({ path: file });
    results[`board_${theme}`] = file;
  }

  // живое обновление: задача, добавленная извне, появляется без перезагрузки
  const firstRow = await page.$('.tasklist .row[data-key="PAY-101"]');
  cli('add-task', '--json', JSON.stringify({ key: 'PAY-104', title: 'Переключение чтения на PG' }));
  cli('status', 'PAY-101', 'in_progress');
  await page.waitForFunction(() => document.querySelector('.proj .sub').textContent.includes('4 задачи'), { timeout: 10000 });
  await page.waitForFunction(() => document.querySelector('.tasklist .row[data-key="PAY-101"] .spill').textContent === 'В работе', { timeout: 10000 });
  results.liveSub = await page.$eval('.proj .sub', el => el.textContent);
  results.rowNodeKept = await page.evaluate(el => el.isConnected, firstRow);

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

  // список проектов
  await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);
  await page.goto(url.replace(/\?.*$/, '') + '#/', { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.scr[data-key="pay"]', { timeout: 10000 });
  const projFile = path.join(out, 'skeleton-projects-light.png');
  await page.screenshot({ path: projFile });
  results.projects_light = projFile;

  // переключатель темы перекрывает системную
  await page.click('.hd .btn.icon');
  await page.click('.hd .btn.icon');
  results.themeAfterToggle = await page.evaluate(() => document.documentElement.dataset.theme);
  const projDark = path.join(out, 'skeleton-projects-dark.png');
  await page.screenshot({ path: projDark });
  results.projects_dark = projDark;

  results.pageErrors = errors;
} finally {
  if (browser) await browser.close();
  proc.kill('SIGTERM');
  await withTimeout(new Promise(r => proc.once('exit', r)), 10000, 'остановка сервера').catch(() => proc.kill('SIGKILL'));
  fs.rmSync(home, { recursive: true, force: true });
}
console.log(JSON.stringify(results, null, 2));
const ok = results.markdown.pwned === null && results.markdown.img === 0 && results.markdown.script === 0
  && results.rowNodeKept && results.addressAfterLogin === '/#/p/pay' && results.pageErrors.length === 0
  && results.themeAfterToggle === 'dark';
process.exit(ok ? 0 : 1);
