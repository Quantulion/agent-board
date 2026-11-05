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
