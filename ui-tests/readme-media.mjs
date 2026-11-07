// Медиа для README: демо-проект на английском, запись сценария в кадры для GIF и статичный снимок доски.
// Всё живёт во временном каталоге: агенты поддельные (fake), репозитории без remote, команда отправки не задана.
// Запуск из корня репозитория: node ui-tests/readme-media.mjs
// Кадры: ui-tests/out/readme/frames, сборка GIF: ui-tests/frames_to_gif.py.
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import puppeteer from 'puppeteer';
import { cleanupAll, createEnv, out, script, until } from './lib.mjs';

const VIEW = { width: 1280, height: 800 };
const dir = path.join(out, 'readme');
const framesDir = path.join(dir, 'frames');
fs.rmSync(dir, { recursive: true, force: true });
fs.mkdirSync(framesDir, { recursive: true });

const env = createEnv({ prefix: 'ab-readme-', slug: 'pay', title: 'Payments → PostgreSQL', language: 'en' });

// Локальные репозитории без remote: пушить некуда.
function makeRepo(name, files) {
  const repo = path.join(env.home, 'repos', name);
  fs.mkdirSync(repo, { recursive: true });
  const git = (...args) => execFileSync('git', args, { cwd: repo, encoding: 'utf-8' });
  git('init', '-q', '-b', 'main');
  git('config', 'user.email', 'demo@example.com');
  git('config', 'user.name', 'demo');
  for (const [file, text] of Object.entries(files)) {
    fs.mkdirSync(path.dirname(path.join(repo, file)), { recursive: true });
    fs.writeFileSync(path.join(repo, file), text);
  }
  git('add', '.');
  git('commit', '-q', '-m', 'init');
  return { name, path: repo, base: 'main' };
}

const REPO_PY = `class PaymentRepository:
    def __init__(self, legacy_db):
        self.legacy_db = legacy_db

    def save(self, payment):
        self.legacy_db.insert("payments", payment.to_row())
        return payment.id
`;
const REPO_PY_NEW = `class PaymentRepository:
    def __init__(self, legacy_db, dual_writer=None):
        self.legacy_db = legacy_db
        self.dual_writer = dual_writer

    def save(self, payment):
        self.legacy_db.insert("payments", payment.to_row())
        if self.dual_writer:
            self.dual_writer.write(payment)
        return payment.id
`;
const DUAL_WRITER = `import logging

log = logging.getLogger(__name__)


class DualWriter:
    """Copies every payment to PostgreSQL without slowing down the main write."""

    def __init__(self, pg, retries=3):
        self.pg = pg
        self.retries = retries

    def write(self, payment):
        for attempt in range(1, self.retries + 1):
            try:
                return self.pg.upsert("payments", payment.to_row())
            except self.pg.Error:
                log.warning("pg write failed", extra={"paymentId": payment.id, "attempt": attempt})
        # TODO: what to do after the last retry — see the open question
`;
const TEST_PY = `def test_save__pg_down__payment_is_still_saved(legacy_db, broken_pg):
    repo = PaymentRepository(legacy_db, DualWriter(broken_pg))

    repo.save(make_payment())

    assert legacy_db.count("payments") == 1
`;

const QUESTION = 'What should happen when a PostgreSQL write still fails after all retries?';
const steps = [
  { write: { path: 'payments/repository.py', text: REPO_PY_NEW } },
  { write: { path: 'payments/dual_writer.py', text: DUAL_WRITER } },
  { write: { path: 'tests/test_dual_writer.py', text: TEST_PY } },
  { cli: ['say', 'PAY-5', '--text', 'Dual writes are wired into `PaymentRepository.save`. PostgreSQL errors are retried 3 times and never fail the payment.'] },
  { cli: ['update', 'PAY-5', '--summary', 'Dual writes are in place with retries; one decision left for the final failure.',
    '--todo-json', JSON.stringify([
      { text: 'Write to PostgreSQL next to the legacy store', done: true },
      { text: 'Retries with logging', done: true },
      { text: 'Handle writes that fail after all retries', done: false },
      { text: 'Functional tests', done: false },
    ]),
    '--changed-files-json', JSON.stringify(['payments/repository.py', 'payments/dual_writer.py', 'tests/test_dual_writer.py'])] },
  { cli: ['ask', 'PAY-5', '--text', QUESTION,
    '--option', 'Log and skip', '--option', 'Park it in an outbox and replay later', '--option', 'Fail the payment',
    '--recommended', 'Park it in an outbox and replay later',
    '--context', 'The legacy store stays the source of truth during the migration, so a failed copy must not block the payment.\n\nSkipping loses data for reconciliation; an outbox keeps it and needs a replay job.'] },
];
const DESC5 = 'Every payment saved to the legacy store is also written to PostgreSQL. PostgreSQL errors must never fail a payment.';

let browser;
let ok = false;
const results = {};
try {
  const api = makeRepo('payments-api', { 'payments/repository.py': REPO_PY, 'README.md': '# payments-api\n' });
  const worker = makeRepo('payments-worker', { 'README.md': '# payments-worker\n' });
  const reports = makeRepo('ledger-reports', { 'README.md': '# ledger-reports\n' });
  const cfgPath = path.join(env.home, 'projects', 'pay', 'project.json');
  const cfg = JSON.parse(fs.readFileSync(cfgPath, 'utf-8'));
  Object.assign(cfg, {
    max_agents: 4, repos: [api, worker, reports],
    links: [{ title: 'Migration plan', href: 'https://example.com/plan' }, { title: 'PG schema', href: 'docs/schema.md' }],
  });
  delete cfg.ship;
  fs.writeFileSync(cfgPath, JSON.stringify(cfg, null, 2));

  const tasks = [
    { key: 'PAY-1', title: 'Audit the current payments schema', repo: 'payments-api', kind: 'research', estimate: 1 },
    { key: 'PAY-2', title: 'PostgreSQL schema and migrations for payments', repo: 'payments-api', estimate: 2, deps: ['PAY-1'] },
    { key: 'PAY-3', title: 'Storage layer with a database switch', repo: 'payments-api', estimate: 3, deps: ['PAY-2'] },
    { key: 'PAY-4', title: 'Backfill historical payments in batches', repo: 'payments-worker', estimate: 3, deps: ['PAY-2'] },
    { key: 'PAY-5', title: 'Dual-write payments to PostgreSQL', repo: 'payments-api', estimate: 2, deps: ['PAY-2'],
      description: DESC5 + '\n\n' + script(steps),
      done_when: 'Both stores hold the same payments; PostgreSQL errors show up in metrics and never change the API response.' },
    { key: 'PAY-6', title: 'Consistency check between the two stores', repo: 'payments-worker', estimate: 2, deps: ['PAY-2'] },
    { key: 'PAY-7', title: 'Metrics and alerts for the new write path', repo: 'payments-api', estimate: 1, deps: ['PAY-2'] },
    { key: 'PAY-8', title: 'Revenue reports read from PostgreSQL', repo: 'ledger-reports', estimate: 2, deps: ['PAY-4', 'PAY-6'] },
    { key: 'PAY-9', title: 'Accounting export from PostgreSQL', repo: 'ledger-reports', estimate: 1, deps: ['PAY-8'] },
    { key: 'PAY-10', title: 'Load test on a production-size copy', repo: 'payments-api', kind: 'research', estimate: 1, deps: ['PAY-3'] },
    { key: 'PAY-11', title: 'Switch payment reads to PostgreSQL', repo: 'payments-api', estimate: 2, deps: ['PAY-5', 'PAY-6'] },
    { key: 'PAY-12', title: 'Retire the legacy write path', repo: 'payments-worker', estimate: 1, deps: ['PAY-11', 'PAY-9'] },
  ];

  // этап 1: раннер включён, работает только поддельный агент PAY-5 — пишет код, резюме и задаёт вопрос
  await env.start({ runner: true });
  for (const t of tasks.filter(t => ['PAY-1', 'PAY-2', 'PAY-5'].includes(t.key))) env.cli('add-task', '--json', JSON.stringify(t));
  for (const k of ['PAY-1', 'PAY-2']) await env.post(`tasks/${k}/transition`, { to: 'done', actor: 'user' });
  env.cli('stage', 'tracker');
  env.cli('stage', 'work');
  await until(async () => (await env.task('PAY-5')).open_questions === 1, 30000, 'вопрос PAY-5');
  await until(async () => (await env.task('PAY-5')).agent_state === 'idle', 30000, 'ход PAY-5');
  await env.stop();

  // этап 2: без раннера — остальное состояние задаётся напрямую, ни один агент больше не запускается
  await env.start({ runner: false });
  const post = env.post;
  const update = (key, actor, fields) => post(`tasks/${key}/update`, { actor, fields });
  const status = (key, to, actor = 'user') => post(`tasks/${key}/transition`, { to, actor });
  await update('PAY-5', 'user', { description: DESC5 });
  // в карточке показывается CLI агента; на демо — обычный claude, хотя ходы делал поддельный агент
  await update('PAY-5', 'system', { harness: 'claude' });
  for (const t of tasks.filter(t => !['PAY-1', 'PAY-2', 'PAY-5'].includes(t.key))) env.cli('add-task', '--json', JSON.stringify(t));

  await status('PAY-3', 'sent');
  await status('PAY-4', 'in_progress', 'agent');
  await update('PAY-4', 'system', { agent_state: 'running' });
  await update('PAY-4', 'agent', { summary: 'Copies payments in batches of 5,000 and resumes from the last cursor.' });
  await status('PAY-6', 'in_progress', 'agent');
  await status('PAY-6', 'in_review', 'agent');
  await update('PAY-6', 'agent', { summary: 'Nightly job compares both stores and reports mismatches. Ready for review.' });
  await status('PAY-10', 'in_progress', 'agent');
  await update('PAY-10', 'system', { agent_state: 'running' });
  await update('PAY-10', 'agent', { summary: 'Replaying a peak day against the copy, p99 so far 140 ms.' });

  await post('threads/PAY-5/messages', { author: 'project_agent', text: 'PAY-6 matches rows by `updated_at` — please copy it from the legacy row instead of setting it in PostgreSQL.' });
  await post('threads/PAY-5/messages', { author: 'agent', text: 'Done: `updated_at` now comes from the legacy row.' });

  await post('threads/_project/messages', { author: 'user', text: 'PAY-5 will probably park failed writes in an outbox. Is anything missing from the plan?' });
  await post('threads/_project/messages', { author: 'project_agent', text: 'Yes: nobody replays the outbox yet. I suggest a separate task in payments-worker that PAY-11 waits for, so reads switch only after the stores are in sync.' });
  await post('proposals', {
    kind: 'add_task',
    payload: { task: { key: 'PAY-13', title: 'Replay job for the dual-write outbox', repo: 'payments-worker', estimate: 1, deps: ['PAY-5'],
      description: 'Replay payments parked in the outbox until PostgreSQL accepts them.', done_when: 'The outbox drains after a PostgreSQL outage; replays are idempotent.' } },
    reason: 'Failed dual writes must reach PostgreSQL before reads switch over.',
  });

  browser = await puppeteer.launch({ headless: true, args: ['--no-sandbox'] });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  await page.setViewport(VIEW);
  await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);
  await page.goto(env.url.replace(/#.*$/, '') + '#/p/pay', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.querySelectorAll('.card[data-key]').length === 12, { timeout: 10000 });
  await page.waitForFunction(() => document.querySelector('.sync')?.textContent === 'Online', { timeout: 10000 });

  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const shot = async name => page.screenshot({ path: path.join(dir, name + '.png') });
  // панель «Проект» открывается сама из-за предложения — для общего вида доски её сворачиваем
  await page.evaluate(() => document.querySelector(`aside.dock:not([hidden]) button[aria-label="Collapse"]`)?.click());
  await page.waitForSelector('aside.dock[hidden]', { timeout: 5000 });
  await page.evaluate(() => document.activeElement?.blur());
  await sleep(400);
  await shot('board');

  // запись: кадры приходят только при изменении экрана, метка времени — для длительности кадра в GIF
  const frames = [];
  const cdp = await page.createCDPSession();
  cdp.on('Page.screencastFrame', async ({ data, metadata, sessionId }) => {
    frames.push({ data, t: metadata.timestamp });
    await cdp.send('Page.screencastFrameAck', { sessionId }).catch(() => {});
  });

  // экранный курсор: в headless-браузере системного нет, а зрителю нужно видеть, куда нажимают
  await page.evaluate(() => {
    const c = document.createElement('div');
    c.id = 'demo-cursor';
    c.innerHTML = '<svg width="22" height="22" viewBox="0 0 24 24"><path d="M3 2l7.5 19 2.6-7.9L21 10.5z" fill="#111" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    Object.assign(c.style, { position: 'fixed', left: '0', top: '0', zIndex: '2147483647', pointerEvents: 'none',
      transform: 'translate(640px, 520px)', transition: 'transform 700ms cubic-bezier(.4,0,.2,1)' });
    document.documentElement.append(c);
  });
  const center = async selector => {
    const el = await page.waitForSelector(selector, { visible: true, timeout: 10000 });
    const b = await el.boundingBox();
    return { x: b.x + Math.min(b.width / 2, 60), y: b.y + b.height / 2 };
  };
  const moveTo = async ({ x, y }) => {
    await page.evaluate((x, y) => { document.getElementById('demo-cursor').style.transform = `translate(${x - 3}px, ${y - 2}px)`; }, x, y);
    await page.mouse.move(x, y);
    await sleep(800);
  };
  const click = async selector => {
    const p = await center(selector);
    await moveTo(p);
    await page.evaluate(() => { const c = document.getElementById('demo-cursor'); c.firstChild.style.transform = 'scale(.8)'; });
    await sleep(120);
    await page.mouse.click(p.x, p.y);
    await page.evaluate(() => { document.getElementById('demo-cursor').firstChild.style.transform = ''; });
  };

  await cdp.send('Page.startScreencast', { format: 'png', everyNthFrame: 1 });
  await sleep(2800);
  // карточка с открытым вопросом агента — из «Ждут вас»
  await click('.attn .it.q');
  await page.waitForSelector('.drawer[data-key="PAY-5"] .tabs', { timeout: 10000 });
  await sleep(1200);
  await moveTo(await center('.drawer[data-key="PAY-5"] .tabs'));
  await moveTo({ x: 700, y: 380 });
  await sleep(1800);
  // тред с агентом
  await click('.tabs [data-tab="thread"]');
  await page.waitForSelector('.drawer .thread .msg', { timeout: 10000 });
  await sleep(2600);
  // изменения в worktree: раскрываем правку существующего файла
  await click('.tabs [data-tab="changes"]');
  await page.waitForSelector('.drawer .file[data-path="payments/repository.py"]', { timeout: 10000 });
  await sleep(1200);
  await click('.drawer .file[data-path="payments/repository.py"]');
  await sleep(2800);
  // чат с агентом проекта и предложение новой задачи
  await click('.drawer button[aria-label="Close"]');
  await page.waitForFunction(() => !document.querySelector('.drawer'), { timeout: 10000 });
  await sleep(500);
  await click('.top button[aria-pressed]');
  await page.waitForSelector('aside.dock:not([hidden]) .popen .proposal', { timeout: 10000 });
  await sleep(2600);
  await click('aside.dock .popen .proposal .btn.ok');
  await page.waitForFunction(() => document.querySelectorAll('.card[data-key]').length === 13, { timeout: 10000 });
  await sleep(1800);
  // новая задача на доске: ждёт PAY-5, поэтому в колонке заблокированных
  await click('aside.dock:not([hidden]) button[aria-label="Collapse"]');
  await page.waitForSelector('aside.dock[hidden]', { timeout: 5000 });
  await page.evaluate(() => document.querySelector('.card[data-key="PAY-13"]').scrollIntoView({ behavior: 'smooth', block: 'center' }));
  await sleep(900);
  await moveTo(await center('.card[data-key="PAY-13"]'));
  await sleep(2600);
  await cdp.send('Page.stopScreencast');
  const end = Date.now() / 1000;

  frames.forEach((f, i) => {
    f.file = String(i).padStart(4, '0') + '.png';
    f.duration = Math.round(((frames[i + 1]?.t ?? end) - f.t) * 1000);
    fs.writeFileSync(path.join(framesDir, f.file), Buffer.from(f.data, 'base64'));
  });
  fs.writeFileSync(path.join(framesDir, 'frames.json'), JSON.stringify(frames.map(({ file, duration }) => ({ file, duration })), null, 1));
  results.frames = frames.length;
  results.seconds = Math.round(end - frames[0].t);
  results.errors = errors;
  ok = !errors.length;
} catch (e) {
  results.exception = String(e && e.stack || e);
} finally {
  if (browser) await browser.close();
  await cleanupAll();
}
console.log(JSON.stringify(results, null, 2));
process.exit(ok ? 0 : 1);
