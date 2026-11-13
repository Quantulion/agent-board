import { test } from 'node:test';
import assert from 'node:assert/strict';
import { boardChanges, boardForm, choices, defaultHarnessType } from '../../agent_board/static/js/boardsettings.js';
import { setLanguage } from '../../agent_board/static/js/i18n.js';

setLanguage('ru');

const SETTINGS = {
  language: 'ru', global_max_agents: 6,
  harness: { type: 'claude', permission_mode: 'auto', allowed_tools: ['Bash'] },
  review: { tool: 'crit', binary: 'crit' },
};

test('boardForm: значения формы по настройкам доски', () => {
  // act
  const form = boardForm(SETTINGS);
  // assert
  assert.deepEqual(form, {
    language: 'ru', global_max_agents: '6', harness_type: 'claude', model: '', permission_mode: 'auto', sandbox: '',
    review_tool: 'crit', review_binary: 'crit',
  });
});

test('boardChanges: без правок — ничего не отправляется', () => {
  // act
  const result = boardChanges(boardForm(SETTINGS), SETTINGS);
  // assert
  assert.deepEqual(result, { changes: {}, errors: {} });
});

test('boardChanges: язык и лимит уходят своими полями, агент — целиком с прежними ключами', () => {
  // arrange
  const form = { ...boardForm(SETTINGS), language: 'en', global_max_agents: ' 9 ', model: ' opus ', permission_mode: 'acceptEdits' };
  // act
  const { changes, errors } = boardChanges(form, SETTINGS);
  // assert
  assert.deepEqual(errors, {});
  assert.deepEqual(changes, {
    language: 'en', global_max_agents: 9,
    harness: { type: 'claude', permission_mode: 'acceptEdits', allowed_tools: ['Bash'], model: 'opus' },
  });
});

test('boardChanges: у Codex вместо режима разрешений — песочница, пустая модель убирается', () => {
  // arrange
  const settings = { ...SETTINGS, harness: { type: 'claude', permission_mode: 'auto', model: 'opus' } };
  const form = { ...boardForm(settings), harness_type: 'codex', model: '', sandbox: 'read-only' };
  // act
  const { changes } = boardChanges(form, settings);
  // assert
  assert.deepEqual(changes, { harness: { type: 'codex', sandbox: 'read-only' } });
});

test('boardChanges: ревью без команды — команда по умолчанию', () => {
  // arrange
  const form = { ...boardForm(SETTINGS), review_tool: 'none', review_binary: ' ' };
  // act
  const { changes } = boardChanges(form, SETTINGS);
  // assert
  assert.deepEqual(changes, { review: { tool: 'none' } });
});

test('boardChanges: пустой язык и негодный лимит — ошибки полей', () => {
  // arrange
  const form = { ...boardForm(SETTINGS), language: ' ', global_max_agents: '0' };
  // act
  const { changes, errors } = boardChanges(form, SETTINGS);
  // assert
  assert.deepEqual(errors, { language: 'Укажите язык, например ru или en', global_max_agents: 'Нужно целое число не меньше 1' });
  assert.equal('language' in changes, false);
  assert.equal('global_max_agents' in changes, false);
});

test('choices: текущее значение остаётся в списке, даже если оно не из известных', () => {
  // act + assert
  assert.deepEqual(choices(['ru', 'en'], 'de'), ['ru', 'en', 'de']);
  assert.deepEqual(choices(['ru', 'en'], 'en'), ['ru', 'en']);
  assert.deepEqual(choices(['ru', 'en'], ''), ['ru', 'en']);
});

test('defaultHarnessType: агент нового проекта — как у доски, неизвестный — Claude Code', () => {
  // act + assert
  assert.equal(defaultHarnessType({ harness: { type: 'codex' } }), 'codex');
  assert.equal(defaultHarnessType({ harness: { type: 'claude' } }), 'claude');
  assert.equal(defaultHarnessType({ harness: { type: 'fake' } }), 'claude');
  assert.equal(defaultHarnessType(null), 'claude');
});
