import { test } from 'node:test';
import assert from 'node:assert/strict';
import { installFakeDocument } from './fake-dom.mjs';
import { raiseCommand, slotNote, slotWait } from '../../agent_board/static/js/slot.js';
import { setLanguage } from '../../agent_board/static/js/i18n.js';

installFakeDocument();

const config = { max_agents: 2, global_max_agents: 6 };

test('slotWait: только при ожидании слота, с упёршимся лимитом и его значением', () => {
  // act + assert
  assert.equal(slotWait('running', 'project', config), null);
  assert.equal(slotWait('idle', '', config), null);
  assert.deepEqual(slotWait('waiting_turn', 'project', config), { reason: 'project', field: 'max_agents', limit: 2 });
  assert.deepEqual(slotWait('waiting_turn', 'global', config), { reason: 'global', field: 'global_max_agents', limit: 6 });
  assert.deepEqual(slotWait('waiting_turn', '', config), { reason: null, field: null, limit: null }, 'причина неизвестна');
  assert.deepEqual(slotWait('waiting_turn', 'toString', config), { reason: null, field: null, limit: null });
  assert.deepEqual(slotWait('waiting_turn', 'project', null), { reason: 'project', field: 'max_agents', limit: null });
});

test('raiseCommand: команда поднимает упёршийся лимит на единицу', () => {
  // act + assert
  assert.equal(raiseCommand(slotWait('waiting_turn', 'project', config)), 'agent-board config set --json \'{"max_agents": 3}\'');
  assert.equal(raiseCommand(slotWait('waiting_turn', 'global', {})), 'agent-board config set --json \'{"global_max_agents": N}\'');
  assert.equal(raiseCommand(slotWait('waiting_turn', '', config)), null);
});

test('slotNote: причина и подсказка на ru и en', () => {
  // arrange
  const wait = slotWait('waiting_turn', 'global', config);
  // act
  setLanguage('ru');
  const ru = slotNote(wait).textContent;
  setLanguage('en');
  const en = slotNote(wait).textContent;
  setLanguage('ru');
  const unknown = slotNote(slotWait('waiting_turn', '', config)).textContent;
  // assert
  assert.match(ru, /Ждёт свободный слот/);
  assert.match(ru, /общий лимит/);
  assert.match(ru, /global_max_agents": 7/);
  assert.match(en, /Waiting for a free slot/);
  assert.match(en, /server limit/);
  assert.match(unknown, /лимит одновременных ходов/);
  assert.doesNotMatch(unknown, /config set/);
});
