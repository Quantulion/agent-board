import { test } from 'node:test';
import assert from 'node:assert/strict';
import { cardModel, columnsOf, connectionIndicator, reactionText, stepperModel, waitingForYou, whyBlocked } from '../../agent_board/static/js/board.js';

function task(key, extra = {}) {
  const status = extra.status || 'not_started';
  return {
    key, title: 'Задача ' + key, repo: 'api', kind: 'code', estimate: 1, deps: [], status,
    display_status: extra.display_status || status, open_deps: [], external_blocker: null,
    open_questions: 0, unread: 0, agent_state: 'idle', turn_error: '', rule_violations: [],
    last_message: null, locks: [], base_branch: null, summary: '', ...extra,
  };
}

function snap(tasks, extra = {}) {
  return {
    config: { title: 'Проект', stage: 'work', repos: [{ name: 'api', base: 'main' }], links: [], chains: true, ...(extra.config || {}) },
    tasks, proposals: extra.proposals || [], project_agent: extra.project_agent || { state: 'idle', turn_error: '' },
    project_unread: 0, last_seq: 1,
  };
}

const LOOP = 'Агенты переписываются без участия пользователя — остановлено; ответьте в треде, чтобы продолжить';

test('columnsOf раскладывает задачи по display_status во все колонки в порядке статусов', () => {
  const s = snap([
    task('A-2', { status: 'in_progress' }),
    task('A-10', { status: 'in_progress' }),
    task('A-3', { display_status: 'blocked', open_deps: ['A-2'] }),
    task('A-4', { status: 'cancelled' }),
  ]);
  const cols = columnsOf(s, {});
  assert.deepEqual(cols.map(c => c.id), ['not_started', 'blocked', 'in_progress', 'waiting_answer', 'queued',
    'in_review', 'approved', 'sent', 'done', 'cancelled']);
  const by = Object.fromEntries(cols.map(c => [c.id, c.tasks.map(t => t.key)]));
  assert.deepEqual(by.in_progress, ['A-2', 'A-10'], 'ключи сортируются по номеру');
  assert.deepEqual(by.blocked, ['A-3']);
  assert.deepEqual(by.not_started, []);
  assert.deepEqual(by.cancelled, ['A-4']);
  assert.equal(cols.find(c => c.id === 'in_progress').total, 2);
});

test('columnsOf применяет фильтры репозитория, типа, поиска и «требующие меня»', () => {
  const s = snap([
    task('PAY-1', { repo: 'api', kind: 'code', title: 'Схема PG' }),
    task('PAY-2', { repo: 'worker', kind: 'research', title: 'Нагрузка' }),
    task('PAY-3', { repo: 'api', kind: 'research', title: 'Отчёт', open_questions: 1 }),
  ]);
  const keys = f => columnsOf(s, f).flatMap(c => c.tasks.map(t => t.key));
  assert.deepEqual(keys({ repo: 'api' }), ['PAY-1', 'PAY-3']);
  assert.deepEqual(keys({ type: 'research' }), ['PAY-2', 'PAY-3']);
  assert.deepEqual(keys({ q: 'схема' }), ['PAY-1'], 'поиск по заголовку без учёта регистра');
  assert.deepEqual(keys({ q: 'pay-2' }), ['PAY-2'], 'поиск по ключу');
  assert.deepEqual(keys({ attn: true }), ['PAY-3']);
  assert.deepEqual(keys({ repo: 'api', type: 'code', q: '' }), ['PAY-1']);
  const ns = columnsOf(s, { repo: 'api' }).find(c => c.id === 'not_started');
  assert.equal(ns.total, 3, 'total — без фильтров');
});

test('columnsOf без снимка возвращает пустые колонки', () => {
  const cols = columnsOf(null, {});
  assert.equal(cols.length, 10);
  assert.ok(cols.every(c => c.tasks.length === 0));
});

test('waitingForYou собирает все виды пунктов', () => {
  const s = snap([
    task('P-1', { open_questions: 2, status: 'waiting_answer' }),
    task('P-2', { status: 'in_review' }),
    task('P-3', { agent_state: 'error', turn_error: 'упал', status: 'in_progress' }),
    task('P-4', { status: 'in_progress', agent_parked: true }),
    task('P-5', { rule_violations: [{ rule: 'r1', text: 'слишком крупная' }] }),
    task('P-6', { status: 'cancelled', open_questions: 1 }),
    task('P-7', { status: 'in_progress', last_message: { author: 'system', text: LOOP, at: '' } }),
    task('P-8', { display_status: 'blocked', external_blocker: 'ждём доступ от DBA' }),
  ], {
    proposals: [
      { id: 'p1', kind: 'add_task', payload: { task: { key: 'P-9' } }, reason: '', status: 'open' },
      { id: 'p2', kind: 'set_deps', payload: { key: 'P-2', deps: [] }, reason: '', status: 'rejected' },
      { id: 'p3', kind: 'cancel_task', payload: { key: 'P-1' }, reason: '', status: 'open' },
    ],
    project_agent: { state: 'error', turn_error: 'нет харнесса', parked: true },
  });
  const items = waitingForYou(s);
  assert.deepEqual(items.map(i => [i.kind, i.key || null]), [
    ['question', 'P-1'],
    ['proposal', 'P-9'],
    ['proposal', 'P-1'],
    ['review', 'P-2'],
    ['error', 'P-3'],
    ['error', null],
    ['loop', 'P-4'],
    ['loop', null],
    ['blocker', 'P-8'],
  ], 'нарушения правил — только на стадии разбивки; отменённые не ждут; остановку видно по флагу, а не по тексту');
  assert.deepEqual(items[7].open, { type: 'project' });
  assert.equal(items[8].text, 'ждём доступ от DBA');
  assert.deepEqual(items[8].open, { type: 'card', key: 'P-8' });
  assert.equal(items[0].count, 2);
  assert.deepEqual(items[1].open, { type: 'project' });
  assert.deepEqual(items[0].open, { type: 'card', key: 'P-1' });
  assert.deepEqual(items[5].open, { type: 'project' });
  assert.equal(items[1].proposal, 'add_task');
});

test('waitingForYou не зовёт к одобренным, отправленным и готовым задачам с вопросами и перепиской', () => {
  const s = snap(['approved', 'sent', 'done'].map((status, i) =>
    task('D-' + i, { status, open_questions: 1, agent_parked: true, external_blocker: 'x' })));
  assert.deepEqual(waitingForYou(s), []);
});

test('waitingForYou на стадии разбивки показывает нарушения правил', () => {
  const s = snap([task('P-5', { rule_violations: [{ rule: 'r1', text: 'x' }, { rule: 'r2', text: 'y' }] })],
    { config: { stage: 'decomposition' } });
  const items = waitingForYou(s);
  assert.deepEqual(items.map(i => [i.kind, i.key, i.count]), [['violation', 'P-5', 2]]);
  assert.deepEqual(items[0].open, { type: 'decomposition', key: 'P-5' }, 'нарушение ведёт к задаче на странице разбивки');
  assert.deepEqual(waitingForYou(null), []);
});

test('whyBlocked: открытые зависимости со статусами и внешний блокер', () => {
  const tasks = [
    task('B-1', { status: 'in_progress' }),
    task('B-2', { display_status: 'blocked', open_deps: ['B-3'] }),
    task('B-3', { status: 'not_started' }),
  ];
  const byKey = new Map(tasks.map(t => [t.key, t]));
  const t = task('B-9', { display_status: 'blocked', open_deps: ['B-1', 'B-2', 'B-404'], external_blocker: 'ждём DBA' });
  assert.deepEqual(whyBlocked(t, byKey), {
    deps: [{ key: 'B-1', status: 'in_progress' }, { key: 'B-2', status: 'blocked' }, { key: 'B-404', status: null }],
    external: 'ждём DBA',
  });
  assert.equal(whyBlocked(task('B-8'), byKey), null, 'нечего показывать');
  assert.deepEqual(whyBlocked(task('B-7', { external_blocker: '  ' , open_deps: ['B-1'] }), byKey).external, null);
});

test('reactionText описывает переход и реакцию сервера', () => {
  const ev = (type, data) => ({ seq: 1, type, at: '', data });
  assert.equal(reactionText([
    ev('status_changed', { key: 'A-1', from: 'in_review', to: 'approved', actor: 'user' }),
    ev('task_unblocked', { key: 'A-2' }),
    ev('agent_wake', { key: 'A-2', reason: 'unblocked' }),
    ev('task_unblocked', { key: 'A-3' }),
    ev('agent_wake', { key: 'A-3', reason: 'unblocked' }),
    ev('agent_wake', { key: 'A-4', reason: 'rebase', from: 'A-1' }),
  ]), 'A-1 → Одобрена. Разблокированы A-2, A-3. Разбужены агенты A-2, A-3, A-4.');
  assert.equal(reactionText([
    ev('status_changed', { key: 'A-1', from: 'in_review', to: 'in_progress', actor: 'user' }),
    ev('agent_wake', { key: 'A-1', reason: 'returned', from: 'in_review' }),
  ]), 'A-1 → В работе. Разбужен агент A-1.');
  assert.equal(reactionText([
    ev('status_changed', { key: 'A-1', from: 'in_progress', to: 'cancelled', actor: 'user' }),
    ev('agent_stop', { key: 'A-1' }),
    ev('deps_changed', { key: 'A-5', deps: [] }),
    ev('task_unblocked', { key: 'A-5' }),
    ev('agent_wake', { key: 'A-5', reason: 'unblocked' }),
  ]), 'A-1 → Отменена. Остановлен агент A-1. Убрана из зависимостей A-5. Разблокирована A-5. Разбужен агент A-5.');
  assert.equal(reactionText([ev('status_changed', { key: 'A-1', from: 'approved', to: 'done', actor: 'user' })]),
    'A-1 → Готово. Других изменений нет.');
  assert.equal(reactionText([]), 'Статус не изменился.');
});

test('cardModel собирает очередь, агента, цепочку и чипы', () => {
  const s = snap([
    task('C-1', { status: 'in_progress' }),
    task('C-2', {
      status: 'queued', locks: [{ resource: 'функтесты', position: 2 }], open_questions: 1, unread: 3,
      base_branch: 'C-1', agent_state: 'running', summary: 'Пишет переливку\nвторая строка',
    }),
    task('C-3', { agent_state: 'error', turn_error: 'boom', base_branch: 'main' }),
  ], { proposals: [{ id: 'p1', kind: 'set_deps', payload: { key: 'C-2', deps: [] }, reason: '', status: 'open' }] });
  const byKey = new Map(s.tasks.map(t => [t.key, t]));
  const ctx = { byKey, proposalKeys: new Set(['C-2']), repoBase: new Map([['api', 'main']]) };
  const m = cardModel(s.tasks[1], ctx);
  assert.deepEqual(m.queue, { resource: 'функтесты', position: 2 });
  assert.deepEqual(m.agent, { live: true, text: 'Пишет переливку' });
  assert.equal(m.chain, 'C-1');
  assert.equal(m.questions, 1);
  assert.equal(m.unread, 3);
  assert.equal(m.proposal, true);
  assert.equal(m.error, null);
  const e = cardModel(s.tasks[2], ctx);
  assert.equal(e.error, 'boom');
  assert.equal(e.chain, null, 'ветка от базы репозитория — не цепочка');
  assert.equal(e.proposal, false);
});

test('stepperModel: пройденные стадии, текущая и пропущенный трекер', () => {
  const states = (stage, tracker) => stepperModel(stage, tracker).map(s => s.id + ':' + s.state).join(' ');
  assert.equal(states('decomposition', 'jira'), 'decomposition:cur tracker:todo work:todo done:todo');
  assert.equal(states('tracker', 'jira'), 'decomposition:done tracker:cur work:todo done:todo');
  assert.equal(states('work', 'jira'), 'decomposition:done tracker:done work:cur done:todo');
  assert.equal(states('decomposition', 'none'), 'decomposition:cur tracker:skipped work:todo done:todo');
  assert.equal(states('work', 'none'), 'decomposition:done tracker:skipped work:cur done:todo');
  assert.equal(states('done', 'none'), 'decomposition:done tracker:skipped work:done done:done');
  assert.equal(states('work', undefined), 'decomposition:done tracker:skipped work:cur done:todo', 'без трекера в конфиге — тоже пропуск');
  assert.equal(states('tracker', 'none'), 'decomposition:done tracker:cur work:todo done:todo', 'текущая стадия не пропущена');
});

test('connectionIndicator: при отключённом потоке «Переподключение…» важнее ошибки снимка', () => {
  const down = connectionIndicator({ connection: 'reconnecting', error: new Error('нет связи') });
  assert.equal(down.text, 'Переподключение…');
  assert.equal(down.failed, false);
  assert.equal(down.busy, true);
});

test('connectionIndicator: ошибка снимка при живом потоке и обычные состояния', () => {
  assert.deepEqual(connectionIndicator({ connection: 'live', error: null }), { failed: false, busy: false, text: 'В сети' });
  const err = connectionIndicator({ connection: 'live', error: new Error('x') });
  assert.equal(err.text, 'Ошибка загрузки');
  assert.equal(err.failed, true);
  assert.equal(connectionIndicator({ connection: 'offline', error: new Error('x') }).failed, true);
});
