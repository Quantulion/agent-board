import { test } from 'node:test';
import assert from 'node:assert/strict';
import { setLanguage } from '../../agent_board/static/js/i18n.js';
import { collectChanges, errorField, formValue, parseField, settingsFields } from '../../agent_board/static/js/settings.js';

const SCHEMA = {
  fields: [
    { name: 'title', type: 'string', default: null, description: 'Title' },
    { name: 'chains', type: 'boolean', default: true, description: 'Chains' },
    { name: 'max_agents', type: 'integer', default: 3, description: 'Max' },
    { name: 'repos', type: 'array', default: [], description: 'Repos', shape: { example: { name: 'api' } } },
    { name: 'tracker', type: 'object', default: { type: 'none' }, description: 'Tracker' },
    { name: 'agent_rules', type: 'string', default: '', description: 'Rules' },
    { name: 'global_max_agents', type: 'integer', default: 6, description: 'Global' },
    { name: 'future_field', type: 'string', default: '', description: 'New' },
  ],
  not_settable: ['slug', 'stage'],
};

test('settingsFields: виды полей по типу схемы, простые раньше составных', () => {
  // act
  const fields = settingsFields(SCHEMA);
  // assert
  assert.deepEqual(fields.map(f => [f.name, f.kind]), [
    ['title', 'text'], ['max_agents', 'int'], ['global_max_agents', 'int'], ['chains', 'bool'],
    ['agent_rules', 'textarea'], ['future_field', 'text'], ['repos', 'json'], ['tracker', 'json'],
  ]);
  assert.deepEqual(fields.find(f => f.name === 'repos').example, { name: 'api' });
  assert.equal(fields.find(f => f.name === 'tracker').example, null);
});

test('settingsFields: пустая или битая схема — пустая форма', () => {
  // act + assert
  assert.deepEqual(settingsFields(null), []);
  assert.deepEqual(settingsFields({ fields: 'x' }), []);
});

test('formValue: составные — отформатированный JSON, отсутствующее значение — по умолчанию', () => {
  // arrange
  const [title, maxAgents, , chains, , , repos, tracker] = settingsFields(SCHEMA);
  // act + assert
  assert.equal(formValue(repos, [{ name: 'a' }]), '[\n  {\n    "name": "a"\n  }\n]');
  assert.equal(formValue(tracker, undefined), '{\n  "type": "none"\n}');
  assert.equal(formValue(maxAgents, 4), '4');
  assert.equal(formValue(chains, false), false);
  assert.equal(formValue(title, null), '');
});

test('parseField: число, JSON и его тип проверяются до отправки', () => {
  // arrange
  setLanguage('ru');
  const byName = Object.fromEntries(settingsFields(SCHEMA).map(f => [f.name, f]));
  // act + assert
  assert.deepEqual(parseField(byName.max_agents, ' 5 '), { value: 5 });
  assert.deepEqual(parseField(byName.max_agents, '0'), { error: 'Нужно целое число не меньше 1' });
  assert.deepEqual(parseField(byName.max_agents, '1.5'), { error: 'Нужно целое число не меньше 1' });
  assert.deepEqual(parseField(byName.repos, '[{"name": "a"}]'), { value: [{ name: 'a' }] });
  assert.match(parseField(byName.repos, '[{"name": }]').error, /^Некорректный JSON: /);
  assert.deepEqual(parseField(byName.repos, '{}'), { error: 'Нужен JSON-список: […]' });
  assert.deepEqual(parseField(byName.tracker, '[]'), { error: 'Нужен JSON-объект: {…}' });
  assert.deepEqual(parseField(byName.tracker, 'null'), { error: 'Нужен JSON-объект: {…}' });
  assert.deepEqual(parseField(byName.chains, false), { value: false });
  assert.deepEqual(parseField(byName.title, ' x '), { value: ' x ' });
});

test('collectChanges: отправляются только изменённые поля, ошибки разбора — по полям', () => {
  // arrange
  setLanguage('ru');
  const fields = settingsFields(SCHEMA);
  const config = { title: 'P', chains: true, max_agents: 3, repos: [], tracker: { type: 'none' }, agent_rules: '', global_max_agents: 6 };
  const raws = Object.fromEntries(fields.map(f => [f.name, formValue(f, config[f.name])]));
  // act
  const same = collectChanges(fields, raws, config);
  const changed = collectChanges(fields, { ...raws, max_agents: '2', tracker: '{"type": "github"}' }, config);
  const broken = collectChanges(fields, { ...raws, max_agents: 'x', repos: '[' }, config);
  // assert
  assert.deepEqual(same, { changes: {}, errors: {} });
  assert.deepEqual(changed.changes, { max_agents: 2, tracker: { type: 'github' } });
  assert.deepEqual(Object.keys(broken.errors).sort(), ['max_agents', 'repos']);
});

test('errorField: ошибка сервера попадает под поле по имени или по смыслу', () => {
  // arrange
  const names = ['max_agents', 'global_max_agents', 'repos', 'tracker', 'ship', 'title'];
  // act + assert
  assert.equal(errorField('max_agents должен быть целым числом не меньше 1', names), 'max_agents');
  assert.equal(errorField('global_max_agents должен быть целым числом не меньше 1', names), 'global_max_agents');
  assert.equal(errorField('Не задано tracker.url', names), 'tracker');
  assert.equal(errorField('repos[].path должен быть непустой строкой', names), 'repos');
  assert.equal(errorField('Каталог репозитория app не найден: /x', names), 'repos');
  assert.equal(errorField('Неизвестный плейсхолдер в команде отправки: {x}', names), 'ship');
  assert.equal(errorField('Каталог репозитория app не найден: /x', ['title']), null);
  assert.equal(errorField('Настройки одновременно меняются из другого места', names), null);
});

const SHIP_SCHEMA = {
  fields: [
    { name: 'title', type: 'string', default: null, description: 'Title' },
    { name: 'repos', type: 'array', default: [], description: 'Repos' },
    { name: 'ship', type: 'object', default: { command: null, timeout: 600 }, description: 'Ship',
      shape: { placeholders: ['key', 'branch'], example: 'git push origin {branch}' } },
  ],
};

test('settingsFields: отправка в ревью — свой редактор между простыми и составными полями', () => {
  // act
  const fields = settingsFields(SHIP_SCHEMA);
  // assert
  assert.deepEqual(fields.map(f => [f.name, f.kind]), [['title', 'text'], ['ship', 'ship'], ['repos', 'json']]);
  assert.deepEqual(fields[1].placeholders, ['key', 'branch']);
});

test('редактор отправки: пустая команда выключает отправку, таймаут проверяется', () => {
  // arrange
  setLanguage('ru');
  const ship = settingsFields(SHIP_SCHEMA).find(f => f.name === 'ship');
  // act + assert
  assert.deepEqual(formValue(ship, undefined), { command: '', timeout: '600' });
  assert.deepEqual(formValue(ship, { command: 'git push', timeout: 60 }), { command: 'git push', timeout: '60' });
  assert.deepEqual(parseField(ship, { command: '  git push origin {branch} ', timeout: ' 120 ' }),
    { value: { command: 'git push origin {branch}', timeout: 120 } });
  assert.deepEqual(parseField(ship, { command: '   ', timeout: '600' }), { value: { command: null, timeout: 600 } });
  assert.deepEqual(parseField(ship, { command: 'x', timeout: '0' }), { error: 'Таймаут — целое число секунд не меньше 1' });
});

test('collectChanges: команда отправки уходит объектом, без изменений — ничего', () => {
  // arrange
  const fields = settingsFields(SHIP_SCHEMA);
  const config = { title: 'P', repos: [], ship: { command: null, timeout: 600 } };
  const raws = Object.fromEntries(fields.map(f => [f.name, formValue(f, config[f.name])]));
  // act
  const same = collectChanges(fields, raws, config);
  const set = collectChanges(fields, { ...raws, ship: { command: 'git push origin {branch}', timeout: '600' } }, config);
  // assert
  assert.deepEqual(same.changes, {});
  assert.deepEqual(set.changes, { ship: { command: 'git push origin {branch}', timeout: 600 } });
});
