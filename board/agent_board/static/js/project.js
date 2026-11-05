// Панель «Проект»: тред с агентом проекта, его состояние и предложения по изменению графа задач.
import { h, icon, patchList } from './dom.js';
import { T } from './i18n.js';
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
  return { cls: 'idle', text: T.project.state.idle };
}

// --- Предложение: блок с кнопками «Применить» / «Отклонить» ---

// предложения, по которым запрос в пути или уже выполнен: кнопки в панели и карточке неактивны,
// пока следующий снимок не уберёт предложение из открытых
const pending = new Set();
const settled = new Set();
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
  const error = h('div', { class: 'ferr', role: 'alert', hidden: true });
  const id = actionKey(ctx, p);
  const act = action => {
    if (pending.has(id) || settled.has(id)) return;
    pending.add(id);
    buttons.forEach(b => { b.disabled = true; });
    error.hidden = true;
    ctx.client.post('/proposals/' + encodeURIComponent(p.id) + '/' + action)
      .then(() => {
        settled.add(id);
        toast(action === 'apply' ? T.proposal.appliedToast(partsText(d.title)) : T.proposal.rejectedToast);
      })
      .catch(err => {
        buttons.forEach(b => { b.disabled = false; });
        error.textContent = (err && err.message) || String(err);
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

// Предложение в панели «Проект»: открытое — с кнопками, обработанное — с итогом.
export function proposalCard(ctx, p, byKey) {
  const d = describeFor(cacheFor(ctx.slug), p, byKey);
  const reason = str(p.reason).trim();
  const status = p.status === 'applied' || p.status === 'rejected' ? p.status : 'open';
  return h('div', { class: 'proposal ' + status, 'data-id': p.id },
    h('div', { class: 'ph' }, icon('spark'), T.proposal.header),
    h('div', { class: 'pb' },
      h('div', { class: 'pt' }, renderParts(ctx, d.title)),
      changeRows(ctx, d),
      reason ? h('div', { class: 'why' }, T.proposal.why + reason) : null,
      status === 'open' ? actions(ctx, p, d)
        : status === 'applied' ? h('div', { class: 'res ok' }, icon('check'), T.proposal.applied)
          : h('div', { class: 'res muted' }, icon('x'), T.proposal.rejected)));
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

function storedOpen() {
  try {
    return window.localStorage.getItem(DOCK_KEY) !== '0';
  } catch (e) {
    return true;
  }
}

const narrow = () => Boolean(window.matchMedia && window.matchMedia(NARROW).matches);

// Панель справа от доски; свёрнутость запоминается. toggleButton — кнопка «Проект» в шапке:
// ей сообщается состояние панели, на неё возвращается фокус после сворачивания.
export function mountProjectPanel(ctx, store, toggleButton) {
  let open = storedOpen() && !narrow();
  const onToggle = on => toggleButton.setAttribute('aria-pressed', String(on));
  let alive = true;
  const stateDot = h('span', { class: 'sdot' });
  const stateText = h('span');
  const scroll = h('div', { class: 'scroll' });
  const banners = h('div', { class: 'pbanners', hidden: true });
  const historyCount = h('span');
  const history = h('div', { class: 'phist' });
  const historyBox = h('details', { class: 'phistory', hidden: true }, h('summary', null, historyCount), history);
  const openList = h('div', { class: 'popen' });
  const thread = createThread({
    client: ctx.client, thread: PROJECT_THREAD, drafts: draftsFor(ctx.slug),
    scroller: () => scroll,
    visible: () => open && alive && document.visibilityState !== 'hidden',
    alive: () => alive,
    placeholder: T.project.composer,
    hint: T.project.composerHint,
  });
  scroll.append(banners, historyBox, ...thread.list, openList);
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
  const onDocClick = e => {
    if (!open || !narrow() || el.contains(e.target) || toggleButton.contains(e.target)) return;
    if (e.target.closest && e.target.closest('.drawer, .modal-host, .toasts')) return;
    setOpen(false);
  };
  document.addEventListener('keydown', onKey);
  document.addEventListener('click', onDocClick);

  function setOpen(on) {
    open = on;
    el.hidden = !on;
    try {
      window.localStorage.setItem(DOCK_KEY, on ? '1' : '0');
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
    const notes = [];
    if (pa.turn_error) notes.push(h('div', { class: 'ferr' }, h('b', null, T.card.turnError + ': '), pa.turn_error));
    if (pa.parked) notes.push(h('div', { class: 'pa-note' }, T.project.parked));
    banners.replaceChildren(...notes);
    banners.hidden = !notes.length;

    const byKey = new Map(snap.tasks.map(t => [t.key, t]));
    const proposals = snap.proposals || [];
    const openOnes = proposals.filter(p => p.status === 'open');
    const done = proposals.filter(p => p.status !== 'open').reverse();
    patchList(openList, openOnes, p => proposalSignature(p, byKey), p => proposalCard(ctx, p, byKey));
    // обработанные не меняются: их описание заморожено в момент обработки
    patchList(history, done, p => p.id + ':' + p.status, p => proposalCard(ctx, p, byKey));
    historyBox.hidden = !done.length;
    historyCount.textContent = T.project.history(done.length);
    thread.paint({ size: snap.project_thread_size, unread: snap.project_unread });
  }

  const unsubscribe = store.subscribe(paint);
  paint(store.get());
  onToggle(open);
  if (open) thread.shown();

  return {
    el,
    isOpen: () => open,
    toggle: () => setOpen(!open),
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
    },
  };
}
