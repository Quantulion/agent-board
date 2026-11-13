import { test } from 'node:test';
import assert from 'node:assert/strict';
import { installFakeDocument } from './fake-dom.mjs';
import { setLanguage } from '../../agent_board/static/js/i18n.js';
import { overviewPane, questionCounter } from '../../agent_board/static/js/overview.js';

installFakeDocument();
setLanguage('ru');

test('questionCounter: один вопрос — без счётчика, несколько — «N из M»', () => {
  assert.equal(questionCounter(0, 1), '');
  assert.equal(questionCounter(0, 2), '1 из 2');
  assert.equal(questionCounter(2, 3), '3 из 3');
  setLanguage('en');
  assert.equal(questionCounter(1, 2), '2 of 2');
  setLanguage('ru');
});

test('overviewPane: после ответа на один из двух вопросов номер у оставшегося пропадает', () => {
  // Arrange
  const pane = overviewPane({ key: 'A-1', ctx: {}, client: {}, slug: 'p', drafts: { get: () => '', set() {} } });
  const first = { id: 'q1', text: 'Первый?' };
  const second = { id: 'q2', text: 'Второй?' };
  const paint = questions => {
    const t = { key: 'A-1', questions };
    pane.paint(t, { tasks: [t], proposals: [], config: {} });
  };
  const counters = () => pane.el.querySelectorAll('span')
    .filter(s => s.getAttribute('class') === 'qn').map(s => s.textContent);

  // Act
  paint([first, second]);
  const before = counters();
  paint([{ ...first, answer: { choice: null, comment: 'да' } }, second]);

  // Assert
  assert.deepEqual(before, ['1 из 2', '2 из 2']);
  assert.deepEqual(counters(), ['']);
});
