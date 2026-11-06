// Ожидание свободного слота: ход агента стоит в очереди, потому что занят лимит одновременных ходов.
import { h, icon } from './dom.js';
import { T } from './i18n.js';

// настройка проекта, которая задаёт каждый из лимитов
export const LIMIT_FIELDS = { project: 'max_agents', global: 'global_max_agents' };

// Что показать об ожидании слота; null, если агент слота не ждёт.
export function slotWait(state, reason, config) {
  if (state !== 'waiting_turn') return null;
  const known = Object.prototype.hasOwnProperty.call(LIMIT_FIELDS, reason) ? reason : null;
  const field = known ? LIMIT_FIELDS[known] : null;
  const value = field && config ? config[field] : null;
  return { reason: known, field, limit: Number.isInteger(value) ? value : null };
}

// Команда, которой поднимают упёршийся лимит.
export function raiseCommand(w) {
  if (!w || !w.field) return null;
  return 'agent-board config set --json \'{"' + w.field + '": ' + (w.limit ? w.limit + 1 : 'N') + '}\'';
}

// Плашка с причиной ожидания и подсказкой, где поднять лимит.
export function slotNote(w) {
  const cmd = raiseCommand(w);
  return h('div', { class: 'slot-note', role: 'status' },
    h('b', null, icon('clock'), T.slot.title),
    h('span', null, w.reason ? T.slot.reason[w.reason](w.limit) : T.slot.unknown),
    h('span', { class: 'muted' }, T.slot.auto),
    cmd ? h('span', { class: 'hint' }, T.slot.raise(w.field), h('code', { class: 'inline-code' }, cmd)) : null);
}
