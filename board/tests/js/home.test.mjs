import { test } from 'node:test';
import assert from 'node:assert/strict';
import { homeModel, projectCardModel, projectColumn } from '../../agent_board/static/js/home.js';
import { setLanguage } from '../../agent_board/static/js/i18n.js';

setLanguage('ru');

function task(key, extra = {}) {
  const status = extra.status || 'not_started';
  return {
    key, title: 'Задача ' + key, repo: 'api', kind: 'code', deps: [], status,
    display_status: extra.display_status || status, open_deps: [], external_blocker: null,
    open_questions: 0, unread: 0, agent_state: 'idle', turn_error: '', rule_violations: [],
    last_message: null, locks: [], base_branch: null, summary: '', ...extra,
  };
}

function snap(slug, tasks = [], extra = {}) {
  return {
    config: {
      slug, title: 'Проект ' + slug, kind: 'free', stage: 'work', archived: false,
      repos: [], links: [], chains: true, ...(extra.config || {}),
    },
    tasks, proposals: extra.proposals || [], project_agent: extra.project_agent || { state: 'idle', turn_error: '' },
    project_unread: extra.project_unread || 0, last_seq: 1,
  };
}

test('projectColumn: вопрос агента — «Ждут вас»', () => {
  const s = snap('fix', [task('FIX-1', { status: 'waiting_answer', open_questions: 1, agent_state: 'running' })]);
  assert.equal(projectColumn(s), 'waiting');
});

test('projectColumn: только работающий агент — «Агенты работают»', () => {
  assert.equal(projectColumn(snap('fix', [task('FIX-1', { status: 'in_progress', agent_state: 'running' })])), 'working');
  assert.equal(projectColumn(snap('fix', [task('FIX-1', { status: 'in_progress', agent_state: 'waiting_turn' })])), 'working',
    'ожидание слота — тоже работа');
  assert.equal(projectColumn(snap('fix', [], { project_agent: { state: 'running', turn_error: '' } })), 'working',
    'агент проекта считается');
});

test('projectColumn: ничего не происходит — «Тихо»', () => {
  assert.equal(projectColumn(snap('fix')), 'quiet');
  assert.equal(projectColumn(snap('fix', [task('FIX-1', { status: 'done' })])), 'quiet');
});

test('projectColumn: архивный проект с вопросом остаётся в архиве, пункт — в общей полосе', () => {
  const archived = snap('old', [task('OLD-1', { status: 'waiting_answer', open_questions: 1 })], { config: { archived: true } });
  assert.equal(projectColumn(archived), 'archive');
  const m = homeModel([{ slug: 'old', snapshot: archived }, { slug: 'fix', snapshot: snap('fix') }]);
  assert.deepEqual(m.columns.archive.map(p => p.slug), ['old']);
  assert.deepEqual(m.columns.waiting, []);
  assert.deepEqual(m.waiting.map(i => [i.slug, i.kind, i.key]), [['old', 'question', 'OLD-1']]);
  assert.equal(m.archiveAttention, 1);
});

test('projectColumn: проект по плану на стадии завершения — в архиве', () => {
  assert.equal(projectColumn(snap('pay', [], { config: { kind: 'plan', stage: 'done' } })), 'archive');
  assert.equal(projectColumn(snap('pay', [], { config: { kind: 'plan', stage: 'work' } })), 'quiet');
});

test('projectColumn: непрочитанный ответ агента проекта — «Ждут вас»', () => {
  assert.equal(projectColumn(snap('fix', [], { project_unread: 1 })), 'waiting');
});

test('homeModel: общая полоса с проектом у каждого пункта, лимит в шапке — из настроек доски', () => {
  // arrange
  const a = snap('fix', [
    task('FIX-1', { status: 'in_progress', agent_state: 'running' }),
    task('FIX-2', { status: 'in_progress', agent_state: 'waiting_turn' }),
    task('FIX-3', { status: 'in_review' }),
  ]);
  const b = snap('pay', [task('PAY-1', { status: 'in_progress', agent_state: 'running' })],
    { config: { kind: 'plan' }, project_agent: { state: 'running', turn_error: '' } });
  const c = snap('old', [task('OLD-1', { status: 'in_progress', agent_state: 'running' })], { config: { archived: true } });
  // act
  const m = homeModel([{ slug: 'fix', snapshot: a }, { slug: 'pay', snapshot: b }, { slug: 'old', snapshot: c }],
    { language: 'ru', global_max_agents: 15 });
  // assert
  assert.deepEqual(m.columns.waiting.map(p => p.slug), ['fix']);
  assert.deepEqual(m.columns.working.map(p => p.slug), ['pay']);
  assert.deepEqual(m.waiting.map(i => [i.slug, i.project, i.kind, i.key]), [['fix', 'FIX', 'review', 'FIX-3']]);
  assert.deepEqual(m.header, { active: 2, running: 4, limit: 15, queued: 1 });
  assert.equal(m.archiveAttention, 0);
});

test('homeModel: проект, чей снимок не прочитался, остаётся на экране в «Тихо»', () => {
  const m = homeModel([{ slug: 'broken', snapshot: null }]);
  assert.deepEqual(m.columns.quiet.map(p => p.slug), ['broken']);
  assert.deepEqual(m.header, { active: 1, running: 0, limit: 0, queued: 0 });
});

test('homeModel: лимит проекта в снимке не влияет на шапку, без настроек доски лимита нет', () => {
  // arrange
  const s = snap('fix', [], { config: { global_max_agents: 40 } });
  // act
  const withSettings = homeModel([{ slug: 'fix', snapshot: s }], { global_max_agents: 4 });
  const noSettings = homeModel([{ slug: 'fix', snapshot: s }]);
  // assert
  assert.equal(withSettings.header.limit, 4);
  assert.equal(noSettings.header.limit, 0);
});

test('projectCardModel: проект без плана — без полоски прогресса, счётчики, чипы и репозитории', () => {
  const s = snap('fix', [
    task('FIX-1', { status: 'in_progress', agent_state: 'running' }),
    task('FIX-2', { status: 'waiting_answer', open_questions: 2, agent_state: 'running' }),
    task('FIX-3', { status: 'in_review' }),
    task('FIX-4', { status: 'done' }),
    task('FIX-5', { status: 'in_progress', agent_state: 'error', turn_error: 'упал' }),
    task('FIX-6', { status: 'in_progress', agent_state: 'waiting_turn' }),
  ], {
    config: { repos: [{ name: 'billing-api' }, { name: 'monolith' }, { name: 'a' }, { name: 'b' }] },
    proposals: [{ id: 'p1', kind: 'add_task', payload: { task: { key: 'FIX-9' } }, status: 'open' }],
  });
  const m = projectCardModel(s);
  assert.equal(m.key, 'FIX');
  assert.equal(m.kind, 'free');
  assert.equal(m.stage, null);
  assert.equal(m.progress, null);
  assert.deepEqual(m.agents, { running: 2, queued: 1 });
  assert.deepEqual(m.counts, { working: 4, review: 1, done: 1 });
  assert.deepEqual(m.chips, { question: 2, review: 1, error: 1, proposal: 1 });
  assert.deepEqual(m.repos, { shown: ['billing-api', 'monolith'], more: 2 });
});

test('projectCardModel: проект по плану — стадия и «X из Y готово» без отменённых', () => {
  const s = snap('pay', [
    task('PAY-1', { status: 'done' }),
    task('PAY-2', { status: 'done' }),
    task('PAY-3', { status: 'in_progress' }),
    task('PAY-4', { display_status: 'blocked' }),
    task('PAY-5', { status: 'cancelled' }),
  ], { config: { kind: 'plan', stage: 'work' } });
  const m = projectCardModel(s);
  assert.equal(m.kind, 'plan');
  assert.equal(m.stage, 'Работа');
  assert.deepEqual(m.progress.done, 2);
  assert.deepEqual(m.progress.total, 4);
  assert.deepEqual(m.progress.segments, [{ id: 'done', n: 2 }, { id: 'in_progress', n: 1 }, { id: 'blocked', n: 1 }]);
});

test('projectCardModel: ключ проекта — префикс его задач, без задач — slug', () => {
  assert.equal(projectCardModel(snap('agent-board', [task('AB-1'), task('AB-2')])).key, 'AB');
  assert.equal(projectCardModel(snap('fix2')).key, 'FIX2');
});

test('projectCardModel: проект без поля типа считается проектом по плану', () => {
  const s = snap('old');
  delete s.config.kind;
  assert.equal(projectCardModel(s).kind, 'plan');
});
