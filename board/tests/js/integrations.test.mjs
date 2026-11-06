import { test } from 'node:test';
import assert from 'node:assert/strict';
import { cardActions } from '../../agent_board/static/js/actions.js';
import { reviewChip, reviewOf, reviewTool } from '../../agent_board/static/js/review.js';
import { commandParts, selectShip, shipChip, shipCommand, shipPlan, shippable } from '../../agent_board/static/js/ship.js';
import { relTime, syncModel, toCreate, trackerInfo, trackerLink } from '../../agent_board/static/js/tracker.js';
import { cardModel, taskFilter, waitingForYou, wavesOf } from '../../agent_board/static/js/board.js';
import { T } from '../../agent_board/static/js/i18n.js';

function task(key, extra = {}) {
  const status = extra.status || 'not_started';
  return {
    key, title: 'Задача ' + key, repo: 'api', kind: 'code', estimate: 1, deps: [], status,
    display_status: extra.display_status || status, open_deps: [], external_blocker: null,
    open_questions: 0, unread: 0, agent_state: 'idle', turn_error: '', rule_violations: [],
    last_message: null, locks: [], base_branch: null, summary: '', review: null, ship: null,
    tracker_key: null, tracker_url: null, ...extra,
  };
}

function snap(tasks, config = {}, extra = {}) {
  return {
    config: { title: 'П', stage: 'work', repos: [{ name: 'api', base: 'main' }], links: [], chains: true,
      review: { tool: 'crit', binary: 'crit' }, ship: { command: 'git push origin {branch}', timeout: 600 },
      tracker: { type: 'none' }, ...config },
    tasks, proposals: [], project_agent: { state: 'idle', turn_error: '' }, project_unread: 0, last_seq: 1,
    tracker_sync: null, ...extra,
  };
}

const actionIds = (t, s) => cardActions(t, s).map(a => a.id);
const ids = list => list.map(x => x.key);

// --- ревью ---

test('reviewTool: по умолчанию crit, none — из настроек', () => {
  assert.equal(reviewTool(snap([], { review: undefined })), 'crit');
  assert.equal(reviewTool(snap([], { review: { tool: 'none' } })), 'none');
});

test('reviewOf: ссылка только http(s), история — только записи-объекты', () => {
  // arrange
  const t = task('A', { review: { round: 2, state: 'open', url: 'javascript:alert(1)', history: [{ round: 1, state: 'changes', comments: 3 }, 'мусор'] } });
  // act
  const r = reviewOf(t);
  // assert
  assert.equal(r.url, null);
  assert.deepEqual(r.history.map(x => x.round), [1]);
  assert.equal(reviewOf(task('B', { review: { round: 1, state: 'open', url: 'http://localhost:4000/' } })).url, 'http://localhost:4000/');
  assert.equal(reviewOf(task('C')), null);
});

test('reviewChip: раунд и состояние на карточке доски', () => {
  const chip = (status, review) => reviewChip(task('A', { status, review }));
  assert.deepEqual(chip('in_review', { round: 2, state: 'open' }), { text: 'р2 · ждёт вас', cls: 'rv' });
  assert.deepEqual(chip('in_review', { round: 1, state: 'changes' }), { text: 'р1 · комментарии у агента', cls: '' });
  assert.deepEqual(chip('in_review', { round: 3, state: 'fixing' }), { text: 'р3 · агент правит', cls: '' });
  assert.deepEqual(chip('approved', { round: 2, state: 'approved' }), { text: 'одобрено', cls: 'ok' });
  assert.deepEqual(chip('in_review', { round: 1, state: 'idle', error: 'нет crit' }), { text: 'ревью: ошибка', cls: 'err' });
  assert.equal(chip('in_review', { round: 0, state: 'idle' }), null);
  assert.equal(chip('in_progress', { round: 2, state: 'open' }), null, 'вне ревью чип не нужен');
});

test('действия ревью: начать — без открытого раунда и только с crit', () => {
  const s = snap([]);
  assert.ok(actionIds(task('A', { status: 'in_review' }), s).includes('review-start'));
  assert.ok(actionIds(task('A', { status: 'in_review', review: { round: 1, state: 'changes' } }), s).includes('review-start'));
  assert.ok(!actionIds(task('A', { status: 'in_review', review: { round: 1, state: 'open', url: 'http://x/' } }), s).includes('review-start'));
  assert.ok(!actionIds(task('A', { status: 'in_review', review: { round: 1, state: 'fixing' } }), s).includes('review-start'));
  assert.ok(!actionIds(task('A', { status: 'in_progress' }), s).includes('review-start'));
  assert.ok(!actionIds(task('A', { status: 'in_review' }), snap([], { review: { tool: 'none' } })).includes('review-start'));
  assert.ok(!actionIds(task('A', { status: 'in_review', kind: 'research' }), s).includes('review-start'));
});

test('действия ревью: открыть — только открытый раунд со ссылкой; одобрить и вернуть — на ревью', () => {
  const s = snap([]);
  const open = task('A', { status: 'in_review', review: { round: 1, state: 'open', url: 'http://localhost:1/' } });
  assert.deepEqual(actionIds(open, s).filter(id => id.startsWith('review')), ['review-open', 'review-approve', 'review-return']);
  const noUrl = task('A', { status: 'in_review', review: { round: 1, state: 'open', url: null } });
  assert.ok(!actionIds(noUrl, s).includes('review-open'));
  assert.ok(actionIds(task('A', { status: 'approved' }), s).includes('review-return'));
  assert.ok(!actionIds(task('A', { status: 'approved' }), s).includes('review-approve'));
  assert.ok(!actionIds(task('A', { status: 'in_progress' }), s).includes('review-return'));
});

// --- отправка ---

test('действие отправки: только одобренная, есть команда, не отправляется сейчас', () => {
  const s = snap([]);
  assert.ok(actionIds(task('A', { status: 'approved' }), s).includes('ship'));
  assert.ok(!actionIds(task('A', { status: 'approved' }), snap([], { ship: { command: null } })).includes('ship'));
  assert.ok(!actionIds(task('A', { status: 'in_review' }), s).includes('ship'));
  const running = cardActions(task('A', { status: 'approved', ship: { state: 'running' } }), s).find(a => a.id === 'ship');
  assert.equal(running.enabled, false);
});

test('shipCommand: пустая команда — нет отправки', () => {
  assert.equal(shipCommand(snap([], { ship: { command: '  ' } })), null);
  assert.equal(shipCommand(snap([], { ship: undefined })), null);
  assert.equal(shipCommand(snap([])), 'git push origin {branch}');
});

test('shippable: одобренные без идущей отправки', () => {
  const s = snap([
    task('A', { status: 'approved' }),
    task('B', { status: 'approved', ship: { state: 'queued' } }),
    task('C', { status: 'approved', ship: { state: 'error' } }),
    task('D', { status: 'sent' }),
  ]);
  assert.deepEqual(ids(shippable(s)), ['A', 'C']);
  assert.deepEqual(shippable(snap(s.tasks, { ship: { command: null } })), []);
});

test('shipPlan: порядок по графу, обязательные зависимости и недоступные задачи', () => {
  // arrange: B ждёт A (одобрена), C ждёт X (ещё в работе), D ждёт S (уже отправлена)
  const s = snap([
    task('B', { status: 'approved', deps: ['A'] }),
    task('A', { status: 'approved' }),
    task('C', { status: 'approved', deps: ['X'] }),
    task('X', { status: 'in_progress' }),
    task('D', { status: 'approved', deps: ['S'] }),
    task('S', { status: 'sent' }),
  ]);
  // act
  const plan = shipPlan(s);
  // assert
  assert.deepEqual(ids(plan), ['A', 'B', 'C', 'D']);
  assert.deepEqual(plan.find(p => p.key === 'B').requires, ['A']);
  assert.match(plan.find(p => p.key === 'C').blocked, /X/);
  assert.equal(plan.find(p => p.key === 'D').blocked, null);
});

test('selectShip: выбор тянет зависимости, снятие — зависимые', () => {
  const s = snap([
    task('A', { status: 'approved' }),
    task('B', { status: 'approved', deps: ['A'] }),
    task('C', { status: 'approved', deps: ['B'] }),
    task('E', { status: 'approved' }),
  ]);
  const plan = shipPlan(s);
  assert.deepEqual([...selectShip(plan, new Set(), 'C', true)].sort(), ['A', 'B', 'C']);
  assert.deepEqual([...selectShip(plan, new Set(['A', 'B', 'C', 'E']), 'A', false)].sort(), ['E']);
  assert.deepEqual([...selectShip(plan, new Set(['A', 'B', 'C']), 'C', false)].sort(), ['A', 'B']);
});

test('shipChip: идущая и неудачная отправка видны на карточке', () => {
  assert.deepEqual(shipChip(task('A', { status: 'approved', ship: { state: 'running' } })), { text: 'отправляется', cls: 'rv' });
  assert.deepEqual(shipChip(task('A', { status: 'approved', ship: { state: 'queued' } })), { text: 'в очереди на отправку', cls: '' });
  assert.deepEqual(shipChip(task('A', { status: 'approved', ship: { state: 'error' } })), { text: 'отправка не удалась', cls: 'err' });
  assert.equal(shipChip(task('A', { status: 'sent', ship: { state: 'done' } })), null);
  assert.equal(shipChip(task('A', { status: 'in_progress', ship: { state: 'error' } })), null, 'после возврата в работу старая ошибка не нужна');
});

test('commandParts выделяет известные плейсхолдеры', () => {
  assert.deepEqual(commandParts('gh pr create -H {branch} -B {base} {x}'), [
    { text: 'gh pr create -H ', ph: false }, { text: '{branch}', ph: true }, { text: ' -B ', ph: false },
    { text: '{base}', ph: true }, { text: ' {x}', ph: false },
  ]);
});

// --- трекер ---

test('trackerInfo: тип и хост, без трекера — null', () => {
  assert.deepEqual(trackerInfo({ tracker: { type: 'jira', url: 'https://jira.example.com/' } }), { type: 'jira', name: 'Jira', host: 'jira.example.com' });
  assert.deepEqual(trackerInfo({ tracker: { type: 'github', repo: 'o/r' } }), { type: 'github', name: 'GitHub', host: 'github.com/o/r' });
  assert.deepEqual(trackerInfo({ tracker: { type: 'jira', url: 'не адрес' } }), { type: 'jira', name: 'Jira', host: '' });
  assert.equal(trackerInfo({ tracker: { type: 'none' } }), null);
  assert.equal(trackerInfo({}), null);
});

test('relTime: секунды, минуты, часы', () => {
  const now = Date.parse('2026-11-06T10:00:00Z');
  assert.equal(relTime('2026-11-06T09:59:57Z', now), 'только что');
  assert.equal(relTime('2026-11-06T09:59:30Z', now), '30 с назад');
  assert.equal(relTime('2026-11-06T09:55:00Z', now), '5 мин назад');
  assert.equal(relTime('2026-11-06T07:00:00Z', now), '3 ч назад');
  assert.equal(relTime('мусор', now), '');
});

test('syncModel: ок, ошибка, пауза и ещё не было прохода', () => {
  const now = Date.parse('2026-11-06T10:00:00Z');
  const cfg = { stage: 'work', tracker: { type: 'jira', url: 'https://jira.example.com' } };
  const at = '2026-11-06T09:59:30Z';
  const m = sync => syncModel({ config: cfg, tracker_sync: sync }, now);
  assert.deepEqual(m({ at, ok: true, error: null, paused: false }), { state: 'ok', text: 'Jira · 30 с назад', host: 'jira.example.com', name: 'Jira' });
  assert.equal(m({ at, ok: false, error: 'сеть', paused: false }).state, 'err');
  assert.equal(m({ at, ok: false, error: 'сеть', paused: false }).text, 'Jira: ошибка · 30 с назад');
  assert.equal(m({ at, ok: false, error: '401', paused: true }).text, 'Jira: на паузе');
  assert.equal(m(null).state, 'never');
  assert.equal(m({ at, ok: true, errors: [{ key: 'A', error: 'x' }] }).state, 'warn', 'ошибки отдельных задач видны');
  const insecure = m({ at, ok: true, errors: [], warning: 'Трекер jira доступен по http без шифрования' });
  assert.equal(insecure.state, 'warn', 'http без шифрования виден в индикаторе');
  assert.equal(insecure.text, 'Jira · 30 с назад · без https');
  assert.equal(insecure.warning, 'Трекер jira доступен по http без шифрования');
  assert.equal(m({ at, ok: false, error: 'сеть', warning: 'http' }).state, 'err', 'ошибка важнее предупреждения');
  assert.equal(syncModel({ config: { ...cfg, stage: 'tracker' }, tracker_sync: null }, now), null, 'до стадии работы синхронизации нет');
  assert.equal(syncModel({ config: { stage: 'work', tracker: { type: 'none' } }, tracker_sync: null }, now), null);
});

test('toCreate: заводятся задачи без ключа трекера, кроме отменённых', () => {
  const snap = { tasks: [task('A', { tracker_key: 'MNZ-1' }), task('B'), task('C', { status: 'cancelled' }), task('D')] };
  assert.deepEqual(toCreate(snap).map(t => t.key), ['B', 'D']);
  assert.equal(T.tracker.confirmText(2, 'jira.example.com', 'Jira'), 'В Jira (jira.example.com) будут заведены 2 задачи, затем проставятся связи между ними.');
  assert.equal(T.tracker.confirmText(0, 'jira.example.com', 'Jira'), 'Новых задач нет: в Jira (jira.example.com) проставятся только недостающие связи.');
});

test('trackerLink: ссылка только абсолютная http(s)', () => {
  assert.deepEqual(trackerLink(task('A', { tracker_key: 'MNZ-1', tracker_url: 'https://j/browse/MNZ-1' })), { key: 'MNZ-1', url: 'https://j/browse/MNZ-1' });
  assert.deepEqual(trackerLink(task('A', { tracker_key: 'MNZ-1', tracker_url: '/browse/MNZ-1' })), { key: 'MNZ-1', url: null });
  assert.deepEqual(trackerLink(task('A', { tracker_key: 'MNZ-1', tracker_url: 'javascript:1' })), { key: 'MNZ-1', url: null });
  assert.equal(trackerLink(task('A')), null);
});

// --- доска ---

test('wavesOf: волна — слой графа зависимостей, отменённые без волны', () => {
  const w = wavesOf([task('A'), task('B', { deps: ['A'] }), task('C', { deps: ['B', 'A'] }), task('D'), task('Z', { status: 'cancelled' })]);
  assert.deepEqual(Object.fromEntries(w), { A: 1, B: 2, C: 3, D: 1 });
});

test('taskFilter: фильтр волны', () => {
  const s = snap([task('A'), task('B', { deps: ['A'] })]);
  assert.deepEqual(s.tasks.filter(taskFilter(s, { wave: '2' })).map(t => t.key), ['B']);
  assert.deepEqual(s.tasks.filter(taskFilter(s, { wave: '' })).map(t => t.key), ['A', 'B']);
});

test('waitingForYou: ревью ждёт только без работы агента, отправка и синхронизация', () => {
  // arrange
  const s = snap([
    task('R1', { status: 'in_review', review: { round: 2, state: 'open' } }),
    task('R2', { status: 'in_review', review: { round: 1, state: 'fixing' } }),
    task('A1', { status: 'approved' }),
    task('A2', { status: 'approved', ship: { state: 'error', exit_code: 1 } }),
  ], { tracker: { type: 'jira', url: 'https://j' } }, { tracker_sync: { at: '2026-01-01T00:00:00Z', ok: false, error: '401', paused: true } });
  // act
  const items = waitingForYou(s);
  // assert
  assert.deepEqual(items.map(i => [i.kind, i.key || null]), [
    ['review', 'R1'], ['ship_error', 'A2'], ['ship', null], ['sync', null],
  ]);
  assert.equal(items[0].round, 2);
  assert.equal(items[2].count, 2);
  assert.deepEqual(items[2].open, { type: 'ship' });
  assert.deepEqual(items[3].open, { type: 'sync' });
  assert.ok(!waitingForYou(snap(s.tasks, { ship: { command: null } })).some(i => i.kind === 'ship'), 'без команды отправки пункта нет');
});

test('cardModel: чипы ревью, отправки и ключ трекера', () => {
  const ctx = { byKey: new Map(), proposalKeys: new Set(), repoBase: new Map() };
  const m = cardModel(task('A', { status: 'approved', review: { round: 2, state: 'approved' }, ship: { state: 'error' },
    tracker_key: 'MNZ-7', tracker_url: 'https://j/browse/MNZ-7' }), ctx);
  assert.deepEqual(m.review, { text: 'одобрено', cls: 'ok' });
  assert.deepEqual(m.ship, { text: 'отправка не удалась', cls: 'err' });
  assert.deepEqual(m.tracker, { key: 'MNZ-7', url: 'https://j/browse/MNZ-7' });
});

// --- исправления ревью ---

import { shipValues } from '../../agent_board/static/js/ship.js';
import { stateClass } from '../../agent_board/static/js/review.js';

test('shipValues: значения всех плейсхолдеров шаблона, {base} — ветка единственной неотправленной зависимости', () => {
  // arrange
  const cfg = { repos: [{ name: 'api', path: '/src/api', base: 'master' }],
    ship: { command: 'gh pr create -H {branch} -B {base} -t {title} -R {repo} # {key} {worktree} {base}' } };
  const a = task('A', { status: 'approved', branch: 'A', worktree: '/wt/A' });
  const b = task('B', { status: 'approved', deps: ['A'], branch: 'B', worktree: '/wt/B', title: 'Чтение' });
  const s = snap([a, b], cfg);
  // act
  const vb = shipValues(s, b);
  // assert
  assert.equal(vb.error, null);
  assert.deepEqual(vb.values.map(v => [v.ph, v.value]), [
    ['{branch}', 'B'], ['{base}', 'A'], ['{title}', 'Чтение'], ['{repo}', '/src/api'], ['{key}', 'B'], ['{worktree}', '/wt/B'],
  ]);
  assert.equal(shipValues(s, a).values.find(v => v.ph === '{base}').value, 'master', 'без зависимостей — база репозитория');
  const sentDep = snap([{ ...a, status: 'sent' }, b], cfg);
  assert.equal(shipValues(sentDep, b).values.find(v => v.ph === '{base}').value, 'A', 'отправленная, но не влитая зависимость — тоже база');
  const doneDep = snap([{ ...a, status: 'done' }, b], cfg);
  assert.equal(shipValues(doneDep, b).values.find(v => v.ph === '{base}').value, 'master');
  assert.equal(shipValues(snap([a, b], { ...cfg, chains: false }), b).values.find(v => v.ph === '{base}').value, 'master');
  const two = snap([a, task('C', { status: 'approved', branch: 'C' }), { ...b, deps: ['A', 'C'] }], cfg);
  assert.match(shipValues(two, { ...b, deps: ['A', 'C'] }).error, /A, C/);
});

test('отправка одной задачи недоступна, пока не отправлена одобренная зависимость; есть переход к пакетной', () => {
  const a = task('A', { status: 'approved' });
  const b = task('B', { status: 'approved', deps: ['A'] });
  const s = snap([a, b]);
  const ship = cardActions(b, s).find(x => x.id === 'ship');
  assert.equal(ship.enabled, false);
  assert.equal(ship.title, 'сначала отправьте A');
  assert.ok(actionIds(b, s).includes('ship-batch'));
  assert.ok(!actionIds(a, s).includes('ship-batch'));
  assert.equal(cardActions(a, s).find(x => x.id === 'ship').enabled, true);
});

test('reviewChip: «одобрено» и без данных ревью', () => {
  assert.deepEqual(reviewChip(task('A', { status: 'approved' })), { text: 'одобрено', cls: 'ok' });
});

test('stateClass: в класс попадают только известные состояния', () => {
  assert.equal(stateClass('open'), 'open');
  assert.equal(stateClass('x" onclick'), '');
  assert.equal(stateClass(null), '');
});
