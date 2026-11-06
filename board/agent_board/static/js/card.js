// Карточка задачи: выезжающая панель поверх доски с вкладками «Обзор / Тред / Ревью / Изменения».
// Панель и вкладки создаются один раз на открытую карточку; живые обновления правят их на месте.
import { actionsSignature, cardActions, createActionRunner, registerCardAction } from './actions.js';
import { spill, statusOf } from './chips.js';
import { h, icon } from './dom.js';
import { T, statusInfo } from './i18n.js';
import { requestMove, statusMenu } from './status.js';
import { createThread, draftsFor } from './thread.js';
import { toast } from './ui.js';
import { changesPane } from './changes.js';
import { overviewPane } from './overview.js';
import { copyText, isResearch, region } from './pane-kit.js';
import { reviewPane } from './review-pane.js';
import { trackerChip, trackerLink } from './tracker.js';
// действия шапки карточки регистрируются при загрузке модулей
import './review.js';
import './ship.js';

export { authorOf, createDrafts, formatTime } from './thread.js';

const WIDTH_KEY = 'ab-dw';
const TAB_KEY = 'ab-card-tab';
const MIN_WIDTH = 420;

registerCardAction({
  id: 'close-research',
  label: () => T.card.closeResearch,
  icon: 'check',
  visible: isResearch,
  run: (ctx, task) => requestMove(ctx, task, 'done'),
});

// --- Чистые функции ---

export function tabsFor(task, filesCount) {
  const files = filesCount ?? (task.changed_files || []).length;
  const round = task.review && task.review.round;
  const tabs = [
    { id: 'overview', label: T.card.tabs.overview, count: task.open_questions > 0 ? '? ' + task.open_questions : '', cls: 'q' },
    { id: 'thread', label: T.card.tabs.thread, count: task.unread > 0 ? String(task.unread) : '', cls: 'unread' },
    { id: 'review', label: T.card.tabs.review, count: round ? T.roundShort(round) : '', cls: '' },
    { id: 'changes', label: T.card.tabs.changes, count: files ? String(files) : '', cls: '' },
  ];
  return isResearch(task) ? tabs.slice(0, 2) : tabs;
}

// Открытый вопрос важнее всего; иначе — вкладка, на которой пользователь был в прошлый раз.
export function initialTab(task, remembered) {
  if (task.open_questions > 0) return 'overview';
  return tabsFor(task).some(t => t.id === remembered) ? remembered : 'overview';
}

// --- Мелкие помощники DOM ---

function storageGet(store, key) {
  try {
    return globalThis[store].getItem(key);
  } catch (e) {
    return null;
  }
}

function storageSet(store, key, value) {
  try {
    globalThis[store].setItem(key, value);
  } catch (e) {
    // хранилище недоступно — настройка живёт до перезагрузки
  }
}

// --- Открытая карточка ---

let current = null;

export function openCard(ctx, key) {
  if (current && current.key === key && current.ctx === ctx) return;
  const returnFocus = current ? current.returnFocus : document.activeElement;
  closeCard(false);
  current = createCard(ctx, key, returnFocus);
}

export function closeCard(restoreFocus = true) {
  if (!current) return;
  const card = current;
  current = null;
  card.destroy(restoreFocus);
}

function createCard(ctx, key, returnFocus) {
  const { store, client, slug } = ctx;
  const drafts = draftsFor(slug);
  const cleanups = [];
  const self = { key, ctx, returnFocus, destroy };
  let ui = null;

  const findTask = () => {
    const snap = store.get().snapshot;
    return snap ? snap.tasks.find(t => t.key === key) || null : undefined;
  };

  function onState(state) {
    const t = findTask();
    if (t === undefined) return;
    if (t === null) {
      if (!ui) {
        toast(T.card.notFound(key), { error: true });
        ctx.closeCard({ replace: true });
      }
      return;
    }
    if (!ui) ui = build(t);
    ui.paint(t, state.snapshot);
  }

  function destroy(restoreFocus) {
    cleanups.forEach(fn => fn());
    if (ui) ui.host.remove();
    if (restoreFocus && returnFocus && returnFocus.isConnected && typeof returnFocus.focus === 'function') {
      returnFocus.focus({ preventScroll: true });
    }
  }

  cleanups.push(store.subscribe(onState));
  cleanups.push(store.onEvent(e => { if (ui) ui.onEvent(e); }));
  onState(store.get());
  return self;

  // --- построение панели ---

  function build(first) {
    const scrim = h('div', { class: 'dscrim', onclick: () => ctx.closeCard() });
    const drawer = h('div', { class: 'drawer', role: 'dialog', 'aria-label': key, 'data-key': key, tabindex: '-1' });
    const savedWidth = Number(storageGet('localStorage', WIDTH_KEY));
    if (savedWidth >= MIN_WIDTH) drawer.style.setProperty('--dw', savedWidth + 'px');
    const handle = h('div', { class: 'dhandle', title: T.card.resize });
    const head = header(first);
    const banners = region('div', 'banners');
    const tabsBar = h('div', { class: 'tabs', role: 'tablist' });
    const body = h('div', { class: 'dbody', role: 'tabpanel' });
    drawer.append(handle, head.el, banners.el, tabsBar, body);
    const host = h('div', { class: 'card-host' }, scrim, drawer);
    document.body.append(host);
    bindResize(handle, drawer);

    const panes = {};
    const pc = {
      key, ctx, client, slug, drafts,
      task: () => task,
      isVisible: id => isVisible(id),
      alive: () => current === self,
      paintTabs: () => paintTabs(),
      showTab: id => showTab(id),
    };
    const builders = { overview: () => overviewPane(pc), thread: threadPane, review: () => reviewPane(pc), changes: () => changesPane(pc) };
    const scrolls = {};
    let task = first;
    let snapshot = store.get().snapshot;
    let tab = null;

    const tabButtons = new Map();
    for (const t of tabsFor(first)) {
      const count = h('span', { class: 'tb', hidden: true });
      const btn = h('button', {
        type: 'button', role: 'tab', 'data-tab': t.id, 'aria-selected': 'false', tabindex: '-1',
        onclick: () => showTab(t.id),
        onkeydown: e => {
          if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
          const ids = [...tabButtons.keys()];
          const next = ids[(ids.indexOf(t.id) + (e.key === 'ArrowRight' ? 1 : ids.length - 1)) % ids.length];
          showTab(next);
          tabButtons.get(next).btn.focus();
        },
      }, t.label, count);
      tabButtons.set(t.id, { btn, count });
      tabsBar.append(btn);
    }

    function paintTabs() {
      const files = panes.changes ? panes.changes.filesCount() : null;
      for (const t of tabsFor(task, files)) {
        const ref = tabButtons.get(t.id);
        if (!ref) continue;
        ref.count.textContent = t.count;
        ref.count.className = t.cls ? 'tb ' + t.cls : 'tb';
        ref.count.hidden = !t.count;
      }
    }

    function showTab(id) {
      if (!tabButtons.has(id)) id = 'overview';
      if (id === tab) return;
      if (tab) scrolls[tab] = body.scrollTop;
      tab = id;
      storageSet('localStorage', TAB_KEY, id);
      if (!panes[id]) {
        panes[id] = builders[id]();
        panes[id].paint(task, snapshot);
      }
      body.replaceChildren(panes[id].el);
      for (const [tid, ref] of tabButtons) {
        ref.btn.setAttribute('aria-selected', String(tid === id));
        ref.btn.tabIndex = tid === id ? 0 : -1;
      }
      body.scrollTop = scrolls[id] ?? 0;
      if (panes[id].shown) panes[id].shown();
    }

    // Esc закрывает сначала меню статуса, потом карточку; модальные окна перехватывают Esc раньше.
    const onKey = e => {
      // Esc во время набора через IME отменяет композицию, а не закрывает карточку
      if (e.key !== 'Escape' || e.defaultPrevented || e.isComposing || e.keyCode === 229) return;
      if (head.menuOpen()) {
        head.closeMenu(true);
        return;
      }
      ctx.closeCard();
    };
    document.addEventListener('keydown', onKey);
    cleanups.push(() => document.removeEventListener('keydown', onKey));
    const onDocClick = e => { if (!e.target.closest || !e.target.closest('.dh .stmenu')) head.closeMenu(false); };
    document.addEventListener('click', onDocClick);
    cleanups.push(() => document.removeEventListener('click', onDocClick));

    showTab(initialTab(first, storageGet('localStorage', TAB_KEY)));
    drawer.focus({ preventScroll: true });

    return {
      host,
      paint(t, snap) {
        task = t;
        snapshot = snap;
        head.paint(t, snap);
        banners.paint(bannerModel(t, snap), () => bannerNodes(t, snap));
        for (const p of Object.values(panes)) p.paint(t, snap);
        paintTabs();
      },
      onEvent(e) {
        for (const p of Object.values(panes)) if (p.onEvent) p.onEvent(e);
      },
    };

    // --- вкладки ---

    function isVisible(id) {
      return tab === id && document.visibilityState !== 'hidden';
    }

    function threadPane() {
      const sessionNote = h('span', { class: 'r faint' });
      const thread = createThread({
        client, thread: key, drafts, agentName: key,
        scroller: () => body,
        visible: () => isVisible('thread'),
        alive: () => current === self,
        placeholder: T.card.composer(key),
      });
      const el = h('div', { class: 'pane' },
        h('section', { class: 'sec' }, h('h3', null, T.card.threadTitle, sessionNote), thread.list, thread.composer));
      return {
        el,
        paint(t) {
          sessionNote.textContent = t.session_id ? T.card.sessionShort(t.session_id.slice(0, 8)) : '';
          thread.paint({ size: t.thread_size, unread: t.unread });
        },
        shown: thread.shown,
      };
    }

  }

  // --- шапка ---

  function header(first) {
    const keySlot = h('span', { class: 'slot' });
    const chips = h('span', { class: 'slot' });
    const statusBtn = h('button', { class: 'stbtn', type: 'button', 'aria-haspopup': 'menu', 'aria-label': T.statusMenuAria });
    const menuSlot = h('div', { class: 'slot' });
    const stmenu = h('div', { class: 'stmenu' }, statusBtn, menuSlot);
    const title = h('h2');
    const terminal = h('button', { class: 'btn', type: 'button', onclick: () => { if (lastTask.terminal_command) copyText(lastTask.terminal_command); } },
      icon('term'), T.card.terminal);
    const acts = h('span', { class: 'slot' });
    let actsSig = null;
    let lastSnap = null;
    const runner = createActionRunner(message => toast(message, { error: true }));
    const el = h('div', { class: 'dh' },
      h('div', { class: 'l1' }, keySlot, chips, stmenu, h('span', { class: 'sp' }),
        h('button', { class: 'btn icon ghost', type: 'button', 'aria-label': T.close, title: T.close, onclick: () => ctx.closeCard() }, icon('x'))),
      title,
      h('div', { class: 'acts' }, acts, h('span', { class: 'grow' }), terminal));
    let lastTask = first;
    let sig = null;
    let menu = null;

    // кнопки действий пересобираются, только когда меняется их набор или состояние
    function repaintActions() {
      actsSig = null;
      paintActions(lastTask, lastSnap);
    }

    function paintActions(t, snap) {
      lastSnap = snap;
      const list = cardActions(t, snap);
      const next = actionsSignature(list);
      if (next === actsSig) return;
      actsSig = next;
      const focused = acts.contains(document.activeElement) ? document.activeElement.dataset.action : null;
      acts.replaceChildren(...list.map(a => {
        const btn = h('button', {
          class: 'btn', type: 'button', 'data-action': a.id, title: a.title,
          onclick: () => runner.click(a.id, btn, () => a.run(ctx, lastTask)).then(() => repaintActions()),
        }, a.icon ? icon(a.icon) : null, a.label);
        btn.disabled = !a.enabled || runner.busy(a.id);
        return btn;
      }));
      // перерисовка не должна сбивать фокус с кнопки, которая осталась в наборе
      const kept = focused && [...acts.children].find(b => b.dataset.action === focused && !b.disabled);
      if (kept) kept.focus();
    }

    function closeMenu(refocus) {
      if (!menu) return;
      menu = null;
      menuSlot.replaceChildren();
      statusBtn.setAttribute('aria-expanded', 'false');
      if (refocus) statusBtn.focus();
    }

    statusBtn.addEventListener('click', e => {
      e.stopPropagation();
      if (menu) {
        closeMenu(false);
        return;
      }
      menu = statusMenu(statusOf(lastTask), to => {
        closeMenu(true);
        requestMove(ctx, lastTask, to);
      });
      menu.addEventListener('focusout', ev => { if (menu && !stmenu.contains(ev.relatedTarget)) closeMenu(false); });
      menuSlot.replaceChildren(menu);
      statusBtn.setAttribute('aria-expanded', 'true');
      const cur = menu.querySelector('button.cur') || menu.querySelector('button');
      if (cur) cur.focus();
    });

    return {
      el,
      menuOpen: () => Boolean(menu),
      closeMenu,
      paint(t, snap) {
        lastTask = t;
        paintActions(t, snap);
        const next = JSON.stringify([t.title, t.tracker_key, t.tracker_url, t.repo, t.kind, t.estimate, statusOf(t), t.terminal_command]);
        if (next === sig) return;
        sig = next;
        keySlot.replaceChildren(h('span', { class: 'key' }, key));
        // replaceChildren превратил бы null в текст «null»
        chips.replaceChildren(...[
          trackerChip(trackerLink(t)),
          t.repo ? h('span', { class: 'chip repo' }, t.repo) : null,
          h('span', { class: 'chip' }, icon(isResearch(t) ? 'flask' : 'code'), isResearch(t) ? T.kindResearch : T.kindCode),
          t.estimate !== null && t.estimate !== undefined ? h('span', { class: 'chip' }, T.estimate(t.estimate)) : null,
        ].filter(Boolean));
        statusBtn.replaceChildren(spill(statusOf(t)), icon('chev'));
        title.textContent = t.title;
        terminal.disabled = !t.terminal_command;
        terminal.title = t.terminal_command || T.card.noTerminal;
      },
    };
  }

  // --- плашки над вкладками: очередь, причины блокировки, остановленная переписка ---

  function bannerModel(t, snap) {
    const byKey = new Map(snap.tasks.map(x => [x.key, statusOf(x)]));
    return [statusOf(t), t.locks, t.open_deps, (t.open_deps || []).map(k => byKey.get(k)), t.external_blocker, t.agent_parked];
  }

  function bannerNodes(t, snap) {
    const nodes = [];
    const byKey = new Map(snap.tasks.map(x => [x.key, x]));
    const waiting = (t.locks || []).find(l => l.position > 0);
    if (waiting) nodes.push(h('div', { class: 'banner queue' }, icon('clock'), h('span', null, T.card.queue(waiting.resource, waiting.position))));
    for (const l of (t.locks || []).filter(x => x.position === 0)) {
      nodes.push(h('div', { class: 'banner queue' }, icon('lock'), h('span', null, T.card.holds(l.resource))));
    }
    const ext = typeof t.external_blocker === 'string' ? t.external_blocker.trim() : '';
    if (statusOf(t) === 'blocked' || ext) {
      const deps = (t.open_deps || []).map(k => {
        const d = byKey.get(k);
        return d ? k + ' (' + statusInfo(statusOf(d)).label + ')' : k;
      });
      const reasons = [deps.length ? T.card.waits(deps.join(', ')) : '', ext ? T.card.external(ext) : ''].filter(Boolean);
      nodes.push(h('div', { class: 'banner blocked' }, icon('lock'),
        h('span', null, h('b', null, T.card.whyBlocked), ' ', reasons.join('; ') || T.card.depsClosed)));
    }
    if (t.agent_parked) nodes.push(h('div', { class: 'banner parked' }, icon('chat'), h('span', null, T.card.parked)));
    return nodes;
  }

  function bindResize(handle, drawer) {
    handle.addEventListener('pointerdown', e => {
      e.preventDefault();
      handle.setPointerCapture(e.pointerId);
      handle.classList.add('drag');
      document.body.classList.add('resizing');
      const move = ev => {
        const w = Math.min(window.innerWidth - 40, Math.max(MIN_WIDTH, window.innerWidth - ev.clientX));
        drawer.style.setProperty('--dw', Math.round(w) + 'px');
      };
      const up = () => {
        handle.removeEventListener('pointermove', move);
        handle.removeEventListener('pointerup', up);
        handle.removeEventListener('pointercancel', up);
        handle.classList.remove('drag');
        document.body.classList.remove('resizing');
        storageSet('localStorage', WIDTH_KEY, String(Math.round(drawer.getBoundingClientRect().width)));
      };
      handle.addEventListener('pointermove', move);
      handle.addEventListener('pointerup', up);
      handle.addEventListener('pointercancel', up);
    });
  }
}
