import { test } from 'node:test';
import assert from 'node:assert/strict';
import { actionsSignature, cardActions, registerCardAction, unregisterCardAction } from '../../agent_board/static/js/actions.js';
import { statusOf } from '../../agent_board/static/js/chips.js';

const def = (id, extra = {}) => ({ id, label: id, icon: 'eye', visible: () => true, enabled: () => true, run() {}, ...extra });
const ids = (task, snap) => cardActions(task, snap).map(a => a.id).filter(id => id.startsWith('t-'));

test('cardActions возвращает видимые действия в порядке регистрации', () => {
  registerCardAction(def('t-a'));
  registerCardAction(def('t-b', { visible: task => task.kind === 'code' }));
  registerCardAction(def('t-c'));
  assert.deepEqual(ids({ kind: 'code' }, {}), ['t-a', 't-b', 't-c']);
  assert.deepEqual(ids({ kind: 'research' }, {}), ['t-a', 't-c']);
  ['t-a', 't-b', 't-c'].forEach(unregisterCardAction);
});

test('cardActions передаёт задачу и снимок в visible/enabled и отдаёт подсказку', () => {
  const seen = [];
  registerCardAction(def('t-d', {
    enabled: (task, snap) => { seen.push([task.key, snap.n]); return snap.n > 1; },
    title: task => 'Нельзя: ' + task.key,
  }));
  const [a] = cardActions({ key: 'K-1' }, { n: 1 }).filter(x => x.id === 't-d');
  assert.equal(a.enabled, false);
  assert.equal(a.title, 'Нельзя: K-1');
  assert.deepEqual(seen, [['K-1', 1]]);
  unregisterCardAction('t-d');
});

test('повторная регистрация заменяет действие на прежнем месте', () => {
  registerCardAction(def('t-e'));
  registerCardAction(def('t-f'));
  registerCardAction(def('t-e', { label: 'новое' }));
  const list = cardActions({}, {}).filter(a => a.id.startsWith('t-'));
  assert.deepEqual(list.map(a => [a.id, a.label]), [['t-e', 'новое'], ['t-f', 't-f']]);
  ['t-e', 't-f'].forEach(unregisterCardAction);
});

test('actionsSignature меняется при смене доступности и не зависит от run', () => {
  const a = { id: 'x', label: 'X', icon: 'eye', enabled: true, title: null, run() {} };
  assert.equal(actionsSignature([a]), actionsSignature([{ ...a, run() {} }]));
  assert.notEqual(actionsSignature([a]), actionsSignature([{ ...a, enabled: false }]));
});

test('registerCardAction отвергает действие без id или run', () => {
  assert.throws(() => registerCardAction({ label: 'x' }));
  assert.throws(() => registerCardAction({ id: 'x' }));
});

test('statusOf предпочитает display_status', () => {
  assert.equal(statusOf({ status: 'a', display_status: 'b' }), 'b');
  assert.equal(statusOf({ status: 'a' }), 'a');
});
