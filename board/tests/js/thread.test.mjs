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
