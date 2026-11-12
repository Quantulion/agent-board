// Панель «Проект»: тред с агентом проекта, его состояние и предложения: изменения графа задач и действия пользователя.
import { h, icon, patchList } from './dom.js';
import { T, statusInfo } from './i18n.js';
import { threadAgentModel } from './agent-status.js';
import { createThread, draftsFor } from './thread.js';
import { toast } from './ui.js';

export const PROJECT_THREAD = '_project';
const DOCK_KEY = 'ab-dock';
// на узком экране панель выезжает поверх доски и закрывается, как всплывающее окно
const NARROW = '(max-width: 900px)';

const str = v => (typeof v === 'string' ? v : '');
const strList = v => (Array.isArray(v) ? v.filter(x => typeof x === 'string') : []);

// --- Чистые функции ---

// Части описания — строки и ссылки на задачи {key, exists}; partsText склеивает их в текст.
export function partsText(parts) {
  return parts.map(p => (typeof p === 'string' ? p : p.key)).join('');
}

function keyList(keys, byKey) {
  const out = [];
  keys.forEach((k, i) => {
    if (i) out.push(', ');
    out.push({ key: k, exists: byKey.has(k) });
  });
  return out;
}

const keyRef = (k, byKey) => ({ key: k || '?', exists: Boolean(k) && byKey.has(k) });

const MAX_VALUE = 80;

// Значение настройки одной строкой: длинные составные значения обрезаются
function valueText(v) {
  if (v === undefined || v === null || v === '') return T.proposal.unset;
  const text = typeof v === 'string' ? '«' + v + '»' : JSON.stringify(v);
  return text.length > MAX_VALUE ? text.slice(0, MAX_VALUE - 1) + '…' : text;
}

const titleOf = (key, byKey) => (byKey.has(key) ? ' «' + byKey.get(key).title + '»' : '');
const stageLabel = id => T.stageLabels[id] || String(id || '?');
const expectOf = p => (p.expect && typeof p.expect === 'object' ? p.expect : {});

// Что изменится, если применить предложение: заголовок и строки изменений человеческим текстом.
export function describeProposal(p, byKey) {
  const payload = p && p.payload && typeof p.payload === 'object' ? p.payload : {};
  const P = T.proposal;
  if (p.kind === 'add_task') {
    const t = payload.task && typeof payload.task === 'object' ? payload.task : {};
    const deps = strList(t.deps);
    const facts = [str(t.repo), t.kind === 'research' ? T.kindResearch.toLowerCase() : T.kindCode.toLowerCase()].filter(Boolean);
    if (typeof t.estimate === 'number') facts.push('~' + T.estimate(t.estimate));
    return {
      title: [P.newTask, keyRef(str(t.key), byKey), ' «' + str(t.title) + '»'],
      changes: [{ op: 'add', label: P.opTask, parts: [facts.join(' · '), ' · ', ...(deps.length ? [P.dependsOn, ...keyList(deps, byKey)] : [P.noDeps])] }],
    };
  }
  if (p.kind === 'set_deps') {
    const key = str(payload.key);
    const next = strList(payload.deps);
    const task = byKey.get(key);
    const before = task ? strList(task.deps) : [];
    const added = next.filter(k => !before.includes(k));
    const removed = before.filter(k => !next.includes(k));
    const changes = [];
    if (added.length) changes.push({ op: 'add', label: P.opLinkAdd, parts: [P.willWait, ...keyList(added, byKey)] });
    if (removed.length) changes.push({ op: 'del', label: P.opLinkDel, parts: [P.noLongerWaits, ...keyList(removed, byKey)] });
    return { title: [keyRef(key, byKey), P.depsArrow, ...(next.length ? keyList(next, byKey) : [P.none])], changes };
  }
  if (p.kind === 'cancel_task') {
    const key = str(payload.key);
    const task = byKey.get(key);
    const dependents = [...byKey.values()].filter(t => strList(t.deps).includes(key)).map(t => t.key);
    return {
      title: [P.cancel, keyRef(key, byKey), task ? ' «' + task.title + '»' : ''],
      changes: dependents.length ? [{ op: 'del', label: P.opCancel, parts: [P.dropsFrom, ...keyList(dependents, byKey)] }] : [],
    };
  }
  if (p.kind === 'set_config') {
    const fields = payload.fields && typeof payload.fields === 'object' ? payload.fields : {};
    const before = expectOf(p).config || {};
    return {
      title: [P.config],
      changes: Object.keys(fields).sort().map(name => ({
        op: 'mod', label: P.opConfig,
        parts: [(T.settings.labels[name] || name) + ' (' + name + '): ', valueText(before[name]), ' → ', valueText(fields[name])],
      })),
    };
  }
  if (p.kind === 'set_status') {
    const key = str(payload.key);
    const task = byKey.get(key);
    const from = str(payload.from) || str(expectOf(p).status) || (task ? task.status : '');
    return {
      title: [P.status, keyRef(key, byKey), titleOf(key, byKey)],
      changes: [{ op: 'mod', label: P.opStatus, parts: [from ? statusInfo(from).label : '?', ' → ', statusInfo(str(payload.to)).label] }],
    };
  }
  if (p.kind === 'ship') {
    const keys = strList(payload.keys);
    return {
      title: [P.ship, ...keyList(keys, byKey)],
      changes: keys.map(k => ({ op: 'mod', label: P.opShip, parts: [keyRef(k, byKey), titleOf(k, byKey)] })),
    };
  }
  if (p.kind === 'stage') {
    return {
      title: [P.stage + '«' + stageLabel(payload.stage) + '»'],
      changes: [{ op: 'mod', label: P.opStage, parts: [stageLabel(expectOf(p).stage), ' → ', stageLabel(payload.stage)] }],
    };
  }
  if (p.kind === 'retry') {
    const key = str(payload.key);
    if (key === PROJECT_THREAD) return { title: [P.retryProject], changes: [] };
    const task = byKey.get(key);
    const error = task ? str(task.turn_error) : '';
    return {
      title: [P.retry, keyRef(key, byKey), titleOf(key, byKey)],
      changes: error ? [{ op: 'mod', label: P.opRetry, parts: [P.lastError + error] }] : [],
    };
  }
  return { title: [String(p.kind || '?')], changes: [] };
}

// Открытые предложения, которые меняют задачу key: её зависимости, её отмену или новую задачу рядом с ней.
export function proposalsTouching(proposals, key) {
  return (proposals || []).filter(p => {
    if (p.status !== 'open') return false;
    const payload = p.payload || {};
    if (p.kind === 'add_task') {
      const t = payload.task || {};
      return t.key === key || strList(t.deps).includes(key);
    }
    if (p.kind === 'ship') return strList(payload.keys).includes(key);
    return payload.key === key;
  });
}

// Сигнатура блока предложения: описание считается от текущих зависимостей, и при их смене блок перерисовывается.
export function proposalSignature(p, byKey) {
  return JSON.stringify([p.id, p.status, describeProposal(p, byKey)]);
}

// Описание для показа: пока предложение открыто, оно запоминается в cache; обработанное показывается таким,
// каким его видел пользователь, — после применения текущий граф уже не отличается от предложенного.
export function describeFor(cache, p, byKey) {
  if (p.status === 'open') {
    const d = describeProposal(p, byKey);
    cache.set(p.id, d);
    return d;
  }
  return cache.get(p.id) || describeProposal(p, byKey);
}

export function agentStateLine(pa) {
  if (pa && pa.parked) return { cls: 'err', text: T.project.state.parked };
  const state = pa && pa.state;
  if (state === 'error') return { cls: 'err', text: T.project.state.error };
  if (state === 'running') return { cls: 'run', text: T.project.state.running };
  if (state === 'waiting_turn') return { cls: 'wait', text: T.project.state.waiting_turn };
  return { cls: 'idle', text: T.project.state.idle };
}

// --- Предложение: блок с кнопками «Применить» / «Отклонить» ---

// предложения, по которым запрос в пути или уже выполнен: кнопки в панели и карточке неактивны,
// пока следующий снимок не уберёт предложение из открытых
const pending = new Set();
const settled = new Set();
// последняя ошибка применения или отклонения: видна у предложения и после перерисовки, пока его не обработают
const failures = new Map();
// описания открытых предложений по проектам — для истории
const described = new Map();

function cacheFor(slug) {
  if (!described.has(slug)) described.set(slug, new Map());
  return described.get(slug);
}

const actionKey = (ctx, p) => ctx.slug + ':' + p.id;

function renderParts(ctx, parts) {
  return parts.map(p => {
    if (typeof p === 'string') return p;
    if (!p.exists) return h('span', { class: 'mono' }, p.key);
    return h('a', {
      class: 'mono', href: '#', onclick: e => { e.preventDefault(); ctx.openCard(p.key); },
    }, p.key);
  });
}

function changeRows(ctx, d) {
  return d.changes.length ? h('div', { class: 'chg' }, d.changes.map(c =>
    h('div', null, h('span', { class: 'op ' + c.op }, c.label), h('span', null, renderParts(ctx, c.parts))))) : null;
}

function actions(ctx, p, d) {
  const id = actionKey(ctx, p);
  const error = h('div', { class: 'ferr', role: 'alert', hidden: !failures.has(id) }, failures.get(id) || '');
  const act = action => {
    if (pending.has(id) || settled.has(id)) return;
    pending.add(id);
    buttons.forEach(b => { b.disabled = true; });
    error.hidden = true;
    failures.delete(id);
    ctx.client.post('/proposals/' + encodeURIComponent(p.id) + '/' + action)
      .then(() => {
        settled.add(id);
        toast(action === 'apply' ? T.proposal.appliedToast(partsText(d.title)) : T.proposal.rejectedToast);
      })
      .catch(err => {
        buttons.forEach(b => { b.disabled = false; });
        const text = T.proposal.failed(action, (err && err.message) || String(err));
        failures.set(id, text);
        error.textContent = text;
        error.hidden = false;
      })
      .finally(() => pending.delete(id));
  };
  const busy = pending.has(id) || settled.has(id);
  const buttons = [
    h('button', { class: 'btn ok sm', type: 'button', disabled: busy, onclick: () => act('apply') }, icon('check'), T.proposal.apply),
    h('button', { class: 'btn sm', type: 'button', disabled: busy, onclick: () => act('reject') }, T.proposal.reject),
  ];
  return [h('div', { class: 'pa-act' }, buttons), error];
}

// предложения, которые меняют граф задач; остальные — действия пользователя от его имени
const GRAPH_KINDS = new Set(['add_task', 'set_deps', 'cancel_task']);

// Итог обработанного предложения для истории; у открытого итога нет.
export function proposalOutcome(p) {
  const P = T.proposal;
  const graph = GRAPH_KINDS.has(p.kind);
  if (p.status === 'applied') return { status: 'applied', cls: 'ok', icon: 'check', text: graph ? P.applied : P.appliedAction };
  if (p.status === 'rejected') return { status: 'rejected', cls: 'muted', icon: 'x', text: graph ? P.rejected : P.rejectedAction };
  if (p.status === 'obsolete') {
    const why = P.obsoleteWhy[p.obsolete_reason];
    return { status: 'obsolete', cls: 'muted', icon: 'x', text: why ? P.obsolete + ': ' + why : P.obsolete };
  }
  return null;
}

// Предложение в панели «Проект»: открытое — с кнопками, обработанное — с итогом.
export function proposalCard(ctx, p, byKey) {
  const d = describeFor(cacheFor(ctx.slug), p, byKey);
  const reason = str(p.reason).trim();
  const outcome = proposalOutcome(p);
  const status = outcome ? outcome.status : 'open';
  return h('div', { class: 'proposal ' + status, 'data-id': p.id },
    h('div', { class: 'ph' }, icon('spark'), T.proposal.header),
    h('div', { class: 'pb' },
      h('div', { class: 'pt' }, renderParts(ctx, d.title)),
      changeRows(ctx, d),
      reason ? h('div', { class: 'why' }, T.proposal.why + reason) : null,
      outcome ? h('div', { class: 'res ' + outcome.cls }, icon(outcome.icon), outcome.text) : actions(ctx, p, d)));
}

// Предложение в карточке задачи, которую оно затрагивает.
export function proposalNote(ctx, p, byKey) {
  const d = describeFor(cacheFor(ctx.slug), p, byKey);
  const reason = str(p.reason).trim();
  return h('div', { class: 'pa-note', 'data-id': p.id },
    h('div', null, h('b', null, T.proposal.suggests), ' ', renderParts(ctx, d.title)),
    changeRows(ctx, d),
    reason ? h('div', { class: 'why' }, T.proposal.why + reason) : null,
    actions(ctx, p, d));
}

// --- Панель ---

// Где запоминается, закрыта ли панель: в проекте «Без плана» панель — главный способ заводить задачи,
// поэтому её состояние своё у каждого проекта и не зависит от других.
export function dockKey(kind, slug) {
  return kind === 'free' ? DOCK_KEY + ':' + slug : DOCK_KEY;
}

// Открыта ли панель: пока пользователь её не закрыл — открыта.
export function dockOpen(kind, slug) {
  try {
    return globalThis.localStorage.getItem(dockKey(kind, slug)) !== '0';
  } catch (e) {
    return true;
  }
}

// Подсказки поля ввода панели по типу проекта.
export function panelTexts(kind) {
  return kind === 'free'
    ? { placeholder: T.project.composerFree, hint: T.project.composerHintFree }
    : { placeholder: T.project.composer, hint: T.project.composerHint };
}

const narrow = () => Boolean(window.matchMedia && window.matchMedia(NARROW).matches);

// Панель справа от доски; свёрнутость запоминается. toggleButton — кнопка «Проект» в шапке:
// ей сообщается состояние панели, на неё возвращается фокус после сворачивания.
export function mountProjectPanel(ctx, store, toggleButton) {
  // тип проекта известен из снимка: до него панель ведёт себя как в проекте «По плану»
  let kind = ctx.kind || 'plan';
  let open = dockOpen(kind, ctx.slug) && !narrow();
  const onToggle = on => toggleButton.setAttribute('aria-pressed', String(on));
  let alive = true;
  const stateDot = h('span', { class: 'sdot' });
  const stateText = h('span');
  const scroll = h('div', { class: 'scroll' });
  const historyCount = h('span');
  const history = h('div', { class: 'phist' });
  const historyBox = h('details', { class: 'phistory', hidden: true }, h('summary', null, historyCount), history);
  const openList = h('div', { class: 'popen' });
  const thread = createThread({
    client: ctx.client, thread: PROJECT_THREAD, drafts: draftsFor(ctx.slug),
    scroller: () => scroll,
    visible: () => open && alive && document.visibilityState !== 'hidden',
    alive: () => alive,
    ...panelTexts(kind),
    parkedText: T.project.parked,
  });
  scroll.append(historyBox, ...thread.list, openList);
  const el = h('aside', { class: 'dock', 'aria-label': T.projectPanel, hidden: !open },
    h('header', null,
      h('span', { class: 'av' }, icon('spark')),
      h('div', { class: 'ptitle' }, h('h3', null, T.projectPanel),
        h('div', { class: 'sub pstate' }, stateDot, T.project.agent + ' · ', stateText)),
      h('span', { class: 'grow' }),
      h('button', { class: 'btn icon ghost sm', type: 'button', 'aria-label': T.collapse, title: T.collapse, onclick: () => collapse() }, icon('x'))),
    scroll,
    h('div', { class: 'foot' }, ...thread.composer));

  function collapse() {
    setOpen(false);
    if (toggleButton.isConnected) toggleButton.focus();
  }

  const onKey = e => {
    if (e.key !== 'Escape' || e.defaultPrevented || e.isComposing || !open || !narrow()) return;
    // карточка и модальные окна поверх панели закрываются своим Esc
    if (document.querySelector('.drawer, .modal-host')) return;
    collapse();
  };
  // щелчок, которым панель только что открыли (пункт «Ждут вас» и т. п.), всплывает до документа — его пропускаем
  let currentClick = null;
  let openingClick = null;
  const onClickStart = e => { currentClick = e; };
  const onDocClick = e => {
    if (e === openingClick || !open || !narrow() || el.contains(e.target) || toggleButton.contains(e.target)) return;
    if (e.target.closest && e.target.closest('.drawer, .modal-host, .toasts')) return;
    setOpen(false);
  };
  document.addEventListener('keydown', onKey);
  window.addEventListener('click', onClickStart, true);
  document.addEventListener('click', onDocClick);

  function setOpen(on) {
    if (on && !open) openingClick = currentClick;
    open = on;
    el.hidden = !on;
    try {
      window.localStorage.setItem(dockKey(kind, ctx.slug), on ? '1' : '0');
    } catch (e) {
      // хранилище недоступно — состояние живёт до перезагрузки
    }
    onToggle(on);
    if (on) {
      thread.shown();
      paint(store.get());
    }
  }

  function paint(state) {
    const snap = state.snapshot;
    if (!snap) return;
    const pa = snap.project_agent || {};
    const line = agentStateLine(pa);
    stateDot.className = 'sdot ' + line.cls;
    stateText.textContent = line.text;

    const byKey = new Map(snap.tasks.map(t => [t.key, t]));
    const proposals = snap.proposals || [];
    const openOnes = proposals.filter(p => p.status === 'open');
    const done = proposals.filter(p => p.status !== 'open').reverse();
    patchList(openList, openOnes, p => proposalSignature(p, byKey), p => proposalCard(ctx, p, byKey));
    // обработанные не меняются: их описание заморожено в момент обработки
    patchList(history, done, p => p.id + ':' + p.status, p => proposalCard(ctx, p, byKey));
    historyBox.hidden = !done.length;
    historyCount.textContent = T.project.history(done.length);
    thread.paint({ size: snap.project_thread_size, unread: snap.project_unread, agent: threadAgentModel(pa, snap.config) });
  }

  const unsubscribe = store.subscribe(paint);
  paint(store.get());
  onToggle(open);
  if (open) thread.shown();

  return {
    el,
    isOpen: () => open,
    toggle: () => setOpen(!open),
    // тип проекта пришёл со снимком: подсказки поля ввода и открытость панели — по нему
    setKind(next) {
      if (next === kind) return;
      kind = next;
      const texts = panelTexts(kind);
      thread.area.placeholder = texts.placeholder;
      thread.area.setAttribute('aria-label', texts.placeholder);
      if (thread.hint) thread.hint.textContent = texts.hint;
      const want = dockOpen(kind, ctx.slug) && !narrow();
      if (want !== open) {
        open = want;
        el.hidden = !open;
        onToggle(open);
        if (open) {
          thread.shown();
          paint(store.get());
        }
      }
    },
    // показать панель с открытыми предложениями
    reveal() {
      if (!open) setOpen(true);
      const first = openList.firstElementChild;
      if (first) first.scrollIntoView({ block: 'nearest' });
      else scroll.scrollTop = scroll.scrollHeight;
    },
    destroy() {
      alive = false;
      unsubscribe();
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('click', onDocClick);
      window.removeEventListener('click', onClickStart, true);
    },
  };
}
