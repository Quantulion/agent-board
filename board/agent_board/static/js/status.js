// Смена статуса задачи: меню статуса, перенос с реакцией сервера и диалог внешнего блокера.
import { h } from './dom.js';
import { STATUSES, T, statusInfo } from './i18n.js';
import { sdot, statusOf } from './chips.js';
import { openModal, toast } from './ui.js';

const unique = list => [...new Set(list)];

// Текст тоста после смены статуса: что сервер сделал в ответ.
export function reactionText(events) {
  const changed = (events || []).find(e => e.type === 'status_changed');
  if (!changed) return T.statusUnchanged;
  const keysOf = type => unique(events.filter(e => e.type === type).map(e => e.data.key));
  const parts = [changed.data.key + ' → ' + statusInfo(changed.data.to).label + '.'];
  const stopped = keysOf('agent_stop');
  const depsRemoved = keysOf('deps_changed');
  const unblocked = keysOf('task_unblocked');
  const woken = keysOf('agent_wake').filter(k => !String(k).startsWith('_'));
  if (stopped.length) parts.push(T.reaction.stopped(stopped));
  if (depsRemoved.length) parts.push(T.reaction.depsRemoved(depsRemoved));
  if (unblocked.length) parts.push(T.reaction.unblocked(unblocked));
  if (woken.length) parts.push(T.reaction.woken(woken));
  if (parts.length === 1) parts.push(T.noReaction);
  return parts.join(' ');
}

// Меню выбора статуса; onPick получает id выбранного статуса.
// «Заблокирована» вычисляется сервером, вручную задаётся только внешний блокер — пункт открывает его диалог.
export function statusMenu(current, onPick) {
  return h('div', { class: 'menu', role: 'menu', 'aria-label': T.statusMenuAria },
    STATUSES.map(s => h('button', {
      type: 'button', role: 'menuitem', class: s.id === current ? 'cur' : null,
      onclick: () => onPick(s.id),
    }, sdot(s.id), s.id === 'blocked' ? T.externalBlockerItem : s.label)));
}

// Выбор в меню статуса или перенос в колонку: «Заблокирована» — диалог внешнего блокера, иначе смена статуса.
export function requestMove(ctx, task, to) {
  if (to === 'blocked') return externalBlockerDialog(ctx, task);
  if (to !== statusOf(task)) moveTask(ctx.client, task.key, to);
  return null;
}

const moving = new Set();

// Перенос задачи пользователем: тост с реакцией сервера или с его ошибкой (карточка тогда остаётся на месте).
// Повторный перенос той же задачи до ответа сервера игнорируется.
export async function moveTask(client, key, to) {
  if (moving.has(key)) return null;
  moving.add(key);
  try {
    const res = await client.post('/tasks/' + encodeURIComponent(key) + '/transition', { to, actor: 'user' });
    const events = (res && res.events) || [];
    toast(reactionText(events));
    return events;
  } catch (err) {
    toast(T.moveFailed(key) + ((err && err.message) || String(err)), { error: true });
    return null;
  } finally {
    moving.delete(key);
  }
}

const BLOCKER_MAX = 500;

// Внешний блокер задаёт и снимает пользователь: задача не стартует, пока он есть, причину видят все.
export function externalBlockerDialog(ctx, task) {
  const current = typeof task.external_blocker === 'string' ? task.external_blocker.trim() : '';
  const input = h('input', { type: 'text', id: 'ext-blocker', placeholder: T.blocker.placeholder, maxlength: String(BLOCKER_MAX) });
  input.value = current;
  const error = h('div', { class: 'ferr', role: 'alert', hidden: true });
  const save = value => {
    error.hidden = true;
    buttons.forEach(b => { b.disabled = true; });
    ctx.client.post('/tasks/' + encodeURIComponent(task.key) + '/update', { actor: 'user', fields: { external_blocker: value } })
      .then(() => {
        close();
        toast(value ? T.blocker.set(task.key) : T.blocker.cleared(task.key));
      })
      .catch(err => {
        buttons.forEach(b => { b.disabled = false; });
        error.textContent = (err && err.message) || String(err);
        error.hidden = false;
      });
  };
  const submit = () => {
    const value = input.value.trim();
    // пустая причина снимает блокер, только если он есть; иначе задача заблокировалась бы без объяснения
    const problem = !value && !current ? T.blocker.needReason : value.length > BLOCKER_MAX ? T.blocker.tooLong(BLOCKER_MAX) : '';
    if (problem) {
      error.textContent = problem;
      error.hidden = false;
      input.focus();
      return;
    }
    save(value || null);
  };
  input.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); submit(); } });
  const clearBtn = current ? h('button', { class: 'btn danger', type: 'button', onclick: () => save(null) }, T.blocker.clear) : null;
  const okBtn = h('button', { class: 'btn primary', type: 'button', onclick: submit }, current ? T.blocker.save : T.blocker.block);
  const buttons = [clearBtn, okBtn].filter(Boolean);
  const { close } = openModal(T.blocker.title(task.key),
    [h('div', { class: 'field' }, h('label', { for: 'ext-blocker' }, T.blocker.label), input), error],
    [clearBtn, h('span', { class: 'grow' }), h('button', { class: 'btn', type: 'button', onclick: () => close() }, T.cancel), okBtn]);
  input.focus();
  return close;
}
