import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createStore, diffTasks, nextBackoff } from '../../agent_board/static/js/store.js';

function fakeTimers() {
  let now = 0;
  let seq = 0;
  const queue = new Map();
  return {
    setTimeout(fn, ms) { queue.set(++seq, { at: now + ms, fn }); return seq; },
    clearTimeout(id) { queue.delete(id); },
    async advance(ms) {
      const target = now + ms;
      for (;;) {
        const due = [...queue.entries()].filter(([, t]) => t.at <= target).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        queue.delete(due[0]);
        now = due[1].at;
        due[1].fn();
        await flush();
      }
      now = target;
    },
  };
}

const flush = () => new Promise(resolve => setImmediate(resolve));

class FakeEventSource {
  static instances = [];
  constructor(url) { this.url = url; this.listeners = {}; this.closed = false; FakeEventSource.instances.push(this); }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  emit(type, id) { for (const fn of this.listeners[type] || []) fn({ lastEventId: String(id), data: '{}' }); }
  close() { this.closed = true; }
}

function fakeApi(snapshots) {
  const api = { calls: [], get: async path => { api.calls.push(path); return snapshots[Math.min(api.calls.length, snapshots.length) - 1]; } };
  return api;
}

const snap = (lastSeq, tasks) => ({ config: { title: 'Платежи', stage: 'work' }, tasks, last_seq: lastSeq });
const task = (key, extra = {}) => ({ key, title: key, status: 'not_started', updated_at: '1', ...extra });

async function started(snapshots) {
  FakeEventSource.instances = [];
  const timers = fakeTimers();
  const api = fakeApi(snapshots);
  const store = createStore('pay', api, { EventSource: FakeEventSource, timers });
  const notes = [];
  store.subscribe((state, changes) => notes.push({ state, changes }));
  store.start();
  await flush();
  return { store, api, timers, notes, es: () => FakeEventSource.instances.at(-1) };
}

test('nextBackoff растёт от 0.5 до 8 секунд', () => {
  assert.deepEqual([0, 1, 2, 3, 4, 5, 20].map(nextBackoff), [500, 1000, 2000, 4000, 8000, 8000, 8000]);
});

test('diffTasks находит добавленные, удалённые и изменённые задачи', () => {
  // arrange
  const prev = [task('A'), task('B'), task('C', { locks: [] })];
  const next = [task('A'), task('C', { locks: [{ resource: 'db', position: 1 }] }), task('D')];
  // act
  const d = diffTasks(prev, next);
  // assert
  assert.deepEqual(d, { added: ['D'], removed: ['B'], changed: ['C'] });
});

test('diffTasks с пустым прошлым снимком — все задачи добавлены', () => {
  assert.deepEqual(diffTasks(null, [task('A')]), { added: ['A'], removed: [], changed: [] });
});

test('start читает снимок и подключается к потоку с since', async () => {
  // act
  const { store, api, es, notes } = await started([snap(5, [task('A')])]);
  // assert
  assert.deepEqual(api.calls, ['/state']);
  assert.equal(es().url, '/api/p/pay/stream?since=5');
  assert.equal(store.get().snapshot.last_seq, 5);
  assert.equal(store.get().connection, 'connecting');
  assert.deepEqual([...notes[0].changes.tasks], ['A']);
  store.stop();
});

test('события объединяются: не больше одного чтения снимка за 150 мс', async () => {
  // arrange
  const { store, api, es, timers } = await started([snap(5, [])]);
  es().onopen();
  await timers.advance(150);
  assert.equal(api.calls.length, 2);
  // act
  for (let i = 6; i <= 15; i++) es().emit('status_changed', i);
  await timers.advance(149);
  const before = api.calls.length;
  await timers.advance(1);
  // assert
  assert.equal(before, 2);
  assert.equal(api.calls.length, 3);
  store.stop();
});

test('ошибка потока: переподключение с нарастающей задержкой и since последнего события', async () => {
  // arrange
  const { store, es, timers } = await started([snap(5, [])]);
  es().onopen();
  es().emit('task_added', 9);
  const first = es();
  // act
  first.onerror();
  // assert
  assert.equal(first.closed, true);
  assert.equal(store.get().connection, 'reconnecting');
  await timers.advance(499);
  assert.equal(FakeEventSource.instances.length, 1);
  await timers.advance(1);
  assert.equal(FakeEventSource.instances.length, 2);
  assert.equal(es().url, '/api/p/pay/stream?since=9');
  es().onerror();
  await timers.advance(999);
  assert.equal(FakeEventSource.instances.length, 2);
  await timers.advance(1);
  assert.equal(FakeEventSource.instances.length, 3);
  es().onopen();
  assert.equal(store.get().connection, 'live');
  es().onerror();
  await timers.advance(1999);
  assert.equal(FakeEventSource.instances.length, 3);
  await timers.advance(1);
  assert.equal(FakeEventSource.instances.length, 4);
  store.stop();
});

test('задержка сбрасывается после 5 с в сети', async () => {
  // arrange
  const { store, es, timers } = await started([snap(5, [])]);
  es().onerror();
  await timers.advance(500);
  es().onerror();
  await timers.advance(1000);
  // act
  es().onopen();
  await timers.advance(5000);
  es().onerror();
  await timers.advance(500);
  // assert
  assert.equal(FakeEventSource.instances.length, 4);
  store.stop();
});

test('задержка сбрасывается после полученного события', async () => {
  // arrange
  const { store, es, timers } = await started([snap(5, [])]);
  es().onerror();
  await timers.advance(500);
  es().onerror();
  await timers.advance(1000);
  // act
  es().onopen();
  es().emit('task_added', 6);
  es().onerror();
  await timers.advance(500);
  // assert
  assert.equal(FakeEventSource.instances.length, 4);
  store.stop();
});

test('событие во время чтения снимка вызывает ещё одно чтение после него', async () => {
  // arrange
  FakeEventSource.instances = [];
  const timers = fakeTimers();
  const pending = [];
  const api = { calls: 0, get: () => { api.calls++; return new Promise(resolve => pending.push(resolve)); } };
  const store = createStore('pay', api, { EventSource: FakeEventSource, timers });
  store.start();
  pending.shift()(snap(5, []));
  await flush();
  const es = FakeEventSource.instances[0];
  es.emit('task_added', 6);
  await timers.advance(150);
  assert.equal(api.calls, 2);
  // act
  es.emit('task_added', 7);
  await timers.advance(150);
  const duringFlight = api.calls;
  pending.shift()(snap(6, []));
  await flush();
  await timers.advance(150);
  // assert
  assert.equal(duringFlight, 2);
  assert.equal(api.calls, 3);
  store.stop();
});

for (const status of [401, 404]) {
  test(`ошибка ${status} снимка останавливает поток и переподключения`, async () => {
    // arrange
    FakeEventSource.instances = [];
    const timers = fakeTimers();
    let calls = 0;
    const api = { get: async () => { calls++; if (calls === 1) return snap(5, []); throw Object.assign(new Error('x'), { status }); } };
    const store = createStore('pay', api, { EventSource: FakeEventSource, timers });
    store.start();
    await flush();
    const es = FakeEventSource.instances[0];
    // act
    es.emit('task_added', 6);
    await timers.advance(150);
    es.onerror();
    await timers.advance(60000);
    // assert
    assert.equal(es.closed, true);
    assert.equal(FakeEventSource.instances.length, 1);
    assert.equal(store.get().connection, 'offline');
    assert.equal(store.get().error.status, status);
  });
}

test('ошибка 401 при старте — поток не открывается', async () => {
  // arrange
  FakeEventSource.instances = [];
  const timers = fakeTimers();
  const api = { get: async () => { throw Object.assign(new Error('x'), { status: 401 }); } };
  const store = createStore('pay', api, { EventSource: FakeEventSource, timers });
  // act
  store.start();
  await flush();
  // assert
  assert.equal(FakeEventSource.instances.length, 0);
  assert.equal(store.get().connection, 'offline');
});

test('временная ошибка снимка не останавливает поток, следующий снимок её снимает', async () => {
  // arrange
  FakeEventSource.instances = [];
  const timers = fakeTimers();
  let calls = 0;
  const api = { get: async () => { calls++; if (calls === 1) throw Object.assign(new Error('boom'), { status: 500 }); return snap(5, []); } };
  const store = createStore('pay', api, { EventSource: FakeEventSource, timers });
  store.start();
  await flush();
  assert.equal(store.get().error.status, 500);
  // act
  FakeEventSource.instances[0].onopen();
  await timers.advance(150);
  // assert
  assert.equal(store.get().error, null);
  assert.equal(store.get().snapshot.last_seq, 5);
  store.stop();
});

test('после переподключения снимок перечитывается', async () => {
  // arrange
  const { store, api, es, timers } = await started([snap(5, [])]);
  es().onerror();
  await timers.advance(500);
  const callsBefore = api.calls.length;
  // act
  es().onopen();
  await timers.advance(150);
  // assert
  assert.equal(api.calls.length, callsBefore + 1);
  store.stop();
});

test('подписчики получают ключи изменившихся задач', async () => {
  // arrange
  const { store, es, timers, notes } = await started([
    snap(5, [task('A'), task('B')]),
    snap(7, [task('A'), task('B', { status: 'in_progress' }), task('C')]),
  ]);
  // act
  es().emit('status_changed', 6);
  await timers.advance(150);
  // assert
  const last = notes.at(-1).changes;
  assert.deepEqual([...last.tasks].sort(), ['B', 'C']);
  assert.equal(store.get().snapshot.last_seq, 7);
  store.stop();
});

test('устаревший снимок не затирает более новый', async () => {
  // arrange
  const { store, es, timers } = await started([snap(7, [task('A')]), snap(5, [])]);
  // act
  es().emit('task_added', 8);
  await timers.advance(150);
  // assert
  assert.equal(store.get().snapshot.last_seq, 7);
  store.stop();
});

test('ошибка чтения снимка попадает в состояние', async () => {
  // arrange
  FakeEventSource.instances = [];
  const timers = fakeTimers();
  const api = { get: async () => { throw Object.assign(new Error('нет доступа'), { status: 401 }); } };
  const store = createStore('pay', api, { EventSource: FakeEventSource, timers });
  // act
  store.start();
  await flush();
  // assert
  assert.equal(store.get().error.status, 401);
  assert.equal(store.get().snapshot, null);
  store.stop();
});

test('stop закрывает поток и отменяет переподключение', async () => {
  // arrange
  const { store, es, timers } = await started([snap(5, [])]);
  es().onerror();
  // act
  store.stop();
  await timers.advance(10000);
  // assert
  assert.equal(FakeEventSource.instances.length, 1);
});
