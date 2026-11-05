import { test } from 'node:test';
import assert from 'node:assert/strict';
import { decompositionSummary, nextStage, repoColors } from '../../agent_board/static/js/decomposition.js';

const t = (key, extra = {}) => ({
  key, title: key, deps: [], status: 'not_started', display_status: 'not_started', open_deps: [], estimate: 1,
  repo: 'api', kind: 'code', done_when: 'ok', rule_violations: [], thread_size: 0, ...extra,
});
const snap = (tasks, config = {}) => ({ config: { stage: 'decomposition', tracker: { type: 'none' }, repos: [], ...config }, tasks, proposals: [] });

test('nextStage: с трекером разбивка уходит в tracker, без него — сразу в work', () => {
  // act + assert
  assert.equal(nextStage({ tracker: { type: 'none' } }), 'work');
  assert.equal(nextStage({}), 'work');
  assert.equal(nextStage({ tracker: { type: 'jira' } }), 'tracker');
});

test('decompositionSummary: нарушения собраны по задачам, отменённые задачи не считаются', () => {
  // arrange
  const tasks = [
    t('T2', { rule_violations: [{ rule: 'Один деплой', text: 'два сервиса' }, { rule: 'Не больше 3 дней', text: '5 д' }] }),
    t('T10', { rule_violations: [{ rule: 'Один деплой', text: 'и тут' }] }),
    t('T3', { status: 'cancelled', display_status: 'cancelled', rule_violations: [{ rule: 'Один деплой', text: 'x' }] }),
    t('T1'),
  ];
  // act
  const s = decompositionSummary(snap(tasks));
  // assert
  assert.deepEqual(s.tasks.map(x => x.key), ['T1', 'T2', 'T10']);
  assert.deepEqual(s.violations, [
    { key: 'T2', rule: 'Один деплой', text: 'два сервиса' },
    { key: 'T2', rule: 'Не больше 3 дней', text: '5 д' },
    { key: 'T10', rule: 'Один деплой', text: 'и тут' },
  ]);
  assert.equal('byRule' in s, false, 'группировка по правилам странице не нужна');
});

test('decompositionSummary: оценка, критический путь, шаги и параллельность, задачи без критерия готовности', () => {
  // arrange: A → B → D, A → C, E отдельно
  const tasks = [
    t('A', { estimate: 2 }), t('B', { deps: ['A'], estimate: 3, open_deps: ['A'] }), t('C', { deps: ['A'], done_when: ' ', open_deps: ['A'] }),
    t('D', { deps: ['B'], estimate: null, open_deps: ['B'], repo: 'web', thread_size: 2 }), t('E', { estimate: 0.5, thread_size: 3 }),
  ];
  // act
  const s = decompositionSummary(snap(tasks));
  // assert
  assert.equal(s.estimate, 2 + 3 + 1 + 0.5, 'задача без оценки не добавляет дней');
  assert.deepEqual(s.critical, { path: ['A', 'B', 'D'], total: 6 });
  assert.equal(s.steps, 3);
  assert.equal(s.parallel, 2);
  assert.deepEqual(s.missingDod, ['C']);
  assert.deepEqual(s.repos, ['api', 'web']);
  assert.equal(s.messages, 5);
  assert.deepEqual(s.startKeys, ['A', 'E']);
  assert.equal(s.readOnly, false);
  assert.equal(s.next, 'work');
});

test('decompositionSummary: после утверждения страница только для чтения', () => {
  // act
  const s = decompositionSummary(snap([t('A')], { stage: 'work' }));
  // assert
  assert.equal(s.readOnly, true);
});

test('decompositionSummary: внешний блокер не даёт задаче стартовать первой', () => {
  // act
  const s = decompositionSummary(snap([t('A', { external_blocker: 'ждём DBA' }), t('B')]));
  // assert
  assert.deepEqual(s.startKeys, ['B']);
});

test('repoColors: цвета по порядку репозиториев проекта, затем — задач; палитра повторяется', () => {
  // act
  const colors = repoColors(['api', 'worker'], [t('X', { repo: 'reports' }), t('Y', { repo: 'api' }), t('Z', { repo: '' })]);
  // assert
  assert.deepEqual([...colors.keys()], ['api', 'worker', 'reports']);
  assert.equal(colors.get('api'), 'var(--s-progress)');
  assert.notEqual(colors.get('worker'), colors.get('api'));
});
