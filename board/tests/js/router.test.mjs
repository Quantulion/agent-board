import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseRoute } from '../../agent_board/static/js/router.js';

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
