// Отправка в ревью командой проекта: действие карточки, массовая отправка одобренных и состояние отправки.
import { registerCardAction } from './actions.js';
import { spill, statusOf } from './chips.js';
import { h, icon, patchList } from './dom.js';
import { T, statusInfo } from './i18n.js';
import { isResearch, sec } from './pane-kit.js';
import { confirmDialog, openModal, toast } from './ui.js';

const KEYS = new Intl.Collator('ru', { numeric: true });
const PLACEHOLDERS = ['key', 'title', 'branch', 'base', 'worktree', 'repo'];
// зависимость уже влита или отменена: отправлять её не нужно
const SHIPPED = new Set(['sent', 'done', 'cancelled']);
const ACTIVE = new Set(['queued', 'running']);

export function shipCommand(snapshot) {
  const ship = snapshot && snapshot.config && snapshot.config.ship;
  const command = ship && typeof ship.command === 'string' ? ship.command.trim() : '';
  return command || null;
}

const shipState = task => (task && task.ship && typeof task.ship === 'object' && typeof task.ship.state === 'string' ? task.ship.state : null);
export const isShipping = task => ACTIVE.has(shipState(task));

// Одобренные задачи, которые можно отправить сейчас; без команды отправки — ни одной.
export function shippable(snapshot) {
  if (!snapshot || !shipCommand(snapshot)) return [];
  return snapshot.tasks.filter(t => t.status === 'approved' && !isShipping(t)).sort((a, b) => KEYS.compare(a.key, b.key));
}

// План массовой отправки: зависимости раньше зависимых; requires — одобренные зависимости, которые уйдут вместе
// с задачей; blocked — почему задачу сейчас отправить нельзя.
export function shipPlan(snapshot) {
  const candidates = shippable(snapshot);
  const byKey = new Map(snapshot.tasks.map(t => [t.key, t]));
  const inPlan = new Map(candidates.map(t => [t.key, t]));
  const ordered = [];
  const seen = new Set();
  const visit = t => {
    if (seen.has(t.key)) return;
    seen.add(t.key);
    for (const d of [...(t.deps || [])].sort(KEYS.compare)) if (inPlan.has(d)) visit(inPlan.get(d));
    ordered.push(t);
  };
  candidates.forEach(visit);
  const items = new Map();
  for (const t of ordered) {
    const requires = [];
    let blocked = null;
    for (const d of [...(t.deps || [])].sort(KEYS.compare)) {
      const dep = byKey.get(d);
      if (!dep || SHIPPED.has(dep.status)) continue;
      if (inPlan.has(d)) {
        requires.push(d);
        if (items.get(d).blocked && !blocked) blocked = T.ship.blocked(d, items.get(d).blocked);
      } else if (!blocked) {
        blocked = T.ship.blocked(d, isShipping(dep) ? T.ship.state[shipState(dep)] : statusInfo(statusOf(dep)).label);
      }
    }
    items.set(t.key, { key: t.key, title: t.title, requires, blocked, ship: t.ship || null });
  }
  return [...items.values()];
}

// Выбор в плане: отмеченная задача тянет свои зависимости, снятая — снимает зависимые от неё.
export function selectShip(plan, selected, key, on) {
  const next = new Set(selected);
  const byKey = new Map(plan.map(p => [p.key, p]));
  if (on) {
    const add = k => {
      const p = byKey.get(k);
      if (!p || p.blocked || next.has(k)) return;
      next.add(k);
      p.requires.forEach(add);
    };
    add(key);
  } else {
    const drop = k => {
      if (!next.delete(k)) return;
      for (const p of plan) if (p.requires.includes(k)) drop(p.key);
    };
    drop(key);
  }
  return next;
}

// Чип отправки на карточке доски: идущая отправка и неудача, пока задача одобрена.
export function shipChip(task) {
  const state = shipState(task);
  if (statusOf(task) !== 'approved' || !T.ship.chip[state]) return null;
  return { text: T.ship.chip[state], cls: state === 'running' ? 'rv' : state === 'queued' ? '' : 'err' };
}

export function commandParts(command) {
  const parts = [];
  const re = new RegExp('\\{(' + PLACEHOLDERS.join('|') + ')\\}', 'g');
  let last = 0;
  for (const m of command.matchAll(re)) {
    if (m.index > last) parts.push({ text: command.slice(last, m.index), ph: false });
    parts.push({ text: m[0], ph: true });
    last = m.index + m[0].length;
  }
  if (last < command.length) parts.push({ text: command.slice(last), ph: false });
  return parts;
}

export const commandPreview = command => h('pre', { class: 'cmd' },
  commandParts(command).map(p => (p.ph ? h('b', { class: 'ph' }, p.text) : p.text)));

const post = (ctx, keys) => ctx.client.post('/ship', { keys, actor: 'user' });

// Отправка одной задачи: подтверждение с командой и значениями плейсхолдеров.
export function shipOne(ctx, task) {
  const command = shipCommand(ctx.store.get().snapshot);
  const values = [['{key}', task.key], ['{branch}', task.branch], ['{worktree}', task.worktree]].filter(([, v]) => v);
  return confirmDialog({
    title: T.ship.title(task.key),
    body: [
      h('div', { class: 'field' }, h('label', null, T.ship.command), commandPreview(command || '')),
      values.length ? h('dl', { class: 'kv shipvals' }, values.map(([k, v]) => [h('dt', null, k), h('dd', { class: 'mono' }, v)])) : null,
      h('p', { class: 'mtext muted' }, T.ship.hostNote),
    ],
    ok: T.ship.run,
    submit: async () => {
      await post(ctx, [task.key]);
      toast(T.ship.requested(1));
    },
  });
}

registerCardAction({
  id: 'ship',
  label: T.ship.action,
  icon: 'send',
  visible: (task, snap) => !isResearch(task) && statusOf(task) === 'approved' && Boolean(shipCommand(snap)),
  enabled: task => !isShipping(task),
  title: task => (isShipping(task) ? T.ship.chip[shipState(task)] : null),
  run: shipOne,
});

// Строка результата отправки: состояние, код выхода, причина и хвост вывода.
function resultNodes(task) {
  const s = task && task.ship;
  if (!s || typeof s !== 'object') return [];
  const state = shipState(task);
  const failed = state === 'error' || state === 'interrupted' || state === 'cancelled';
  const tail = typeof s.output_tail === 'string' ? s.output_tail : '';
  return [
    h('span', { class: 'shipst ' + (state || '') }, T.ship.state[state] || state || ''),
    Number.isInteger(s.exit_code) && s.exit_code !== 0 ? h('span', { class: 'muted' }, T.ship.exitCode(s.exit_code)) : null,
    s.error || s.reason ? h('div', { class: 'shipwhy' }, String(s.error || s.reason)) : null,
    tail.trim() ? h('details', { class: 'tail', open: failed ? true : null },
      h('summary', null, T.ship.output), h('pre', { class: 'out' }, tail)) : null,
  ];
}

// Секция «Отправка в ревью» во вкладке «Обзор».
export function shipSection(task) {
  if (!task.ship || typeof task.ship !== 'object' || !shipState(task)) return null;
  return h('section', { class: shipState(task) === 'error' ? 'sec err ship' : 'sec ship' },
    h('h3', null, icon('send'), T.ship.section),
    h('div', { class: 'shipres' }, resultNodes(task)),
    h('p', { class: 'faint hint' }, T.ship.log(task.key)));
}

// Массовая отправка: план одобренных задач с выбором, команда и живой результат по каждой задаче.
export function openShipDialog(ctx) {
  const snap = ctx.store.get().snapshot;
  if (!snap) return null;
  const plan = shipPlan(snap);
  let selected = new Set();
  for (const p of plan) if (!p.blocked) selected.add(p.key);
  let sent = null;
  const list = h('div', { class: 'shiplist' });
  const error = h('div', { class: 'ferr', role: 'alert', hidden: true });
  const runBtn = h('button', { class: 'btn primary', type: 'button', 'data-ok': '1', onclick: run });
  const closeBtn = h('button', { class: 'btn', type: 'button', onclick: () => close() }, T.cancel);
  const command = shipCommand(snap) || '';

  function rowNode(item) {
    const box = h('input', { type: 'checkbox', 'data-key': item.key });
    box.addEventListener('change', () => {
      selected = selectShip(plan, selected, item.key, box.checked);
      paint();
    });
    const node = h('label', { class: 'shiprow', 'data-key': item.key }, box,
      h('span', { class: 'k' }, item.key), h('span', { class: 'tt' }, item.title), h('div', { class: 'res' }));
    node._box = box;
    return node;
  }

  function paintRow(node, item) {
    const tasks = new Map((ctx.store.get().snapshot || snap).tasks.map(t => [t.key, t]));
    node._box.checked = selected.has(item.key);
    node._box.disabled = Boolean(sent) || Boolean(item.blocked);
    node.classList.toggle('off', Boolean(sent) && !sent.includes(item.key));
    const res = node.querySelector('.res');
    // результат перерисовывается, только когда он изменился: раскрытый вывод не сворачивается от обновлений
    const sig = JSON.stringify(sent ? [sent.includes(item.key), (tasks.get(item.key) || {}).ship] : [item.blocked, item.requires]);
    if (node._sig === sig) return;
    node._sig = sig;
    if (sent) {
      res.replaceChildren(...(sent.includes(item.key) ? resultNodes(tasks.get(item.key)) : []).filter(Boolean));
      return;
    }
    const notes = [];
    if (item.blocked) notes.push(h('span', { class: 'muted' }, item.blocked));
    else if (item.requires.length) notes.push(h('span', { class: 'muted' }, T.ship.requires(item.requires)));
    if (item.ship && (item.ship.state === 'error' || item.ship.state === 'interrupted')) notes.push(h('span', { class: 'shipst error' }, T.ship.lastError));
    res.replaceChildren(...notes);
  }

  function paint() {
    patchList(list, plan, 'key', item => {
      const node = rowNode(item);
      paintRow(node, item);
      return node;
    }, paintRow);
    if (!plan.length) list.replaceChildren(h('p', { class: 'muted' }, T.ship.none));
    const n = plan.filter(p => selected.has(p.key)).length;
    runBtn.replaceChildren(icon('send'), T.ship.runN(n));
    runBtn.disabled = !n || Boolean(sent);
    runBtn.hidden = Boolean(sent);
    closeBtn.textContent = sent ? T.ship.close : T.cancel;
  }

  async function run() {
    if (sent || runBtn.disabled) return;
    const keys = plan.filter(p => selected.has(p.key)).map(p => p.key);
    runBtn.disabled = true;
    error.hidden = true;
    try {
      await post(ctx, keys);
      sent = keys;
      toast(T.ship.requested(keys.length));
    } catch (err) {
      error.textContent = (err && err.message) || String(err);
      error.hidden = false;
    }
    paint();
  }

  const unsubscribe = ctx.store.subscribe(() => { if (sent) paint(); });
  const { close, modal } = openModal(T.ship.batchTitle, [
    list,
    h('div', { class: 'field' }, h('label', null, T.ship.command), commandPreview(command)),
    h('p', { class: 'mtext muted' }, T.ship.hostNote),
    error,
  ], [h('span', { class: 'grow' }), closeBtn, runBtn], () => unsubscribe());
  modal.classList.add('shipmodal');
  paint();
  runBtn.focus();
  return close;
}
