import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseRoute, withTask } from '../../agent_board/static/js/router.js';

test('parseRoute разбирает маршруты', () => {
  assert.deepEqual(parseRoute(''), { name: 'projects' });
  assert.deepEqual(parseRoute('#/'), { name: 'projects' });
  assert.deepEqual(parseRoute('#/p/pay'), { name: 'board', slug: 'pay' });
  assert.deepEqual(parseRoute('#/p/pay/'), { name: 'board', slug: 'pay' });
  assert.deepEqual(parseRoute('#/p/pay/decomposition'), { name: 'decomposition', slug: 'pay' });
  assert.deepEqual(parseRoute('#/p/%D0%BF%D1%80'), { name: 'board', slug: 'пр' });
  assert.deepEqual(parseRoute('#/p/pay/zzz'), { name: 'not_found' });
  assert.deepEqual(parseRoute('#/zzz'), { name: 'not_found' });
  assert.deepEqual(parseRoute('#/p/%E0%A4%A'), { name: 'not_found' });
});

test('parseRoute читает открытую карточку из ?task=', () => {
  assert.deepEqual(parseRoute('#/p/pay?task=PAY-105'), { name: 'board', slug: 'pay', task: 'PAY-105' });
  assert.deepEqual(parseRoute('#/p/pay/decomposition?task=PAY-1'), { name: 'decomposition', slug: 'pay', task: 'PAY-1' });
  assert.deepEqual(parseRoute('#/p/pay?task='), { name: 'board', slug: 'pay' });
  assert.deepEqual(parseRoute('#/p/pay?other=1'), { name: 'board', slug: 'pay' });
  assert.deepEqual(parseRoute('#/p/pay?task=%D0%97-1'), { name: 'board', slug: 'pay', task: 'З-1' });
});

test('withTask добавляет и убирает карточку в адресе, сохраняя вид', () => {
  assert.equal(withTask('#/p/pay', 'PAY-1'), '#/p/pay?task=PAY-1');
  assert.equal(withTask('#/p/pay?task=PAY-1', 'PAY-2'), '#/p/pay?task=PAY-2');
  assert.equal(withTask('#/p/pay/decomposition?task=PAY-1', null), '#/p/pay/decomposition');
  assert.equal(withTask('#/p/pay', 'A B'), '#/p/pay?task=A%20B');
});
