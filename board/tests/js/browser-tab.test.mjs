import { test } from 'node:test';
import assert from 'node:assert/strict';
import { setTabAttention, setTabTitle, tabIcon, tabTitle } from '../../agent_board/static/js/browser-tab.js';

test('tabTitle: число впереди только когда что-то ждёт', () => {
  assert.equal(tabTitle('Перенос платежей — agent-board', 2), '(2) Перенос платежей — agent-board');
  assert.equal(tabTitle('Проекты — agent-board', 0), 'Проекты — agent-board');
});

test('tabIcon: точка только при ненулевом числе', () => {
  assert.equal(tabIcon(0), '/static/favicon.svg');
  assert.equal(tabIcon(3), '/static/favicon-attn.svg');
});

test('setTabTitle и setTabAttention: повторная установка заголовка не удваивает число, сброс убирает точку', () => {
  // arrange
  const link = { href: '/static/favicon.svg', getAttribute() { return this.href; }, setAttribute(_, v) { this.href = v; } };
  globalThis.document = { title: '', querySelector: () => link };

  // act
  setTabTitle('Доска — agent-board');
  setTabAttention(2);
  setTabTitle('Доска — agent-board');
  const busy = { title: document.title, icon: link.href };
  setTabAttention(0);

  // assert
  assert.deepEqual(busy, { title: '(2) Доска — agent-board', icon: '/static/favicon-attn.svg' });
  assert.deepEqual({ title: document.title, icon: link.href }, { title: 'Доска — agent-board', icon: '/static/favicon.svg' });
});
