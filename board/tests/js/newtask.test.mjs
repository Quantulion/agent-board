import { test } from 'node:test';
import assert from 'node:assert/strict';
import { errorField, pickRepo, repoSuggestions, suggestKey, taskPayload, validateNewTask } from '../../agent_board/static/js/newtask.js';

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

test('taskPayload: в проекте без плана папка уходит как repo_path, ключ не передаётся', () => {
  // arrange
  const form = { key: 'FIX-9', title: ' Убрать эндпоинт ', description: 'd', kind: 'code', repo: ' ~/code/offers ', estimate: '', deps: [] };
  // act
  const p = taskPayload(form, { free: true, repos: ['billing-api'] });
  // assert
  assert.deepEqual(p, { title: 'Убрать эндпоинт', description: 'd', done_when: '', kind: 'code', repo_path: '~/code/offers', deps: [] });
});

test('taskPayload: в проекте без плана репозиторий проекта уходит как repo, приоритет передаётся', () => {
  // arrange
  const form = { title: 'x', kind: 'code', repo: 'billing-api', priority: 'high', deps: [] };
  // act
  const p = taskPayload(form, { free: true, repos: ['billing-api'] });
  const none = taskPayload({ ...form, repo: ' ', priority: '' }, { free: true, repos: ['billing-api'] });
  // assert
  assert.equal(p.repo, 'billing-api');
  assert.equal('repo_path' in p, false);
  assert.equal('key' in p, false);
  assert.equal(p.priority, 'high');
  assert.equal(none.repo, '');
  assert.equal('priority' in none, false);
});

test('validateNewTask: в проекте без плана ключ не проверяется, репозиторий — имя из проекта или путь', () => {
  // arrange
  const opts = { free: true, repos: ['billing-api'] };
  // act + assert
  assert.deepEqual(validateNewTask({ title: 'x', repo: '~/code/offers' }, [], opts), {});
  assert.deepEqual(validateNewTask({ title: 'x', repo: 'billing-api' }, [], opts), {});
  assert.deepEqual(validateNewTask({ title: 'x', repo: '' }, [], opts), {});
  assert.deepEqual(validateNewTask({ title: 'x', repo: 'offers' }, [], opts), { repo: 'Выберите репозиторий проекта или укажите путь к папке (/… или ~/…)' });
});

test('repoSuggestions: сначала репозитории проекта, затем папки; папка не git недоступна для выбора', () => {
  // arrange
  const repos = [{ name: 'billing-api', path: '/w/billing-api' }, { name: 'offers-old', path: '/w/old' }];
  const folders = [
    { path: '~/w/offers', abs: '/h/w/offers', git: true, branch: 'main' },
    { path: '~/w/off-tools', abs: '/h/w/off-tools', git: false, branch: null },
  ];
  // act
  const list = repoSuggestions(repos, folders, 'off');
  // assert
  assert.deepEqual(list.map(o => [o.type, o.value, Boolean(o.disabled)]), [
    ['repo', 'offers-old', false],
    ['folder', '~/w/offers', false],
    ['folder', '~/w/off-tools', true],
  ]);
  assert.equal(pickRepo(list[1]), '~/w/offers');
  assert.equal(pickRepo(list[2]), null, '«не git» не выбирается');
  assert.equal(pickRepo(list[0]), 'offers-old');
});

test('repoSuggestions: пустой ввод — все репозитории проекта; путь ищется и по папке репозитория', () => {
  // arrange
  const repos = [{ name: 'api', path: '/w/billing' }, { name: 'web', path: '/w/site' }];
  // act + assert
  assert.deepEqual(repoSuggestions(repos, [], '').map(o => o.value), ['api', 'web']);
  assert.deepEqual(repoSuggestions(repos, [], '/w/bil').map(o => o.value), ['api']);
});

test('errorField: ошибки пути к репозиторию — под полем репозитория', () => {
  // act + assert
  assert.equal(errorField('Папка не является git-репозиторием: ~/x'), 'repo');
  assert.equal(errorField('Папка репозитория не найдена: ~/x'), 'repo');
  assert.equal(errorField('Репозиторий x не из настроек проекта: a, b'), 'repo');
});
