// Интеграции в интерфейсе: заведение задач в поддельной Jira на стадии «Трекер», ревью задачи через поддельный crit
// в два раунда до одобрения, одобрение второй задачи кнопкой, массовая отправка цепочки командой проекта
// (команда пишет файл-маркер), смена статуса в поддельной Jira → карточка в «Готово», индикатор синхронизации,
// фильтр волны. Скриншоты светлой и тёмной темы: вкладка ревью, окно отправки, плашка трекера и индикатор.
// Запуск: cd ui-tests && node integrations.mjs
import { execFileSync, spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import puppeteer from 'puppeteer';
import { python } from './python.mjs';
import { cleanupAll, createEnv, out, repo, script, until, withTimeout } from './lib.mjs';

const VIEW = { width: 1440, height: 900 };
const fakeCrit = path.join(repo, 'board', 'tests', 'fakes', 'fake_crit.py');
const TOKEN_ENV = 'AB_UI_JIRA_TOKEN';
const TOKEN = 'ui-test-secret-' + Math.random().toString(36).slice(2);
const checks = [];
const check = (name, ok, detail) => {
  checks.push({ name, ok: Boolean(ok) });
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok || detail === undefined ? '' : ' → ' + JSON.stringify(detail)));
};
const shots = [];

const columnOf = (page, key) => page.$eval(`.card[data-key="${key}"]`, el => el.closest('.col').dataset.st).catch(() => null);
const waits = (page, fn, ms, ...args) => page.waitForFunction(fn, { timeout: ms }, ...args).then(() => true, () => false);
const inColumn = (page, key, st, ms = 20000) => waits(page, (k, s) => document.querySelector(`.col[data-st="${s}"] .card[data-key="${k}"]`), ms, key, st);

async function shotThemes(page, name, prepare) {
  for (const theme of ['light', 'dark']) {
    await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: theme }]);
    if (prepare) await prepare();
    await page.evaluate(() => document.querySelectorAll('.toast').forEach(t => t.remove()));
    await new Promise(r => setTimeout(r, 250));
    const file = path.join(out, `integrations-${name}-${theme}.png`);
    await page.screenshot({ path: file });
    shots.push(file);
  }
  await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);
}

// Поддельная Jira отдельным процессом: адрес — первой строкой, живёт, пока открыт stdin.
async function startJira() {
  const proc = spawn(python, [path.join(repo, 'ui-tests', 'fake_jira_server.py')], { stdio: ['pipe', 'pipe', 'inherit'] });
  const url = await withTimeout(new Promise((resolve, reject) => {
    let buf = '';
    proc.stdout.on('data', d => {
      buf += d;
      const line = buf.split('\n')[0];
      if (buf.includes('\n')) resolve(JSON.parse(line).url);
    });
    proc.on('exit', code => reject(new Error('поддельная Jira завершилась: ' + code)));
  }), 15000, 'старт поддельной Jira');
  const control = async (method, p, body) => {
    const resp = await fetch(url + p, { method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
    return resp.json();
  };
  const stop = async () => {
    if (proc.exitCode !== null || proc.signalCode !== null) return;
    const exited = new Promise(r => proc.once('exit', r));
    proc.stdin.end();
    await withTimeout(exited, 5000, 'остановка Jira').catch(() => proc.kill('SIGKILL'));
  };
  return { url, control, stop };
}

async function main(browser) {
  const jira = await startJira();
  process.env[TOKEN_ENV] = TOKEN;
  const env = createEnv({ prefix: 'ab-int-', slug: 'pay', title: 'Интеграции доски' });
  const errors = [];
  let page;
  const critBin = path.join(env.home, 'crit-bin');
  try {
    // репозиторий задач, поддельный crit и настройки интеграций
    const gitRepo = path.join(env.home, 'payments-api');
    const git = (...args) => execFileSync('git', args, { cwd: gitRepo, encoding: 'utf-8', timeout: 20000 });
    fs.mkdirSync(gitRepo);
    git('init', '-q', '-b', 'master');
    git('config', 'user.email', 'ui@test');
    git('config', 'user.name', 'ui');
    fs.writeFileSync(path.join(gitRepo, 'README.md'), '# payments\n');
    git('add', '.');
    git('commit', '-q', '-m', 'init');
    fs.mkdirSync(critBin);
    const crit = path.join(critBin, 'crit');
    fs.writeFileSync(crit, `#!/bin/sh\nFAKE_CRIT_REGISTRY=${critBin}/daemons.txt\nexport FAKE_CRIT_REGISTRY\nexec ${python} ${fakeCrit} "$@"\n`);
    fs.chmodSync(crit, 0o755);
    const marker = path.join(env.home, 'shipped.txt');
    const cfgPath = path.join(env.home, 'projects', 'pay', 'project.json');
    const cfg = JSON.parse(fs.readFileSync(cfgPath, 'utf-8'));
    fs.writeFileSync(cfgPath, JSON.stringify({
      ...cfg,
      repos: [{ name: 'payments-api', path: gitRepo, base: 'master' }],
      review: { tool: 'crit', binary: crit },
      ship: { command: 'echo {key} {base} >> ' + marker, timeout: 60 },
      tracker: { type: 'jira', url: jira.url, project: 'PROJ', issue_type: 'Task', auth: { type: 'token_env', env: TOKEN_ENV },
        statuses: { Done: 'done' }, poll_seconds: 1 },
    }, null, 2));

    await env.start();
    const toReview = key => script([{ write: { path: key + '.txt', text: key } }, { cli: ['status', key, 'in_review'] }]);
    env.cli('add-task', '--json', JSON.stringify({ key: 'PAY-1', title: 'Теневая запись в PG', repo: 'payments-api', description: toReview('PAY-1') }));
    env.cli('add-task', '--json', JSON.stringify({ key: 'PAY-2', title: 'Чтение из PG', repo: 'payments-api', deps: ['PAY-1'], description: toReview('PAY-2') }));
    env.cli('stage', 'tracker');

    page = await browser.newPage();
    page.on('pageerror', e => errors.push(String(e)));
    page.on('console', m => { if (m.type() === 'error' && !m.text().includes('ERR_CONNECTION_REFUSED')) errors.push(m.text()); });
    await page.setViewport(VIEW);
    await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);
    await page.goto(env.url + '#/p/pay', { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => document.querySelectorAll('.card[data-key]').length === 2, { timeout: 10000 });

    // --- стадия «Трекер»: проверка доступа и заведение задач ---
    await page.waitForSelector('.tstage:not([hidden])', { timeout: 10000 });
    const host = new URL(jira.url).host;
    const createText = await page.$eval('.tstage [data-tracker="create"]', el => el.textContent);
    check('кнопка заведения показывает хост трекера', createText.includes(host), createText);
    await shotThemes(page, 'tracker-stage');
    await page.click('.tstage [data-tracker="check"]');
    check('«Проверить доступ» сообщает, что доступ есть',
      await waits(page, h => [...document.querySelectorAll('.toast')].some(t => t.textContent.includes('Доступ к ' + h)), 10000, host));
    await page.click('.tstage [data-tracker="create"]');
    await page.waitForSelector('.modal [data-confirm-create]', { timeout: 5000 });
    const confirmText = await page.$eval('.modal [data-confirm-create]', el => el.textContent);
    check('подтверждение заведения: число задач и хост трекера', confirmText.includes('2 задачи') && confirmText.includes(host), confirmText);
    const beforeConfirm = await jira.control('GET', '/_control/state');
    check('до подтверждения в Jira ничего не заведено', Object.keys(beforeConfirm.issues).length === 0, beforeConfirm);
    await shotThemes(page, 'tracker-confirm');
    await page.click('.modal [data-ok]');
    await page.waitForSelector('.tresult:not([hidden]) .tblock', { timeout: 30000 });
    const result = await page.$eval('.tresult', el => el.textContent);
    check('результат заведения: обе задачи и связь', result.includes('Заведено: 2') && result.includes('Связей: 1') && result.includes('PROJ-1') && result.includes('PROJ-2'), result);
    check('ключ трекера на карточке доски — ссылка',
      await waits(page, () => document.querySelector('.card[data-key="PAY-1"] a.chip.trk')?.href.endsWith('/browse/PROJ-1'), 10000));
    await shotThemes(page, 'tracker-created');
    // итог заведения остаётся после ухода со страницы проекта и возврата на неё
    await page.evaluate(() => { window.location.hash = '#/'; });
    await page.waitForFunction(() => !document.querySelector('.tstage'), { timeout: 5000 });
    await page.evaluate(() => { window.location.hash = '#/p/pay'; });
    check('итог заведения виден после возврата на доску',
      await waits(page, () => document.querySelector('.tresult:not([hidden])')?.textContent.includes('PROJ-2'), 10000));
    const jiraState = await jira.control('GET', '/_control/state');
    check('в Jira заведены задачи и связь', Object.keys(jiraState.issues).length === 2 && jiraState.links.length === 1, jiraState);

    // переход к работе кнопкой шкалы стадий
    await page.click('.stage-go button');
    await page.waitForSelector('.modal .btn.primary', { timeout: 5000 });
    await page.click('.modal .btn.primary');
    check('проект перешёл к работе', await waits(page, () => document.querySelector('.tstage')?.hidden, 10000));
    check('индикатор синхронизации Jira в шапке',
      await waits(page, () => /^Jira · /.test(document.querySelector('.tsync-wrap:not([hidden]) .tsync')?.textContent || ''), 15000));

    // --- ревью PAY-1: два раунда ---
    check('PAY-1 дошла до ревью', await inColumn(page, 'PAY-1', 'in_review', 30000));
    await until(async () => (await env.task('PAY-1')).agent_state === 'idle', 15000, 'ход PAY-1 закончен');
    await page.click('.card[data-key="PAY-1"]');
    await page.waitForSelector('.drawer[data-key="PAY-1"] [data-action="review-start"]', { timeout: 10000 });
    const actions = await page.$$eval('.drawer .acts [data-action]', els => els.map(e => e.dataset.action));
    check('действия на ревью: начать, одобрить, вернуть', ['review-start', 'review-approve', 'review-return'].every(a => actions.includes(a)), actions);
    await page.click('.drawer [data-action="review-start"]');
    await page.waitForSelector('.drawer [data-action="review-open"]', { timeout: 20000 });
    const first = (await env.task('PAY-1')).review;
    check('раунд 1 открыт, «Начать ревью» скрыта', first.state === 'open' && first.round === 1
      && !(await page.$('.drawer [data-action="review-start"]')), first);
    check('чип раунда на доске', await waits(page, () => document.querySelector('.card[data-key="PAY-1"] .chip.rvw')?.textContent === 'р1 · ждёт вас', 10000));
    const tab = waitTarget(browser, first.url);
    await page.click('.drawer [data-action="review-open"]');
    check('«Открыть ревью» открывает страницу crit в новой вкладке', await tab);

    finish(env, first.session, [{ path: 'PAY-1.txt', line: 1, body: 'переименуй файл' }]);
    check('после комментариев агент правит, затем открывается раунд 2',
      await waits(page, () => document.querySelector('.card[data-key="PAY-1"] .chip.rvw')?.textContent === 'р2 · ждёт вас', 40000));
    await page.click('.tabs [data-tab="review"]');
    await page.waitForSelector('.drawer .rvpill.open', { timeout: 10000 });
    const history = await page.$$eval('.drawer .rounds .round', els => els.map(e => e.textContent));
    check('история раундов: раунд 1 с одним комментарием', history.length === 2 && history[0].includes('1 комментарий'), history);
    await shotThemes(page, 'review-tab');
    const second = (await env.task('PAY-1')).review;
    // в настоящем crit пользователь закрывает комментарии на странице; здесь — «Finish Review» с одобрением
    finish(env, second.session, [], true);
    check('раунд без комментариев одобряет PAY-1', await inColumn(page, 'PAY-1', 'approved', 30000));
    check('чип «одобрено»', await waits(page, () => document.querySelector('.card[data-key="PAY-1"] .chip.rvw')?.textContent === 'одобрено', 10000));
    await page.keyboard.press('Escape');
    await page.waitForFunction(() => !document.querySelector('.drawer'), { timeout: 5000 });
    await shotThemes(page, 'board-chips');

    // --- PAY-2: одобрение кнопкой ---
    check('PAY-2 стартовала от одобренной PAY-1 и дошла до ревью', await inColumn(page, 'PAY-2', 'in_review', 40000));
    await until(async () => (await env.task('PAY-2')).agent_state === 'idle', 15000, 'ход PAY-2 закончен');
    await page.evaluate(() => { window.location.hash = '#/p/pay?task=PAY-2'; });
    await page.waitForSelector('.drawer[data-key="PAY-2"] [data-action="review-approve"]', { timeout: 10000 });
    await page.click('.drawer [data-action="review-approve"]');
    await page.waitForSelector('.modal [data-ok]', { timeout: 5000 });
    await page.click('.modal [data-ok]');
    check('PAY-2 одобрена кнопкой', await inColumn(page, 'PAY-2', 'approved', 20000));
    const shipActions = await page.$$eval('.drawer .acts [data-action]', els => els.map(e => e.dataset.action));
    check('у одобренной — «Отправить в ревью» и «Вернуть в работу…»', shipActions.includes('ship') && shipActions.includes('review-return'), shipActions);
    await page.keyboard.press('Escape');

    // --- массовая отправка цепочки ---
    check('«Ждут вас»: одобрено к отправке: 2',
      await waits(page, () => [...document.querySelectorAll('.attn .it')].some(b => b.textContent.includes('одобрено к отправке: 2')), 10000));
    check('кнопка отправки над доской', await waits(page, () => document.querySelector('.shipbtn:not([hidden])')?.textContent.includes('2'), 5000));
    await page.evaluate(() => [...document.querySelectorAll('.attn .it')].find(b => b.textContent.includes('одобрено к отправке')).click());
    await page.waitForSelector('.shipmodal .shiprow', { timeout: 5000 });
    const rows = await page.$$eval('.shipmodal .shiprow', els => els.map(e => [e.dataset.key, e.querySelector('input').checked]));
    check('в окне обе задачи по порядку графа, отмечены', JSON.stringify(rows) === JSON.stringify([['PAY-1', true], ['PAY-2', true]]), rows);
    const checked = () => page.$$eval('.shipmodal .shiprow input', els => els.map(e => e.checked));
    await page.click('.shipmodal .shiprow[data-key="PAY-1"] input');
    const afterUncheck = await checked();
    await page.click('.shipmodal .shiprow[data-key="PAY-2"] input');
    const afterCheck = await checked();
    check('снятие зависимости снимает зависимую, выбор зависимой тянет зависимость',
      afterUncheck.join() === 'false,false' && afterCheck.join() === 'true,true', { afterUncheck, afterCheck });
    check('превью команды с плейсхолдерами', (await page.$$eval('.shipmodal .cmd .ph', els => els.map(e => e.textContent))).join() === '{key},{base}');
    const rowValues = await page.$$eval('.shipmodal .shiprow[data-key="PAY-2"] .vals dd', els => els.map(e => e.textContent));
    const pay1Branch = (await env.task('PAY-1')).branch;
    check('значения плейсхолдеров по строке: {base} PAY-2 — ветка PAY-1', rowValues.join() === 'PAY-2,' + pay1Branch, rowValues);
    await shotThemes(page, 'ship-modal');
    await page.click('.shipmodal [data-ok]');
    check('обе отправлены, результат по каждой', await waits(page, () => document.querySelectorAll('.shipmodal .shipst.done').length === 2, 30000));
    await shotThemes(page, 'ship-done');
    const shipped = fs.readFileSync(marker, 'utf-8').trim().split('\n');
    const branch1 = (await env.task('PAY-1')).branch;
    check('команда выполнена по порядку, база PAY-2 — ветка PAY-1', shipped.length === 2 && shipped[0] === 'PAY-1 master' && shipped[1] === 'PAY-2 ' + branch1, shipped);
    await page.keyboard.press('Escape');
    check('карточки в «Отправлена»', await inColumn(page, 'PAY-1', 'sent', 10000) && await inColumn(page, 'PAY-2', 'sent', 10000));

    // --- Jira: Done → «Готово» ---
    const key1 = (await env.task('PAY-1')).tracker_key;
    await jira.control('POST', '/_control/status', { key: key1, name: 'Done' });
    check('статус Done в Jira переводит PAY-1 в «Готово»', await inColumn(page, 'PAY-1', 'done', 30000));
    await page.click('.tsync');
    await page.waitForSelector('.tsync-wrap .pop:not([hidden])', { timeout: 5000 });
    const pop = await page.$eval('.tsync-wrap .pop', el => el.textContent);
    check('окно синхронизации: итог и статусы без сопоставления', pop.includes('успешно') && pop.includes('Open'), pop);
    await shotThemes(page, 'sync-popover');
    await page.click('.tsync-wrap .pop [data-retry]');
    check('«Повторить» запускает синхронизацию',
      await waits(page, () => [...document.querySelectorAll('.toast')].some(t => t.textContent === 'Синхронизировано'), 15000));
    await page.keyboard.press('Escape');

    // --- фильтр волны: сохраняется после перезагрузки ---
    const waves = await page.$$eval('select[data-filter="wave"] option', els => els.map(e => e.textContent));
    check('волны из графа', waves.join() === 'Все волны,Волна 1,Волна 2', waves);
    await page.select('select[data-filter="wave"]', '2');
    const visible = await page.$$eval('.card[data-key]', els => els.map(e => e.dataset.key));
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.card[data-key="PAY-2"]', { timeout: 10000 });
    const kept = await page.$eval('select[data-filter="wave"]', el => el.value);
    check('фильтр волны оставляет PAY-2 и переживает перезагрузку', visible.join() === 'PAY-2' && kept === '2' && !(await page.$('.card[data-key="PAY-1"]')), { visible, kept });
    await page.select('select[data-filter="wave"]', '');

    // секрет трекера не попадает в снимок и страницу
    const snapText = JSON.stringify(await env.state());
    const html = await page.content();
    check('токен трекера не в снимке и не на странице', !snapText.includes(TOKEN) && !html.includes(TOKEN));
    check('нет ошибок страницы', errors.length === 0, errors);
  } finally {
    if (page) await page.close().catch(() => {});
    try {
      execFileSync(python, ['-c', 'import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); '
        + 'from fakes import fake_crit; fake_crit.cleanup(Path(sys.argv[2]))', path.join(repo, 'board', 'tests'), critBin], { timeout: 20000 });
    } catch (e) {
      console.error('не удалось остановить демоны fake crit: ' + e.message);
    }
    await jira.stop();
  }
}

// Проект без инструмента ревью: подсказка и кнопки во вкладке «Ревью», возврат с обязательным комментарием,
// одобрение, отправка одной задачи с подтверждением и неудачная отправка (хвост вывода текстом, пункт «Ждут вас»).
async function noReviewTool(browser) {
  const env = createEnv({ prefix: 'ab-int2-', slug: 'pay', title: 'Без инструмента ревью' });
  const errors = [];
  const page = await browser.newPage();
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  try {
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
    fs.writeFileSync(cfgPath, JSON.stringify({
      ...cfg,
      repos: [{ name: 'payments-api', path: gitRepo, base: 'master' }],
      review: { tool: 'none' },
      ship: { command: 'echo "<b>boom</b>" {key}; exit 3', timeout: 60 },
    }, null, 2));
    await env.start();
    env.cli('add-task', '--json', JSON.stringify({ key: 'PAY-1', title: 'Без crit', repo: 'payments-api',
      description: script([{ write: { path: 'a.txt', text: 'a' } }, { cli: ['status', 'PAY-1', 'in_review'] }]) }));
    env.cli('stage', 'tracker');
    env.cli('stage', 'work');
    await until(async () => { const t = await env.task('PAY-1'); return t.status === 'in_review' && t.agent_state === 'idle'; }, 30000, 'PAY-1 на ревью');

    await page.setViewport(VIEW);
    await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);
    await page.goto(env.url + '#/p/pay?task=PAY-1', { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.drawer[data-key="PAY-1"]', { timeout: 10000 });
    await page.click('.tabs [data-tab="review"]');
    await page.waitForSelector('.drawer .rvnone', { timeout: 10000 });
    const none = await page.$eval('.drawer .rvnone', el => ({ text: el.textContent, buttons: [...el.querySelectorAll('button')].map(b => b.textContent) }));
    check('tool none: подсказка и кнопки во вкладке «Ревью»', none.text.includes('«Изменения»')
      && JSON.stringify(none.buttons) === JSON.stringify(['Открыть изменения', 'Вернуть в работу…', 'Одобрить']), none);
    check('tool none: «Начать ревью» нет', !(await page.$('.drawer [data-action="review-start"]')));
    await shotThemes(page, 'review-none');

    // возврат: пустой комментарий не уходит, окно остаётся с ошибкой
    const tabButton = text => page.evaluate(t => [...document.querySelectorAll('.drawer .rvnone button')].find(b => b.textContent === t).click(), text);
    await tabButton('Вернуть в работу…');
    await page.waitForSelector('.modal textarea', { timeout: 5000 });
    await page.click('.modal [data-ok]');
    const empty = await waits(page, () => document.querySelector('.modal .ferr:not([hidden])')?.textContent === 'Напишите, что исправить', 5000);
    check('пустой комментарий: окно открыто, ошибка в окне', empty && Boolean(await page.$('.modal textarea')) && (await env.task('PAY-1')).status === 'in_review');
    const comment = 'Добавь тест на таймаут ' + script([{ cli: ['status', 'PAY-1', 'in_review'] }]);
    await page.type('.modal textarea', comment);
    await page.click('.modal [data-ok]');
    check('с комментарием окно закрывается', await waits(page, () => !document.querySelector('.modal'), 10000));
    await page.click('.tabs [data-tab="thread"]');
    check('комментарий возврата в треде от пользователя',
      await waits(page, () => [...document.querySelectorAll('.drawer .thread .msg.user')].some(m => m.textContent.includes('Добавь тест на таймаут')), 15000));
    await until(async () => { const t = await env.task('PAY-1'); return t.status === 'in_review' && t.agent_state === 'idle'; }, 30000, 'PAY-1 снова на ревью');

    // одобрение кнопкой во вкладке и отправка одной задачи
    await page.click('.tabs [data-tab="review"]');
    await page.waitForFunction(() => [...document.querySelectorAll('.drawer .rvnone button')].some(b => b.textContent === 'Одобрить'), { timeout: 10000 });
    await tabButton('Одобрить');
    await page.waitForSelector('.modal [data-ok]', { timeout: 5000 });
    await page.click('.modal [data-ok]');
    await until(async () => (await env.task('PAY-1')).status === 'approved', 20000, 'PAY-1 одобрена');
    await page.waitForSelector('.drawer [data-action="ship"]:not([disabled])', { timeout: 10000 });
    await page.click('.drawer [data-action="ship"]');
    await page.waitForSelector('.modal .shipvals', { timeout: 5000 });
    const vals = await page.$$eval('.modal .shipvals dt, .modal .shipvals dd', els => els.map(e => e.textContent));
    check('подтверждение отправки показывает значения плейсхолдеров', vals.join() === '{key},PAY-1', vals);
    await shotThemes(page, 'ship-confirm');
    await page.click('.modal [data-ok]');

    // неудачная отправка: пункт «Ждут вас» и хвост вывода текстом в «Обзоре»
    check('«Ждут вас»: отправка не удалась',
      await waits(page, () => [...document.querySelectorAll('.attn .it')].some(b => b.textContent.includes('отправка не удалась')), 30000));
    await page.click('.tabs [data-tab="overview"]');
    await page.waitForSelector('.drawer .sec.ship .out', { timeout: 10000 });
    const tail = await page.$eval('.drawer .sec.ship', el => ({ text: el.querySelector('.out').textContent, b: el.querySelectorAll('.out b').length, all: el.textContent }));
    check('хвост вывода текстом, код выхода', tail.text.includes('<b>boom</b> PAY-1') && tail.b === 0 && tail.all.includes('код выхода 3'), tail);
    await shotThemes(page, 'ship-error');
    check('нет ошибок страницы (без ревью)', errors.length === 0, errors);
  } finally {
    await page.close().catch(() => {});
  }
}

// Пользователь нажал «Finish Review» в поддельном crit, оставив comments.
function finish(env, session, comments, approved = null) {
  const dir = path.join(env.home, 'projects', 'pay', 'crit', 'fake-crit');
  const file = path.join(dir, `finish-${session}.json`);
  fs.writeFileSync(file + '.tmp', JSON.stringify({ comments, approved }));
  fs.renameSync(file + '.tmp', file);
}

function waitTarget(browser, url) {
  return browser.waitForTarget(t => t.url().startsWith(url), { timeout: 10000 })
    .then(async t => {
      const p = await t.page().catch(() => null);
      if (p) await p.close().catch(() => {});
      return true;
    }, () => false);
}

let browser;
let failed = false;
try {
  browser = await puppeteer.launch({ headless: true, args: ['--no-sandbox'] });
  await main(browser);
  await noReviewTool(browser);
} catch (e) {
  failed = true;
  console.error(e);
} finally {
  if (browser) await browser.close();
  await cleanupAll();
}
console.log(JSON.stringify({ shots }, null, 2));
process.exit(failed || checks.some(c => !c.ok) ? 1 : 0);
