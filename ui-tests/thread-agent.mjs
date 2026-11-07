// Индикатор агента внизу треда: настоящий сервер с раннером и fake-агентом.
// Сообщение в тред задачи → «агент работает» с часами хода → ответ агента, индикатор пропал;
// упавший ход показывает текст ошибки; то же «работает → ответ» в треде проекта.
// Запуск из корня репозитория: node ui-tests/thread-agent.mjs
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import puppeteer from 'puppeteer';
import { cleanupAll, createEnv, out, script, sleep, until } from './lib.mjs';

const VIEW = { width: 1440, height: 900 };
const results = {};
let browser;
let ok = false;

const env = createEnv({ prefix: 'ab-thread-agent-', slug: 'pay', title: 'Индикатор агента', language: 'ru' });
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

const shot = name => page => page.screenshot({ path: path.join(out, `thread-agent-${name}.png`) });
const indicator = scope => `${scope} .agent-status`;
// текст индикатора или null, если его не видно
const visibleIndicator = scope => {
  const el = document.querySelector(scope + ' .agent-status');
  return el && !el.hidden ? el.textContent : null;
};

async function send(page, scope, text) {
  await page.click(scope + ' .composer textarea');
  await page.type(scope + ' .composer textarea', text);
  await page.click(scope + ' .composer .btn.primary');
}

try {
  await env.start();
  env.cli('stage', 'tracker');
  env.cli('stage', 'work');
  await env.post('tasks', { actor: 'user', task: { key: 'PAY-1', title: 'Схема таблиц', repo: 'payments-api', description: 'Создать таблицы.' } });
  await env.post('tasks/PAY-1/transition', { to: 'in_progress', actor: 'agent' });
  // первый ход по описанию задачи завершается сразу
  await until(async () => { const t = await env.task('PAY-1'); return t.last_turn_at && t.agent_state === 'idle'; }, 30000, 'первый ход PAY-1');

  browser = await puppeteer.launch({ headless: true, args: ['--no-sandbox'] });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  await page.setViewport(VIEW);
  await page.goto(env.url.replace(/#.*$/, '') + '#/p/pay?task=PAY-1', { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.drawer[data-key="PAY-1"]', { timeout: 10000 });
  await page.click('.tabs [data-tab="thread"]');
  const drawer = '.drawer[data-key="PAY-1"]';
  results.idleHidden = await page.$eval(indicator(drawer), el => el.hidden);

  // сообщение → агент работает, часы хода идут → ответ, индикатор пропал
  await send(page, drawer, script([{ sleep: 4 }, { cli: ['say', 'PAY-1', '--text', 'Готово, таблицы созданы'] }]));
  await page.waitForSelector(drawer + ' .agent-status:not([hidden]) .agent-run .elapsed', { timeout: 15000 });
  results.taskRunning = await page.$eval(indicator(drawer), el => el.textContent);
  await sleep(1500);
  results.taskRunningLater = await page.$eval(indicator(drawer), el => el.textContent);
  await sleep(300);
  await shot('task-running')(page);
  await page.waitForFunction(d => [...document.querySelectorAll(d + ' .thread .msg')].some(m => m.textContent.includes('Готово, таблицы созданы')),
    { timeout: 20000 }, drawer);
  await page.waitForFunction(d => document.querySelector(d + ' .agent-status').hidden, { timeout: 10000 }, drawer);
  results.taskAfterReply = await page.evaluate(visibleIndicator, drawer);

  // упавший ход: текст ошибки под тредом
  await send(page, drawer, script([{ fail: 'Не удалось применить миграцию' }]));
  await page.waitForSelector(drawer + ' .agent-status:not([hidden]) .ferr', { timeout: 15000 });
  results.taskError = await page.$eval(indicator(drawer), el => el.textContent);
  results.taskRetry = await page.$eval(indicator(drawer), el => el.querySelector('button[data-retry="PAY-1"]')?.textContent || null);
  await shot('task-error')(page);

  // тред проекта: то же «работает → ответ»
  await page.evaluate(() => { window.location.hash = '#/p/pay'; });
  await page.waitForFunction(() => !document.querySelector('.drawer'), { timeout: 10000 });
  await page.evaluate(() => {
    if (document.querySelector('aside.dock[hidden]')) [...document.querySelectorAll('button')].find(b => b.textContent.startsWith('Проект')).click();
  });
  const dock = 'aside.dock:not([hidden])';
  await page.waitForSelector(dock, { timeout: 10000 });
  results.projectIdleHidden = await page.$eval(indicator(dock), el => el.hidden);
  await send(page, dock, script([{ sleep: 4 }, { cli: ['say', '_project', '--text', 'Ответ агента проекта'] }]));
  await page.waitForSelector(dock + ' .agent-status:not([hidden]) .agent-run .elapsed', { timeout: 15000 });
  results.projectRunning = await page.$eval(indicator(dock), el => el.textContent);
  await sleep(300);
  await shot('project-running')(page);
  await page.waitForFunction(d => [...document.querySelectorAll(d + ' .thread .msg')].some(m => m.textContent.includes('Ответ агента проекта')),
    { timeout: 20000 }, dock);
  await page.waitForFunction(d => document.querySelector(d + ' .agent-status').hidden, { timeout: 10000 }, dock);
  results.projectAfterReply = await page.evaluate(visibleIndicator, dock);
  // страница уходит с доски раньше остановки сервера, чтобы не ловить ошибки переподключения
  await page.goto('about:blank');

  const failures = [];
  const expect = (cond, what) => { if (!cond) failures.push(what); };
  const clock = /(\d+) с/;
  expect(results.idleHidden === true, 'простой агента без индикатора');
  expect(results.taskRunning.includes('Агент работает над ответом') && clock.test(results.taskRunning),
    'задача: работает с часами: ' + results.taskRunning);
  expect(results.taskRunningLater !== results.taskRunning, 'часы хода идут: ' + results.taskRunningLater);
  expect(results.taskAfterReply === null, 'индикатор задачи пропал после ответа');
  expect(results.taskError.includes('Ошибка хода') && results.taskError.includes('Не удалось применить миграцию'),
    'задача: ошибка хода: ' + results.taskError);
  expect(results.taskRetry === 'Повторить', 'задача: кнопка «Повторить» у ошибки хода: ' + results.taskRetry);
  expect(results.projectIdleHidden === true, 'простой агента проекта без индикатора');
  expect(results.projectRunning.includes('Агент работает над ответом') && clock.test(results.projectRunning),
    'проект: работает с часами: ' + results.projectRunning);
  expect(results.projectAfterReply === null, 'индикатор проекта пропал после ответа');
  results.failures = failures;
  results.errors = errors;
  ok = !errors.length && !failures.length;
} catch (e) {
  results.exception = String(e && e.stack || e);
} finally {
  if (browser) await browser.close();
  await cleanupAll();
}
console.log(JSON.stringify(results, null, 2));
console.log(ok ? 'THREAD AGENT OK' : 'THREAD AGENT FAILED');
process.exit(ok ? 0 : 1);
