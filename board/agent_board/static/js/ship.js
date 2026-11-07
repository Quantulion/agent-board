// Отправка в ревью командой проекта: действие карточки, массовая отправка одобренных и состояние отправки.
import { registerCardAction } from './actions.js';
import { spill, statusOf } from './chips.js';
import { h, icon, patchList } from './dom.js';
import { T, statusInfo } from './i18n.js';
import { isResearch } from './pane-kit.js';
import { stateClass } from './review.js';
import { openSettingsDialog } from './settings.js';
import { requestMove } from './status.js';
import { confirmDialog, openModal, toast } from './ui.js';

const KEYS = new Intl.Collator('ru', { numeric: true });
const PLACEHOLDERS = ['key', 'title', 'branch', 'base', 'worktree', 'repo', 'tracker_key', 'tracker_url'];
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

function repoOf(config, task) {
  const repos = (config.repos || []).filter(r => r && typeof r === 'object' && r.path);
  if (task.repo) return repos.find(r => r.name === task.repo) || null;
  return repos.length === 1 ? repos[0] : null;
}

// Что подставится в команду отправки задачи — по тем же правилам, что у сервера:
// {base} — ветка единственной одобренной или отправленной (ещё не влитой) зависимости, иначе база репозитория.
// error — почему отправка не пройдёт (несколько таких зависимостей).
export function shipValues(snapshot, task) {
  const config = snapshot.config;
  const command = shipCommand(snapshot) || '';
  const byKey = new Map(snapshot.tasks.map(t => [t.key, t]));
  const repo = repoOf(config, task);
  let base = repo ? repo.base || 'main' : '';
  let error = null;
  if (config.chains !== false) {
    const unmerged = (task.deps || []).map(d => byKey.get(d)).filter(d => d && (d.status === 'approved' || d.status === 'sent'));
    if (unmerged.length > 1) {
      error = T.ship.multiDeps(unmerged.map(d => d.key));
      base = '';
    } else if (unmerged.length === 1) {
      base = unmerged[0].branch || '';
    }
  }
  const all = {
    key: task.key, title: task.title, branch: task.branch || '', base, worktree: task.worktree || '', repo: repo ? repo.path : '',
    tracker_key: task.tracker_key || '', tracker_url: task.tracker_url || '',
  };
  const seen = new Set();
  const values = [];
  for (const p of commandParts(command)) {
    if (!p.ph || seen.has(p.text)) continue;
    seen.add(p.text);
    values.push({ ph: p.text, value: String(all[p.text.slice(1, -1)] ?? '') });
  }
  return { values, error };
}

const valuesList = model => [
  model.values.length ? h('dl', { class: 'kv shipvals' }, model.values.map(v => [h('dt', { class: 'mono' }, v.ph),
    h('dd', { class: v.value ? 'mono' : 'mono faint' }, v.value || T.ship.empty)])) : null,
  model.error ? h('div', { class: 'ferr' }, model.error) : null,
];

// Одобренная зависимость, которую нужно отправить раньше задачи.
const unsentDep = (snapshot, task) => {
  const byKey = new Map(snapshot.tasks.map(t => [t.key, t]));
  return (task.deps || []).find(d => byKey.get(d) && byKey.get(d).status === 'approved') || null;
};

export const commandPreview = command => h('pre', { class: 'cmd' },
  commandParts(command).map(p => (p.ph ? h('b', { class: 'ph' }, p.text) : p.text)));

const post = (ctx, keys) => ctx.client.post('/ship', { keys, actor: 'user' });

// Отправка одной задачи: подтверждение с командой и значениями плейсхолдеров.
export function shipOne(ctx, task) {
  const snap = ctx.store.get().snapshot;
  const command = shipCommand(snap);
  return confirmDialog({
    title: T.ship.title(task.key),
    body: [
      h('div', { class: 'field' }, h('label', null, T.ship.command), commandPreview(command || '')),
      h('div', { class: 'field' }, h('label', null, T.ship.values), valuesList(shipValues(snap, task))),
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
  label: () => T.ship.action,
  icon: 'send',
  visible: (task, snap) => !isResearch(task) && statusOf(task) === 'approved' && Boolean(shipCommand(snap)),
  enabled: (task, snap) => !isShipping(task) && !unsentDep(snap, task),
  title: (task, snap) => (isShipping(task) ? T.ship.chip[shipState(task)]
    : unsentDep(snap, task) ? T.ship.depFirst(unsentDep(snap, task)) : null),
  run: shipOne,
});

// Задачу с неотправленной одобренной зависимостью отправляют вместе с ней — из пакетной отправки.
registerCardAction({
  id: 'ship-batch',
  label: () => T.ship.openBatch,
  icon: 'send',
  visible: (task, snap) => !isResearch(task) && statusOf(task) === 'approved' && Boolean(shipCommand(snap))
    && !isShipping(task) && Boolean(unsentDep(snap, task)),
  run: ctx => ctx.openShip(),
});

// Одобренная задача без команды отправки: подсказка, что делать с веткой дальше; null — подсказка не нужна.
export function shipHint(task, snapshot) {
  if (!task || isResearch(task) || statusOf(task) !== 'approved' || !snapshot || shipCommand(snapshot)) return null;
  return { branch: task.branch || '' };
}

// Ссылка на настройки проекта с полем команды отправки; клик не открывает карточку под ней.
const settingsLink = (ctx, text) => h('button', {
  class: 'linkbtn', type: 'button', 'data-action': 'ship-settings',
  onclick: e => { e.stopPropagation(); openShipSettings(ctx); },
}, text);

const branchNode = hint => (hint.branch ? h('code', { class: 'br' }, hint.branch) : T.ship.hint.noBranch);

// Подсказка без команды отправки: полная — в открытой карточке, compact — на карточке доски.
export function shipHintNode(ctx, hint, compact = false) {
  const H = T.ship.hint;
  const done = statusInfo('done').label;
  if (compact) {
    const title = H.noCommand + ' ' + H.lead + (hint.branch || H.noBranch) + '. ' + H.setup + H.settings + H.manual(done);
    return h('div', { class: 'shiphint', title },
      icon('branch'), h('span', null, H.short, branchNode(hint), ' · ', settingsLink(ctx, H.setupShort)));
  }
  return h('div', { class: 'banner shiphint' }, icon('send'),
    h('span', null, h('b', null, H.noCommand), ' ', H.lead, branchNode(hint), '. ', H.setup, settingsLink(ctx, H.settings), H.manual(done)),
    h('button', { class: 'btn sm primary', type: 'button', 'data-action': 'ship-setup-banner', onclick: () => openShipSettings(ctx) },
      icon('gear'), H.setupButton));
}

// Настройки проекта, открытые на команде отправки.
export const openShipSettings = ctx => openSettingsDialog(ctx, { focus: 'ship' });

// Без команды отправки вместо кнопки отправки — переход к её настройке.
registerCardAction({
  id: 'ship-setup',
  label: () => T.ship.hint.setupButton,
  icon: 'gear',
  visible: (task, snap) => Boolean(shipHint(task, snap)),
  run: ctx => openShipSettings(ctx),
});

// Без команды отправки одобренную задачу вливают вручную и закрывают сами.
registerCardAction({
  id: 'mark-done',
  label: () => T.ship.hint.markDone(statusInfo('done').label),
  icon: 'check',
  visible: (task, snap) => Boolean(shipHint(task, snap)),
  run: (ctx, task) => requestMove(ctx, task, 'done'),
});

// Строка результата отправки: состояние, код выхода, причина и хвост вывода.
function resultNodes(task) {
  const s = task && task.ship;
  if (!s || typeof s !== 'object') return [];
  const state = shipState(task);
  const failed = state === 'error' || state === 'interrupted' || state === 'cancelled';
  const tail = typeof s.output_tail === 'string' ? s.output_tail : '';
  return [
    h('span', { class: 'shipst ' + stateClass(state) }, T.ship.state[state] || state || ''),
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
    const task = snap.tasks.find(t => t.key === item.key);
    const node = h('div', { class: 'shiprow', 'data-key': item.key },
      h('label', { class: 'shead' }, box, h('span', { class: 'k' }, item.key), h('span', { class: 'tt' }, item.title)),
      h('div', { class: 'res' }),
      task ? h('details', { class: 'vals' }, h('summary', null, T.ship.values), valuesList(shipValues(snap, task))) : null);
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
