import { test } from 'node:test';
import assert from 'node:assert/strict';
import { installFakeDocument } from './fake-dom.mjs';
import { createThread } from '../../agent_board/static/js/thread.js';
import { commentDraftKey } from '../../agent_board/static/js/decomposition.js';

const doc = installFakeDocument();
// иконкам нужен <template>: в тесте он пустой
const createElement = doc.createElement;
doc.createElement = tag => Object.assign(createElement(tag), tag === 'template' ? { content: { firstChild: null } } : {});

function memoryDrafts(initial) {
  const saved = new Map(Object.entries(initial));
  return { saved, get: key => saved.get(key) || '', set: (key, text) => saved.set(key, text) };
}

function thread(drafts, extra = {}) {
  return createThread({
    client: { get: async () => ({ messages: [] }), post: async () => ({}) },
    thread: 'T-1', drafts, scroller: () => ({ scrollTop: 0, scrollHeight: 0, clientHeight: 0 }),
    visible: () => false, alive: () => true, placeholder: 'Комментарий', ...extra,
  });
}

const areaOf = t => t.area;

test('черновик комментария разбивки хранится отдельно от черновика треда задачи', () => {
  // arrange
  const drafts = memoryDrafts({ 'T-1': 'сообщение агенту', [commentDraftKey('T-1')]: 'комментарий к разбивке' });
  // act
  const area = areaOf(thread(drafts, { draftKey: commentDraftKey('T-1') }));
  area.value = 'комментарий, правка';
  area.listeners.input.forEach(fn => fn());
  // assert
  assert.equal(commentDraftKey('T-1'), 'decomp:T-1');
  assert.equal(drafts.saved.get('decomp:T-1'), 'комментарий, правка');
  assert.equal(drafts.saved.get('T-1'), 'сообщение агенту');
});

test('без draftKey черновик треда хранится под именем треда', () => {
  // arrange
  const drafts = memoryDrafts({ 'T-1': 'сообщение агенту' });
  // act
  const area = areaOf(thread(drafts));
  // assert
  assert.equal(area.value, 'сообщение агенту');
});

// --- галочка «прервать текущую работу» ---

const RUNNING = { kind: 'running', since: null };

function sendingThread(post = async () => ({})) {
  const posts = [];
  const t = thread(memoryDrafts({}), {
    client: { get: async () => ({ messages: [] }), post: (path, body) => { posts.push({ path, body }); return post(); } },
  });
  const type = text => {
    t.area.value = text;
    t.area.listeners.keydown.forEach(fn => fn({ key: 'Enter', ctrlKey: true, preventDefault() {} }));
  };
  return { t, posts, type, label: t.interrupt.parentNode };
}

const settle = () => new Promise(resolve => setImmediate(resolve));

test('paint_agentRunning_ShouldShowInterruptCheckbox', () => {
  // arrange
  const { t, label } = sendingThread();
  // act
  t.paint({ size: 0, agent: RUNNING });
  // assert
  assert.equal(label.hidden, false);
  assert.match(label.textContent, /Interrupt current work|Прервать текущую работу/);
});

test('paint_agentIdleOrWaiting_ShouldHideInterruptCheckbox', () => {
  // arrange
  const { t, label } = sendingThread();
  const states = [undefined, null, { kind: 'waiting', wait: {} }, { kind: 'error', text: 'x' }, { kind: 'parked' }];
  for (const agent of states) {
    // act
    t.paint({ size: 0, agent });
    // assert
    assert.equal(label.hidden, true, JSON.stringify(agent));
  }
});

test('submit_checkedWhileRunning_ShouldSendInterruptAndResetCheckbox', () => {
  // arrange
  const { t, posts, type } = sendingThread();
  t.paint({ size: 0, agent: RUNNING });
  t.interrupt.checked = true;
  // act
  type('стоп, сделай иначе');
  // assert
  assert.deepEqual(posts, [{ path: '/threads/T-1/messages', body: { author: 'user', text: 'стоп, сделай иначе', interrupt: true } }]);
  assert.equal(t.interrupt.checked, false);
});

test('submit_notChecked_ShouldSendPlainMessage', () => {
  // arrange
  const { t, posts, type } = sendingThread();
  t.paint({ size: 0, agent: RUNNING });
  // act
  type('просто вопрос');
  // assert
  assert.deepEqual(posts[0].body, { author: 'user', text: 'просто вопрос' });
});

test('submit_turnFinishedAfterChecking_ShouldSendPlainMessage', () => {
  // arrange
  const { t, posts, type, label } = sendingThread();
  t.paint({ size: 0, agent: RUNNING });
  t.interrupt.checked = true;
  t.paint({ size: 0, agent: null });
  // act
  type('после хода');
  // assert
  assert.equal(label.hidden, true);
  assert.equal(t.interrupt.checked, false);
  assert.deepEqual(posts[0].body, { author: 'user', text: 'после хода' });
});

test('submit_requestFailed_ShouldRestoreCheckboxWhileTurnRuns', async () => {
  // arrange
  const { t, type } = sendingThread(async () => { throw new Error('сеть'); });
  t.paint({ size: 0, agent: RUNNING });
  t.interrupt.checked = true;
  // act
  type('стоп');
  await settle();
  // assert
  assert.equal(t.area.value, 'стоп');
  assert.equal(t.interrupt.checked, true);
});
