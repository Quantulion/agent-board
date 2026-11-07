// Галочка «Прервать текущую работу» в треде: настоящий сервер с раннером и fake-агентом.
// Длинный ход → галочка видна → сообщение с галочкой прерывает ход → индикатор показывает новый ход →
// агент отвечает, галочка пропадает; то же в треде проекта.
// Запуск из корня репозитория: node ui-tests/interrupt.mjs
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import puppeteer from 'puppeteer';
import { cleanupAll, createEnv, out, script, sleep, until } from './lib.mjs';

const VIEW = { width: 1440, height: 900 };
const results = {};
let browser;
let ok = false;

const env = createEnv({ prefix: 'ab-interrupt-', slug: 'pay', title: 'Прерывание хода', language: 'ru' });
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

const shot = name => page => page.screenshot({ path: path.join(out, `interrupt-${name}.png`) });
const events = async (type, key) => (await env.api('GET', 'events?since=0')).events
  .filter(e => e.type === type && (e.data.key === key || e.data.thread === key));
// секунды на часах хода в индикаторе; null — часов не видно
const clockSeconds = scope => {
  const el = document.querySelector(scope + ' .agent-status:not([hidden]) .elapsed');
  if (!el) return null;
  const m = el.textContent.match(/(?:(\d+)\D+)?(\d+)\s*(?:с|s)$/);
  return m ? Number(m[1] || 0) * 60 + Number(m[2]) : null;
};
const checkbox = scope => {
  const box = document.querySelector(scope + ' .composer .interrupt input');
  return { visible: Boolean(box) && !box.closest('label').hidden, checked: Boolean(box && box.checked) };
};

async function send(page, scope, text) {
  await page.click(scope + ' .composer textarea');
  await page.type(scope + ' .composer textarea', text);
  await page.click(scope + ' .composer .btn.primary');
}

// Длинный ход → сообщение с галочкой → ответ в новом ходе.
async function interruptScenario(page, scope, key, reply, name) {
  const r = {};
  r.idle = await page.evaluate(checkbox, scope);
  await send(page, scope, script([{ sleep: 120 }]));
  await page.waitForSelector(scope + ' .composer .interrupt:not([hidden])', { timeout: 15000 });
  // ход идёт достаточно долго, чтобы перезапуск был виден по сброшенным часам
  await page.waitForFunction(s => {
    const el = document.querySelector(s + ' .agent-status:not([hidden]) .elapsed');
    return el && /(^|\D)([3-9]|\d\d+)\s*(с|s)$/.test(el.textContent);
  }, { timeout: 20000 }, scope);
  r.clockBefore = await page.evaluate(clockSeconds, scope);
  await page.click(scope + ' .composer .interrupt input');
  r.checkedBeforeSend = await page.evaluate(checkbox, scope);
  await shot(name + '-checked')(page);
  await send(page, scope, script([{ sleep: 4 }, { cli: ['say', key, '--text', reply] }]));
  r.afterSend = await page.evaluate(checkbox, scope);
  await until(async () => (await events('turn_started', key)).length >= 2, 20000, 'новый ход ' + key);
  await page.waitForFunction(s => {
    const el = document.querySelector(s + ' .agent-status:not([hidden]) .elapsed');
    return el && /(^|\D)[0-2]\s*(с|s)$/.test(el.textContent);
  }, { timeout: 15000 }, scope);
  r.clockRestarted = await page.evaluate(clockSeconds, scope);
  await shot(name + '-restarted')(page);
  await page.waitForFunction((s, text) => [...document.querySelectorAll(s + ' .thread .msg')].some(m => m.textContent.includes(text)),
    { timeout: 20000 }, scope, reply);
  await page.waitForFunction(s => document.querySelector(s + ' .agent-status').hidden, { timeout: 10000 }, scope);
  r.afterReply = await page.evaluate(checkbox, scope);
  r.interrupted = (await events('turn_interrupted', key)).map(e => e.data.by);
  r.failed = (await events('turn_failed', key)).length;
  return r;
}

try {
  await env.start();
  env.cli('stage', 'tracker');
  env.cli('stage', 'work');
  await env.post('tasks', { actor: 'user', task: { key: 'PAY-1', title: 'Схема таблиц', repo: 'payments-api', description: 'Создать таблицы.' } });
  await env.post('tasks/PAY-1/transition', { to: 'in_progress', actor: 'agent' });
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
  results.label = await page.$eval(drawer + ' .composer .interrupt', el => el.textContent.trim());
  // первые два хода задачи — по описанию и длинный; прерванный считается вторым
  const taskTurnsBefore = (await events('turn_started', 'PAY-1')).length;
  results.task = await interruptScenario(page, drawer, 'PAY-1', 'Прервал и ответил', 'task');
  results.task.turns = (await events('turn_started', 'PAY-1')).length - taskTurnsBefore;

  await page.evaluate(() => { window.location.hash = '#/p/pay'; });
  await page.waitForFunction(() => !document.querySelector('.drawer'), { timeout: 10000 });
  await page.evaluate(() => {
    if (document.querySelector('aside.dock[hidden]')) [...document.querySelectorAll('button')].find(b => b.textContent.startsWith('Проект')).click();
  });
  const dock = 'aside.dock:not([hidden])';
  await page.waitForSelector(dock, { timeout: 10000 });
  results.project = await interruptScenario(page, dock, '_project', 'Агент проекта прервался и ответил', 'project');
  await page.goto('about:blank');

  const failures = [];
  const expect = (cond, what) => { if (!cond) failures.push(what); };
  expect(results.label === 'Прервать текущую работу', 'подпись галочки: ' + results.label);
  for (const [name, r] of [['задача', results.task], ['проект', results.project]]) {
    expect(!r.idle.visible, name + ': без хода галочки нет');
    expect(r.checkedBeforeSend.visible && r.checkedBeforeSend.checked, name + ': галочка видна и отмечается');
    expect(!r.afterSend.checked, name + ': после отправки галочка сброшена');
    expect(r.clockRestarted < r.clockBefore, name + ': часы хода начались заново: ' + r.clockBefore + ' → ' + r.clockRestarted);
    expect(!r.afterReply.visible, name + ': после ответа галочки нет');
    expect(r.interrupted.length === 1 && r.interrupted[0] === 'user', name + ': ход прерван пользователем: ' + r.interrupted);
    expect(r.failed === 0, name + ': прерывание не считается ошибкой хода');
  }
  expect(results.task.turns === 2, 'задача: длинный ход и новый ход с ответом: ' + results.task.turns);
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
console.log(ok ? 'INTERRUPT OK' : 'INTERRUPT FAILED');
process.exit(ok ? 0 : 1);
