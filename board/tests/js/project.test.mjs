import { test } from 'node:test';
import assert from 'node:assert/strict';
import { agentStateLine, describeFor, describeProposal, partsText, proposalOutcome, proposalSignature, proposalsTouching } from '../../agent_board/static/js/project.js';

const tasks = new Map([
  ['PAY-104', { key: 'PAY-104', title: 'Переливка истории', status: 'in_progress' }],
  ['PAY-107', { key: 'PAY-107', title: 'Метрики PG', status: 'not_started', deps: ['PAY-104'] }],
  ['PAY-109', { key: 'PAY-109', title: 'Выгрузка для бухгалтерии', status: 'not_started', deps: [] }],
  ['PAY-111', { key: 'PAY-111', title: 'Переключить чтение', status: 'blocked', deps: ['PAY-109', 'PAY-104'] }],
]);
const text = d => [partsText(d.title), ...d.changes.map(c => c.label + ' ' + partsText(c.parts))];

test('describeProposal: новая задача — ключ, заголовок, репозиторий, тип, оценка и зависимости', () => {
  // arrange
  const p = { id: 'p1', kind: 'add_task', status: 'open', payload: { task: {
    key: 'PAY-115', title: 'Заполнить ключи идемпотентности', repo: 'payments-worker', kind: 'code', estimate: 1, deps: ['PAY-104'] } } };
  // act
  const d = describeProposal(p, tasks);
  // assert
  assert.deepEqual(text(d), [
    'Новая задача PAY-115 «Заполнить ключи идемпотентности»',
    '+ задача payments-worker · код · ~1 д · зависит от PAY-104',
  ]);
  assert.deepEqual(d.title.filter(x => typeof x === 'object'), [{ key: 'PAY-115', exists: false }]);
});

test('describeProposal: новое исследование без зависимостей и оценки', () => {
  // arrange
  const p = { kind: 'add_task', payload: { task: { key: 'PAY-116', title: 'Профиль нагрузки', kind: 'research' } } };
  // act
  const d = describeProposal(p, tasks);
  // assert
  assert.deepEqual(text(d), ['Новая задача PAY-116 «Профиль нагрузки»', '+ задача исследование · без зависимостей']);
});

test('describeProposal: зависимости — новый список, добавленные и убранные связи', () => {
  // arrange
  const p = { kind: 'set_deps', payload: { key: 'PAY-107', deps: ['PAY-109', 'PAY-111'] } };
  const none = { kind: 'set_deps', payload: { key: 'PAY-107', deps: [] } };
  // act
  const d = describeProposal(p, tasks);
  const cleared = describeProposal(none, tasks);
  // assert
  assert.deepEqual(text(d), [
    'PAY-107: зависимости → PAY-109, PAY-111',
    '+ связь будет ждать PAY-109, PAY-111',
    '− связь больше не ждёт PAY-104',
  ]);
  assert.deepEqual(text(cleared), ['PAY-107: зависимости → нет', '− связь больше не ждёт PAY-104']);
});

test('describeProposal: отмена — заголовок задачи и кого она перестанет держать', () => {
  // arrange
  const p = { kind: 'cancel_task', payload: { key: 'PAY-109' } };
  const lone = { kind: 'cancel_task', payload: { key: 'PAY-111' } };
  // act
  const d = describeProposal(p, tasks);
  const l = describeProposal(lone, tasks);
  // assert
  assert.deepEqual(text(d), ['Отменить PAY-109 «Выгрузка для бухгалтерии»', '× отмена уберётся из зависимостей PAY-111']);
  assert.deepEqual(text(l), ['Отменить PAY-111 «Переключить чтение»']);
});

test('describeProposal: испорченный payload не роняет описание', () => {
  // act
  const d = describeProposal({ kind: 'set_deps', payload: { key: 7, deps: 'x' } }, tasks);
  const u = describeProposal({ kind: 'weird', payload: null }, tasks);
  // assert
  assert.equal(partsText(d.title), '?: зависимости → нет');
  assert.equal(partsText(u.title), 'weird');
});

test('proposalsTouching: открытые предложения, затрагивающие задачу', () => {
  // arrange
  const proposals = [
    { id: 'p1', kind: 'add_task', status: 'open', payload: { task: { key: 'PAY-115', title: 'x', deps: ['PAY-104'] } } },
    { id: 'p2', kind: 'set_deps', status: 'open', payload: { key: 'PAY-107', deps: ['PAY-104'] } },
    { id: 'p3', kind: 'cancel_task', status: 'open', payload: { key: 'PAY-104' } },
    { id: 'p4', kind: 'cancel_task', status: 'applied', payload: { key: 'PAY-104' } },
  ];
  // act
  const ids = key => proposalsTouching(proposals, key).map(p => p.id);
  // assert
  assert.deepEqual(ids('PAY-104'), ['p1', 'p3'], 'новая задача зависит от неё и её отмена; обработанные не в счёт');
  assert.deepEqual(ids('PAY-107'), ['p2']);
  assert.deepEqual(ids('PAY-115'), ['p1']);
  assert.deepEqual(ids('PAY-999'), []);
});

test('agentStateLine: состояние агента проекта человеческим текстом', () => {
  // act + assert
  assert.deepEqual(agentStateLine({ state: 'running' }), { cls: 'run', text: 'работает' });
  assert.deepEqual(agentStateLine({ state: 'waiting_turn', wait_reason: 'global' }), { cls: 'wait', text: 'ждёт свободный слот' });
  assert.deepEqual(agentStateLine({ state: 'idle' }), { cls: 'idle', text: 'ждёт' });
  assert.deepEqual(agentStateLine({ state: 'error', turn_error: 'boom' }), { cls: 'err', text: 'ошибка хода' });
  assert.deepEqual(agentStateLine({ state: 'idle', parked: true }), { cls: 'err', text: 'переписка агентов остановлена' });
  assert.deepEqual(agentStateLine(null), { cls: 'idle', text: 'ждёт' });
});

test('proposalSignature: меняется вместе с текущими зависимостями, от которых считается описание', () => {
  // arrange
  const setDeps = { id: 'p2', kind: 'set_deps', status: 'open', payload: { key: 'PAY-107', deps: ['PAY-109'] } };
  const cancel = { id: 'p3', kind: 'cancel_task', status: 'open', payload: { key: 'PAY-109' } };
  const add = { id: 'p1', kind: 'add_task', status: 'open', payload: { task: { key: 'PAY-115', title: 'x', deps: ['PAY-120'] } } };
  const moved = new Map(tasks);
  moved.set('PAY-107', { ...tasks.get('PAY-107'), deps: ['PAY-109'] });
  const dependent = new Map(tasks);
  dependent.set('PAY-107', { ...tasks.get('PAY-107'), deps: ['PAY-104', 'PAY-109'] });
  const created = new Map(tasks);
  created.set('PAY-120', { key: 'PAY-120', title: 'новая', deps: [] });
  // act + assert
  assert.notEqual(proposalSignature(setDeps, tasks), proposalSignature(setDeps, moved), 'зависимости цели поменялись');
  assert.notEqual(proposalSignature(cancel, tasks), proposalSignature(cancel, dependent), 'появилась зависящая задача');
  assert.notEqual(proposalSignature(add, tasks), proposalSignature(add, created), 'зависимость новой задачи появилась');
  assert.equal(proposalSignature(setDeps, tasks), proposalSignature({ ...setDeps }, new Map(tasks)));
  assert.notEqual(proposalSignature(setDeps, tasks), proposalSignature({ ...setDeps, status: 'applied' }, tasks));
});

test('describeFor: обработанное предложение сохраняет описание, каким оно было открытым', () => {
  // arrange
  const cache = new Map();
  const open = { id: 'p2', kind: 'set_deps', status: 'open', payload: { key: 'PAY-107', deps: ['PAY-109'] } };
  const after = new Map(tasks);
  after.set('PAY-107', { ...tasks.get('PAY-107'), deps: ['PAY-109'] });
  // act
  const before = describeFor(cache, open, tasks);
  const applied = describeFor(cache, { ...open, status: 'applied' }, after);
  const unseen = describeFor(cache, { ...open, id: 'p9', status: 'applied' }, after);
  // assert
  assert.deepEqual(applied, before);
  assert.deepEqual(text(applied).slice(1), ['+ связь будет ждать PAY-109', '− связь больше не ждёт PAY-104']);
  assert.deepEqual(unseen.changes, [], 'без открытого состояния описание считается по текущему графу');
});

test('proposalOutcome: устаревшее предложение показывает итог с причиной, открытое — без итога', () => {
  // arrange
  const p = { id: 'p3', kind: 'add_task', payload: { task: { key: 'PAY-109' } } };
  // act
  const obsolete = proposalOutcome({ ...p, status: 'obsolete', obsolete_reason: 'exists' });
  const unknownWhy = proposalOutcome({ ...p, status: 'obsolete', obsolete_reason: 'zzz' });
  const open = proposalOutcome({ ...p, status: 'open' });
  // assert
  assert.deepEqual(obsolete, { status: 'obsolete', cls: 'muted', icon: 'x', text: 'Устарело: задача с этим ключом уже есть' });
  assert.equal(unknownWhy.text, 'Устарело');
  assert.equal(proposalOutcome({ ...p, status: 'applied' }).status, 'applied');
  assert.equal(open, null);
});

test('proposalsTouching: устаревшее предложение задачу не затрагивает', () => {
  // arrange
  const proposals = [{ id: 'p1', kind: 'cancel_task', status: 'obsolete', obsolete_reason: 'cancelled', payload: { key: 'PAY-104' } }];
  // act / assert
  assert.deepEqual(proposalsTouching(proposals, 'PAY-104'), []);
});
