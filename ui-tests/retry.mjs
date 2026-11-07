// Ошибка хода и кнопка «Повторить» в headless Chrome: настоящий сервер с раннером и fake-агентом.
// Агент задачи и агент проекта падают три раза подряд и уходят в error; кнопка видна в карточке задачи,
// на панели агента проекта и в «Ждут вас»; по нажатию кнопка неактивна, агент снова получает ход,
// а отказ сервера показывается рядом с кнопкой.
// Запуск: cd ui-tests && node retry.mjs
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import puppeteer from 'puppeteer';
import { cleanupAll, createEnv, out, script, until } from './lib.mjs';

const VIEW = { width: 1440, height: 900 };
const ERROR_TEXT = 'Тестовый сбой хода';
// ход длится пару секунд: за это время интерфейс успевает показать, что агент снова работает
const FAILING = script([{ sleep: 2 }, { fail: ERROR_TEXT }]);
const checks = [];
const check = (name, ok, detail) => {
  checks.push({ name, ok: Boolean(ok) });
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok || detail === undefined ? '' : ' → ' + JSON.stringify(detail)));
};
const waits = (page, fn, ms, ...args) => page.waitForFunction(fn, { timeout: ms }, ...args).then(() => true, () => false);

// Нажимает «Повторить» и сразу, в том же такте, читает доступность кнопки: запрос ещё не вернулся.
const clickRetry = (page, selector) => page.$eval(selector, btn => {
  btn.click();
  return { disabled: btn.disabled, text: btn.textContent };
});

async function main(browser) {
  const env = createEnv({ prefix: 'ab-retry-', slug: 'pay', title: 'Перенос платежей на PostgreSQL', language: 'ru' });
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

  const errors = [];
  await env.start();
  env.cli('add-task', '--json', JSON.stringify({ key: 'PAY-1', title: 'Теневая запись платежей', repo: 'payments-api', description: FAILING }));
  env.cli('stage', 'tracker');
  env.cli('stage', 'work');
  env.cli('say', '_project', '--text', FAILING);
  // три попытки с паузами 5 и 30 секунд
  await until(async () => (await env.task('PAY-1')).agent_state === 'error', 90000, 'PAY-1 в error');
  await until(async () => (await env.state()).project_agent.state === 'error', 90000, 'агент проекта в error');

  const page = await browser.newPage();
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  await page.setViewport(VIEW);
  await page.goto(env.url + '#/p/pay', { waitUntil: 'domcontentloaded' });

  const attnTask = '.attn .attn-retry button[data-retry="PAY-1"]';
  const attnProject = '.attn .attn-retry button[data-retry="_project"]';
  const dockProject = '.dock .agent-status button[data-retry="_project"]';
  check('«Ждут вас»: кнопка у ошибки агента задачи', await waits(page, s => document.querySelector(s), 10000, attnTask));
  check('«Ждут вас»: кнопка у ошибки агента проекта', await waits(page, s => document.querySelector(s), 10000, attnProject));
  check('панель проекта: кнопка у ошибки хода', await waits(page, s => document.querySelector(s), 10000, dockProject));
  check('подпись кнопки — «Повторить»', (await page.$eval(dockProject, b => b.textContent)) === 'Повторить');

  await page.goto(env.url + '#/p/pay?task=PAY-1', { waitUntil: 'domcontentloaded' });
  const cardRetry = '.drawer[data-key="PAY-1"] .sec.err button[data-retry="PAY-1"]';
  check('карточка: секция ошибки с текстом и кнопкой', await waits(page, (s, text) => {
    const btn = document.querySelector(s);
    return btn && btn.closest('.sec.err').textContent.includes(text);
  }, 10000, cardRetry, ERROR_TEXT));
  await page.screenshot({ path: path.join(out, 'retry-card-error.png') });

  // отказ сервера: ошибка запроса видна рядом с кнопкой, кнопка снова активна
  await page.setRequestInterception(true);
  const refuse = req => {
    if (req.method() === 'POST' && req.url().endsWith('/retry')) {
      req.respond({ status: 400, contentType: 'application/json', body: JSON.stringify({ error: 'Сервер отказал в повторе' }) });
    } else {
      req.continue();
    }
  };
  page.on('request', refuse);
  await clickRetry(page, cardRetry);
  check('карточка: ошибка запроса рядом с кнопкой', await waits(page, s => {
    const err = document.querySelector(s).parentNode.querySelector('.retry-err');
    return err && !err.hidden && err.textContent === 'Сервер отказал в повторе';
  }, 5000, cardRetry));
  check('карточка: после ошибки кнопка снова активна', await page.$eval(cardRetry, b => !b.disabled && b.textContent === 'Повторить'));
  page.off('request', refuse);
  await page.setRequestInterception(false);

  // повтор агента задачи из карточки
  const since = (await env.api('GET', 'events?since=0')).events.length;
  const pressed = await clickRetry(page, cardRetry);
  check('карточка: пока идёт запрос, кнопка неактивна', pressed.disabled && pressed.text === 'Запуск…', pressed);
  check('карточка: агент задачи снова в ходе', await waits(page, () => {
    const card = document.querySelector('.card[data-key="PAY-1"]');
    return card && card.querySelector('.ag .live');
  }, 15000));
  check('карточка: секция ошибки ушла вместе с кнопкой', await waits(page, s => !document.querySelector(s), 15000, cardRetry));
  const events = (await env.api('GET', 'events?since=0')).events.slice(since);
  check('журнал: agent_wake retry с текстом ошибки и новый ход PAY-1',
    events.some(e => e.type === 'agent_wake' && e.data.key === 'PAY-1' && e.data.reason === 'retry' && e.data.error.includes(ERROR_TEXT))
    && events.some(e => e.type === 'turn_started' && e.data.key === 'PAY-1'), events.map(e => e.type));

  // повтор агента проекта с панели
  const pressedProject = await clickRetry(page, dockProject);
  check('панель проекта: пока идёт запрос, кнопка неактивна', pressedProject.disabled, pressedProject);
  check('панель проекта: агент снова работает', await waits(page, () => {
    const st = document.querySelector('.dock .pstate');
    return st && st.querySelector('.sdot.run');
  }, 15000));
  check('панель проекта: кнопка ушла', await waits(page, s => !document.querySelector(s), 15000, dockProject));
  await page.screenshot({ path: path.join(out, 'retry-project-running.png') });

  // отказ, подставленный тестом, браузер пишет в консоль как ошибку загрузки — её не считаем
  const unexpected = errors.filter(e => !e.includes('status of 400'));
  check('нет ошибок страницы', unexpected.length === 0, unexpected);
}

let browser;
try {
  browser = await puppeteer.launch({ headless: true, args: ['--no-sandbox'] });
  await main(browser);
} catch (e) {
  check('сценарий выполнен без исключений', false, String(e && e.stack || e));
} finally {
  if (browser) await browser.close();
  await cleanupAll();
}
const failed = checks.filter(c => !c.ok);
console.log(failed.length ? `FAIL: ${failed.length} из ${checks.length}` : `OK: ${checks.length} проверок`);
process.exit(failed.length ? 1 : 0);
