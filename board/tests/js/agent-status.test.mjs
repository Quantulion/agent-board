import { test } from 'node:test';
import assert from 'node:assert/strict';
import { installFakeDocument } from './fake-dom.mjs';
import {
  createAgentIndicator, formatElapsed, threadAgentModel, turnStartedAt,
} from '../../agent_board/static/js/agent-status.js';
import { createThread } from '../../agent_board/static/js/thread.js';
import { setLanguage } from '../../agent_board/static/js/i18n.js';

installFakeDocument();
setLanguage('ru');

const config = { max_agents: 2, global_max_agents: 6 };
const START = '2026-11-06T10:00:00+00:00';

function fakeTimers() {
  const live = new Map();
  let next = 1;
  return {
    live,
    setInterval: fn => { live.set(next, fn); return next++; },
    clearInterval: id => { live.delete(id); },
    fire: () => [...live.values()].forEach(fn => fn()),
  };
}

test('turnStartedAt: время, записанное доской, иначе время запуска процесса', () => {
  // act + assert
  assert.equal(turnStartedAt({ at: START, started: 'Thu Nov  5 19:09:21 2026' }), Date.parse(START));
  assert.equal(turnStartedAt({ started: 'Thu Nov  5 19:09:21 2026' }), Date.parse('Thu Nov  5 19:09:21 2026'));
  assert.equal(turnStartedAt({ started: 'чт,  5 ноября' }), null);
  assert.equal(turnStartedAt(null), null);
});

test('threadAgentModel: работает, ждёт слота, ошибка, остановлен; простой — без индикатора', () => {
  // act + assert
  assert.deepEqual(threadAgentModel({ state: 'running', turn_process: { at: START } }, config),
    { kind: 'running', since: Date.parse(START) });
  assert.deepEqual(threadAgentModel({ state: 'running', turn_process: null }, config), { kind: 'running', since: null });
  assert.deepEqual(threadAgentModel({ state: 'waiting_turn', wait_reason: 'project' }, config),
    { kind: 'waiting', wait: { reason: 'project', field: 'max_agents', limit: 2 } });
  assert.deepEqual(threadAgentModel({ state: 'error', turn_error: 'boom' }, config), { kind: 'error', text: 'boom' });
  assert.deepEqual(threadAgentModel({ state: 'error', turn_error: '' }, config), { kind: 'error', text: 'ошибка агента' });
  assert.deepEqual(threadAgentModel({ state: 'idle', parked: true }, config), { kind: 'parked' });
  assert.equal(threadAgentModel({ state: 'idle', turn_error: 'старое' }, config), null);
  assert.equal(threadAgentModel(null, config), null);
});

test('formatElapsed: секунды, минуты, часы на ru и en', () => {
  // act
  const ru = [0, 45_000, 185_000, 3_725_000, -5].map(formatElapsed);
  setLanguage('en');
  const en = [45_000, 185_000, 3_725_000].map(formatElapsed);
  setLanguage('ru');
  // assert
  assert.deepEqual(ru, ['0 с', '45 с', '3 мин 05 с', '1 ч 02 мин', '0 с']);
  assert.deepEqual(en, ['45s', '3m 05s', '1h 02m']);
});

test('индикатор: часы хода идут, при завершении хода индикатор пропадает и часы останавливаются', () => {
  // arrange
  const timers = fakeTimers();
  let now = Date.parse(START) + 5_000;
  const ind = createAgentIndicator({ parkedText: 'остановлено', alive: () => true, now: () => now, timers });
  // act
  ind.paint({ kind: 'running', since: Date.parse(START) });
  const first = ind.el.textContent;
  now += 60_000;
  timers.fire();
  const later = ind.el.textContent;
  ind.paint(null);
  // assert
  assert.match(first, /Агент работает над ответом/);
  assert.match(first, /5 с/);
  assert.match(later, /1 мин 05 с/);
  assert.equal(ind.el.hidden, true);
  assert.equal(ind.el.textContent, '');
  assert.equal(timers.live.size, 0);
});

test('индикатор: тот же ход не перерисовывается, без времени начала часов нет', () => {
  // arrange
  const timers = fakeTimers();
  const ind = createAgentIndicator({ parkedText: 'остановлено', alive: () => true, timers });
  // act
  ind.paint({ kind: 'running', since: null });
  const node = ind.el.firstChild;
  ind.paint({ kind: 'running', since: null });
  // assert
  assert.equal(ind.el.firstChild, node);
  assert.equal(timers.live.size, 0);
  assert.equal(ind.el.hidden, false);
});

test('индикатор: закрытый тред останавливает часы', () => {
  // arrange
  const timers = fakeTimers();
  let open = true;
  const ind = createAgentIndicator({ parkedText: 'остановлено', alive: () => open, timers });
  ind.paint({ kind: 'running', since: Date.now() });
  // act
  open = false;
  timers.fire();
  // assert
  assert.equal(timers.live.size, 0);
});

test('индикатор: ожидание слота, ошибка и остановка показывают свои пояснения', () => {
  // arrange
  const ind = createAgentIndicator({ parkedText: 'Переписка остановлена', alive: () => true, timers: fakeTimers() });
  // act
  ind.paint(threadAgentModel({ state: 'waiting_turn', wait_reason: 'global' }, config));
  const waiting = ind.el.textContent;
  ind.paint(threadAgentModel({ state: 'error', turn_error: 'exit 1' }, config));
  const error = ind.el.textContent;
  ind.paint(threadAgentModel({ state: 'idle', parked: true }, config));
  const parked = ind.el.textContent;
  // assert
  assert.match(waiting, /Ждёт свободный слот/);
  assert.match(waiting, /общий лимит/);
  assert.match(error, /Ошибка хода: exit 1/);
  assert.equal(parked, 'Переписка остановлена');
});

test('тред: индикатор стоит последним под сообщениями и следует за состоянием агента', () => {
  // arrange
  const t = createThread({
    client: { get: async () => ({ messages: [] }), post: async () => ({}) },
    thread: 'T-1', drafts: { get: () => '', set: () => {} },
    scroller: () => ({ scrollTop: 0, scrollHeight: 0, clientHeight: 0 }),
    visible: () => false, alive: () => true, placeholder: 'Сообщение',
  });
  const indicator = t.list[t.list.length - 1];
  // act
  t.paint({ size: 0, unread: 0, agent: threadAgentModel({ state: 'idle', parked: true }, config) });
  const parked = indicator.textContent;
  t.paint({ size: 0, unread: 0 });
  // assert
  assert.match(parked, /остановлено/);
  assert.equal(indicator.hidden, true);
});

test('индикатор: у ошибки хода кнопка «Повторить», после выхода из ошибки она сбрасывается', () => {
  // arrange
  let resets = 0;
  const btn = document.createElement('button');
  const retry = { el: btn, reset: () => { resets++; } };
  const ind = createAgentIndicator({ parkedText: 'остановлено', alive: () => true, retry, timers: fakeTimers() });
  // act
  ind.paint(threadAgentModel({ state: 'error', turn_error: 'exit 1' }, config));
  const shown = ind.el.querySelectorAll('button').includes(btn);
  const resetsOnError = resets;
  ind.paint(threadAgentModel({ state: 'running', turn_process: null }, config));
  // assert
  assert.equal(shown, true);
  assert.equal(resetsOnError, 0);
  assert.equal(resets, 1);
  assert.equal(ind.el.querySelectorAll('button').includes(btn), false);
});
