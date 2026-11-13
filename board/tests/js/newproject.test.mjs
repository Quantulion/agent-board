import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  keyFromTitle, projectErrorField, projectPayload, suggestProjectKey, takenProjectKeys, validateNewProject,
} from '../../agent_board/static/js/newproject.js';
import { setLanguage } from '../../agent_board/static/js/i18n.js';

setLanguage('ru');

const form = extra => ({
  title: 'Мелкие доработки', key: 'FIX', harness: 'claude', max_agents: '3', review: true,
  agentTasks: true, ...extra,
});

test('keyFromTitle: русское название — непустой латинский ключ из первых букв слов', () => {
  // act
  const key = keyFromTitle('Мелкие доработки биллинга');

  // assert
  assert.match(key, /^[A-Z][A-Z0-9]{1,4}$/);
  assert.equal(key, 'MDB');
});

test('keyFromTitle: одно слово — его начало, длинное название — не больше 5 букв', () => {
  // act + assert
  assert.equal(keyFromTitle('Billing'), 'BIL');
  assert.equal(keyFromTitle('a b c d e f g'), 'ABCDE');
  assert.equal(keyFromTitle('Х'), 'KH', 'одна буква-транслитерация дополняется до двух');
});

test('keyFromTitle: ключ начинается с буквы, пустое или нелатинское название — PRJ', () => {
  // act + assert
  assert.equal(keyFromTitle('2026 отчёт'), 'OTC');
  assert.equal(keyFromTitle(''), 'PRJ');
  assert.equal(keyFromTitle('  ...  '), 'PRJ');
  assert.equal(keyFromTitle('数据'), 'PRJ');
});

test('suggestProjectKey: занятый ключ получает номер без учёта регистра', () => {
  // act + assert
  assert.deepEqual(suggestProjectKey('FIX', ['fix', 'AB']), { key: 'FIX2', renamed: true });
  assert.deepEqual(suggestProjectKey('fix', ['FIX', 'fix2']), { key: 'FIX3', renamed: true });
  assert.deepEqual(suggestProjectKey('PAY', ['fix']), { key: 'PAY', renamed: false });
});

test('takenProjectKeys: имена проектов и префиксы ключей их задач', () => {
  // arrange
  const states = [
    { slug: 'agent-board', snapshot: { tasks: [{ key: 'AB-1' }, { key: 'AB-2' }, { key: 'readme' }] } },
    { slug: 'fix', snapshot: null },
  ];

  // act
  const taken = takenProjectKeys(states);

  // assert
  assert.deepEqual(taken.sort(), ['AB', 'AGENT-BOARD', 'FIX']);
  assert.deepEqual(suggestProjectKey('ab', taken), { key: 'AB2', renamed: true });
});

test('validateNewProject: пустое название и недопустимый ключ — ошибки полей', () => {
  // act + assert
  assert.deepEqual(validateNewProject(form()), {});
  assert.ok(validateNewProject(form({ title: '  ' })).title);
  assert.ok(validateNewProject(form({ key: 'F-1' })).key);
  assert.ok(validateNewProject(form({ key: '1AB' })).key);
  assert.ok(validateNewProject(form({ key: '' })).key);
  assert.ok(validateNewProject(form({ max_agents: '0' })).max_agents);
});

test('projectPayload: проект без плана, ревью перед коммитом — это выключенное автоодобрение', () => {
  // act
  const body = projectPayload(form({ key: 'fix', harness: 'codex', review: false, agentTasks: false, max_agents: ' 5 ' }));

  // assert
  assert.deepEqual(body, {
    actor: 'user', title: 'Мелкие доработки', key: 'FIX', kind: 'free', harness_type: 'codex',
    max_agents: 5, auto_approve: true, project_agent_tasks: false,
  });
});

test('projectErrorField: ошибка сервера показывается у своего поля', () => {
  // act + assert
  assert.equal(projectErrorField('Ключ задач должен начинаться с буквы'), 'key');
  assert.equal(projectErrorField('Название проекта не должно быть пустым'), 'title');
  assert.equal(projectErrorField('max_agents должен быть целым числом не меньше 1'), 'max_agents');
  assert.equal(projectErrorField('диск переполнен'), null);
});
