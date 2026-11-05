import { test } from 'node:test';
import assert from 'node:assert/strict';
import { errorField, suggestKey, taskPayload, validateNewTask } from '../../agent_board/static/js/newtask.js';

test('suggestKey: самый частый префикс проекта и следующий номер', () => {
  // act + assert
  assert.equal(suggestKey(['PAY-101', 'PAY-114', 'PAY-9', 'OPS-500'], 'pay'), 'PAY-115');
  assert.equal(suggestKey(['OPS-1', 'OPS-2', 'PAY-30'], 'pay'), 'OPS-3');
  assert.equal(suggestKey(['T007', 'T012'], 'x'), 'T013');
  assert.equal(suggestKey(['T007', 'T008'], 'x'), 'T009', 'ведущие нули сохраняются');
});

test('suggestKey: без задач с номерами — префикс из slug проекта', () => {
  // act + assert
  assert.equal(suggestKey([], 'pay'), 'PAY-1');
  assert.equal(suggestKey(['readme'], 'my_proj'), 'MY_PROJ-1');
});

test('validateNewTask: ключ, заголовок и оценка проверяются до отправки', () => {
  // arrange
  const keys = ['PAY-101'];
  const ok = { key: 'PAY-102', title: 'Задача', estimate: '1,5' };
  // act + assert
  assert.deepEqual(validateNewTask(ok, keys), {});
  assert.deepEqual(validateNewTask({ key: ' ', title: '', estimate: '' }, keys), { key: 'Укажите ключ', title: 'Укажите заголовок' });
  assert.deepEqual(validateNewTask({ key: 'pay-101', title: 'x' }, keys), { key: 'Задача PAY-101 уже есть' });
  assert.deepEqual(validateNewTask({ key: '_x', title: 'x' }, keys), { key: 'Только латиница, цифры, «.», «_» и «-»; не с «_» или «.»' });
  assert.deepEqual(validateNewTask({ key: 'a..b', title: 'x' }, keys).key !== undefined, true);
  assert.deepEqual(validateNewTask({ key: 'Я-1', title: 'x' }, keys).key !== undefined, true);
  assert.deepEqual(validateNewTask({ key: 'A-1', title: 'x', estimate: '-1' }, keys), { estimate: 'Оценка — число дней, не меньше нуля' });
  assert.deepEqual(validateNewTask({ key: 'A-1', title: 'x', estimate: 'два' }, keys), { estimate: 'Оценка — число дней, не меньше нуля' });
});

test('taskPayload: поля задачи для сервера, пустая оценка не передаётся', () => {
  // arrange
  const form = { key: ' PAY-102 ', title: ' Задача ', description: 'd', done_when: '', kind: 'research', repo: '', estimate: '1,5', deps: ['PAY-101'] };
  // act
  const p = taskPayload(form);
  const noEst = taskPayload({ ...form, estimate: ' ' });
  // assert
  assert.deepEqual(p, { key: 'PAY-102', title: 'Задача', description: 'd', done_when: '', kind: 'research', repo: '', estimate: 1.5, deps: ['PAY-101'] });
  assert.equal('estimate' in noEst, false);
});

test('errorField: ошибка сервера показывается под своим полем', () => {
  // act + assert
  assert.equal(errorField('Задача уже существует: PAY-1'), 'key');
  assert.equal(errorField("Недопустимый идентификатор (задача): 'a b'"), 'key');
  assert.equal(errorField('Ключ задачи не может начинаться с «_»: _x'), 'key');
  assert.equal(errorField('Зависимость от неизвестной задачи: PAY-9'), 'deps');
  assert.equal(errorField('Зависимости образуют цикл: A → B'), 'deps');
  assert.equal(errorField('Оценка задачи должна быть числом'), 'estimate');
  assert.equal(errorField('Задачи меняет только пользователь'), null);
});

test('suggestKey: номер растёт, пока ключ совпадает с существующим без учёта регистра', () => {
  // act + assert
  assert.equal(suggestKey(['PAY-1', 'PAY-2', 'pay-3'], 'pay'), 'PAY-4');
  assert.equal(suggestKey(['PAY-1', 'PAY-2', 'pay-3', 'Pay-4'], 'pay'), 'PAY-5');
});

test('blocksUpdates: выбранные задачи получают новую задачу в зависимости, повтор не дублируется', async () => {
  // arrange
  const { blocksUpdates } = await import('../../agent_board/static/js/newtask.js');
  const tasks = [{ key: 'A', deps: ['X'] }, { key: 'B', deps: [] }, { key: 'C', deps: ['N'] }];
  // act
  const updates = blocksUpdates(tasks, ['B', 'A', 'C', 'Z'], 'N');
  // assert
  assert.deepEqual(updates, [{ key: 'A', deps: ['X', 'N'] }, { key: 'B', deps: ['N'] }]);
});

test('validateNewTask: одна задача не может быть и зависимостью, и блокируемой', () => {
  // act
  const errors = validateNewTask({ key: 'N', title: 'x', deps: ['A', 'B'], blocks: ['B'] }, ['A', 'B']);
  // assert
  assert.deepEqual(errors, { blocks: 'B и зависимость, и блокируется новой задачей — получится цикл' });
});

test('applyBlocks: зависимости берутся из свежего снимка, ошибка одной задачи не мешает остальным', async () => {
  // arrange: пока окно было открыто, у B появилась зависимость Y
  const { applyBlocks } = await import('../../agent_board/static/js/newtask.js');
  const posted = [];
  const client = {
    get: async path => (path === '/state' ? { tasks: [{ key: 'A', deps: ['X'] }, { key: 'B', deps: ['Y'] }, { key: 'C', deps: [] }] } : null),
    post: async (path, body) => {
      posted.push([path, body]);
      if (path === '/tasks/A/deps') throw new Error('цикл');
    },
  };
  // act
  const failed = await applyBlocks(client, 'N', ['A', 'B', 'C']);
  // assert
  assert.deepEqual(posted, [
    ['/tasks/A/deps', { deps: ['X', 'N'], actor: 'user' }],
    ['/tasks/B/deps', { deps: ['Y', 'N'], actor: 'user' }],
    ['/tasks/C/deps', { deps: ['N'], actor: 'user' }],
  ]);
  assert.deepEqual(failed, [{ key: 'A', error: 'цикл' }]);
});

test('applyBlocks: снимок не прочитался — ошибка у каждой выбранной задачи', async () => {
  // arrange
  const { applyBlocks } = await import('../../agent_board/static/js/newtask.js');
  const client = { get: async () => { throw new Error('нет связи'); }, post: async () => assert.fail('не должно отправляться') };
  // act
  const failed = await applyBlocks(client, 'N', ['B', 'A']);
  // assert
  assert.deepEqual(failed, [{ key: 'A', error: 'нет связи' }, { key: 'B', error: 'нет связи' }]);
});
