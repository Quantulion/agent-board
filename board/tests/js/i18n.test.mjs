import { test } from 'node:test';
import assert from 'node:assert/strict';
import { plural, statusInfo, tasksCount } from '../../agent_board/static/js/i18n.js';

test('plural склоняет по числу', () => {
  const forms = ['задача', 'задачи', 'задач'];
  assert.deepEqual([0, 1, 2, 4, 5, 11, 12, 21, 22, 25, 101, 111].map(n => plural(n, forms)),
    ['задач', 'задача', 'задачи', 'задачи', 'задач', 'задач', 'задач', 'задача', 'задачи', 'задач', 'задача', 'задач']);
  assert.equal(tasksCount(3), '3 задачи');
});

test('statusInfo знает статусы и не падает на неизвестном', () => {
  assert.deepEqual(statusInfo('in_review'), { id: 'in_review', label: 'На ревью', color: '--s-review' });
  assert.equal(statusInfo('zzz').label, 'zzz');
});
