// Доска проекта: шапка, «Ждут вас», колонки по статусам, карточки и их перенос между статусами.
import { h, icon, patchList } from './dom.js';
import { AGENTS_LOOP_PREFIX, STAGES, STATUSES, T, statusInfo } from './i18n.js';

const KEYS = new Intl.Collator('ru', { numeric: true });
const byKeyOrder = (a, b) => KEYS.compare(a.key, b.key);
const DEFAULT_BASE = 'main';
const WORKING = new Set(['in_progress', 'waiting_answer', 'queued', 'in_review']);
const SHIPPED = new Set(['approved', 'sent']);
// порядок отрезков полосы прогресса: от готового к ещё не начатому
const BAR_ORDER = ['done', 'sent', 'approved', 'in_review', 'queued', 'waiting_answer', 'in_progress', 'blocked', 'not_started'];
const MAX_SLOTS = 5;

const statusOf = t => t.display_status || t.status;
const firstLine = s => (typeof s === 'string' ? s.trim().split('\n')[0].trim() : '');
const unique = list => [...new Set(list)];

// --- Чистые функции ---

export function columnsOf(snapshot, filters = {}) {
  const tasks = snapshot ? [...snapshot.tasks].sort(byKeyOrder) : [];
  const attn = filters.attn ? new Set(waitingForYou(snapshot).map(i => i.key).filter(Boolean)) : null;
  const q = String(filters.q || '').trim().toLowerCase();
  const pass = t => (!filters.repo || t.repo === filters.repo)
    && (!filters.type || t.kind === filters.type)
    && (!attn || attn.has(t.key))
    && (!q || (t.key + ' ' + t.title).toLowerCase().includes(q));
  return STATUSES.map(s => {
    const all = tasks.filter(t => statusOf(t) === s.id);
    return { id: s.id, label: s.label, color: s.color, total: all.length, tasks: all.filter(pass) };
  });
}

function proposalKey(p) {
  const payload = p.payload || {};
  if (p.kind === 'add_task') return payload.task && typeof payload.task.key === 'string' ? payload.task.key : null;
  return typeof payload.key === 'string' ? payload.key : null;
}

// Всё, что требует действия пользователя; open — что открыть по клику.
export function waitingForYou(snapshot) {
  if (!snapshot) return [];
  const tasks = snapshot.tasks.filter(t => t.status !== 'cancelled').sort(byKeyOrder);
  const card = key => ({ type: 'card', key });
  const project = { type: 'project' };
  const items = [];
  for (const t of tasks) {
    if (t.open_questions > 0) items.push({ kind: 'question', key: t.key, count: t.open_questions, open: card(t.key) });
  }
  for (const p of snapshot.proposals || []) {
    if (p.status !== 'open') continue;
    const key = proposalKey(p);
    items.push({ kind: 'proposal', id: p.id, proposal: p.kind, ...(key ? { key } : {}), open: project });
  }
  for (const t of tasks) if (statusOf(t) === 'in_review') items.push({ kind: 'review', key: t.key, open: card(t.key) });
  for (const t of tasks) {
    if (t.agent_state === 'error') items.push({ kind: 'error', key: t.key, error: t.turn_error || '', open: card(t.key) });
  }
  const pa = snapshot.project_agent;
  if (pa && pa.state === 'error') items.push({ kind: 'error', error: pa.turn_error || '', open: project });
  for (const t of tasks) {
    const m = t.last_message;
    if (m && m.author === 'system' && String(m.text).startsWith(AGENTS_LOOP_PREFIX)) {
      items.push({ kind: 'loop', key: t.key, open: card(t.key) });
    }
  }
  if (snapshot.config && snapshot.config.stage === 'decomposition') {
    for (const t of tasks) {
      const n = (t.rule_violations || []).length;
      if (n) items.push({ kind: 'violation', key: t.key, count: n, open: card(t.key) });
    }
  }
  return items;
}

// Почему задача стоит: незакрытые зависимости с их статусами и внешний блокер.
export function whyBlocked(task, tasksByKey) {
  const deps = (task.open_deps || []).map(key => {
    const dep = tasksByKey.get(key);
    return { key, status: dep ? statusOf(dep) : null };
  });
  const ext = typeof task.external_blocker === 'string' ? task.external_blocker.trim() : '';
  if (!deps.length && !ext) return null;
  return { deps, external: ext || null };
}

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

function agentLine(task, status) {
  if (status === 'blocked') return null;
  const live = task.agent_state === 'running';
  const m = task.last_message;
  const text = firstLine(task.summary)
    || (m && m.author === 'agent' ? firstLine(m.text) : '')
    || (live ? T.agentRunning : task.agent_state === 'waiting_turn' ? T.agentWaitingTurn : '');
  return text ? { live, text } : null;
}

// Всё, что показывает карточка; по нему же решается, нужно ли её перерисовать.
export function cardModel(task, ctx) {
  const status = statusOf(task);
  const locks = task.locks || [];
  const waiting = locks.find(l => l.position > 0);
  const base = ctx.repoBase.get(task.repo) || DEFAULT_BASE;
  return {
    key: task.key,
    title: task.title,
    kind: task.kind,
    repo: task.repo || '',
    estimate: task.estimate ?? null,
    status,
    why: status === 'blocked' ? whyBlocked(task, ctx.byKey) : null,
    queue: waiting ? { resource: waiting.resource, position: waiting.position } : null,
    holds: locks.filter(l => l.position === 0).map(l => l.resource),
    agent: agentLine(task, status),
    error: task.agent_state === 'error' ? (task.turn_error || T.agentError) : null,
    questions: task.open_questions || 0,
    unread: task.unread || 0,
    chain: task.base_branch && task.base_branch !== base ? task.base_branch : null,
    proposal: ctx.proposalKeys.has(task.key),
  };
}

// --- Общие элементы: тосты, меню статуса, перенос ---

const color = id => 'var(' + statusInfo(id).color + ')';
const sdot = id => h('span', { class: 'sdot', style: { '--c': color(id) } });

export function toast(text, options = {}) {
  let host = document.querySelector('.toasts');
  if (!host) {
    host = h('div', { class: 'toasts', role: 'status', 'aria-live': 'polite' });
    document.body.append(host);
  }
  const el = h('div', { class: options.error ? 'toast err' : 'toast' }, h('span', null, text));
  host.append(el);
  setTimeout(() => el.remove(), options.error ? 6000 : 4000);
  return el;
}

// Меню выбора статуса; onPick получает id выбранного статуса.
export function statusMenu(current, onPick) {
  return h('div', { class: 'menu', role: 'menu', 'aria-label': T.statusMenuAria },
    STATUSES.map(s => h('button', {
      type: 'button', role: 'menuitem', class: s.id === current ? 'cur' : null,
      onclick: () => onPick(s.id),
    }, sdot(s.id), s.label)));
}

// Перенос задачи пользователем: тост с реакцией сервера или с его ошибкой (карточка тогда остаётся на месте).
export async function moveTask(client, key, to) {
  try {
    const res = await client.post('/tasks/' + encodeURIComponent(key) + '/transition', { to, actor: 'user' });
    const events = (res && res.events) || [];
    toast(reactionText(events));
    return events;
  } catch (err) {
    toast(T.moveFailed(key) + ((err && err.message) || String(err)), { error: true });
    return null;
  }
}

export function paintStepper(stepper, stage) {
  const cur = STAGES.findIndex(s => s.id === stage);
  const items = [];
  STAGES.forEach((s, i) => {
    if (i) items.push(h('span', { class: 'sep' }));
    const done = i < cur || (stage === 'done' && i === cur);
    items.push(h('span', { class: done ? 'st done' : i === cur ? 'st cur' : 'st' },
      h('i', null, done ? '✓' : String(i + 1)), s.label));
  });
  stepper.replaceChildren(...items);
}

// --- Настройки вида, переживающие перезагрузку ---

function loadPrefs(key) {
  const defaults = { repo: '', type: '', q: '', attn: false, collapsed: { cancelled: true } };
  try {
    const saved = JSON.parse(window.localStorage.getItem(key) || 'null');
    return saved && typeof saved === 'object' ? { ...defaults, ...saved } : defaults;
  } catch (e) {
    return defaults;
  }
}

function savePrefs(key, prefs) {
  try {
    window.localStorage.setItem(key, JSON.stringify(prefs));
  } catch (e) {
    // хранилище недоступно — настройки живут до перезагрузки
  }
}

// --- Вид ---

export function mountBoard(root, store, ctx) {
  const slug = ctx.slug;
  const prefsKey = 'ab-board:' + slug;
  const prefs = loadPrefs(prefsKey);
  const cards = new Map();
  const cleanups = [];
  let painted = false;

  const title = h('h1', null, h('span', { class: 'skel title' }));
  const sub = h('span', { class: 'sub' }, h('span', { class: 'skel w60' }));
  const stepper = h('div', { class: 'stepper', 'aria-label': T.stageAria });
  const syncText = h('span');
  const sync = h('div', { class: 'sync static busy', role: 'status' }, h('span', { class: 'sdot' }), syncText);
  const projectBtn = h('button', { class: 'btn', type: 'button', onclick: () => ctx.openProject() });
  const doneText = h('b');
  const restText = h('span', { class: 'muted' });
  const bar = h('div', { class: 'bar' });
  const pills = h('div', { class: 'pills' });
  const artifacts = h('nav', { class: 'artifacts', 'aria-label': T.planning });
  const attn = h('div', { class: 'attn', hidden: true });
  // ошибки чтения показываются над доской и исчезают со следующим удачным снимком
  const banner = h('div', { class: 'banner-slot', hidden: true });

  const repoSelect = h('select', { 'aria-label': T.repoAria, onchange: e => setFilter('repo', e.target.value) });
  const typeSeg = h('div', { class: 'seg', role: 'group', 'aria-label': T.typeAria },
    [['', T.typeAll, null], ['code', T.typeCode, 'code'], ['research', T.typeResearch, 'flask']].map(([id, label, ic]) =>
      h('button', { type: 'button', 'data-type': id, 'aria-pressed': String(prefs.type === id), onclick: () => setFilter('type', id) },
        ic ? icon(ic) : null, label)));
  const attnToggle = h('input', { type: 'checkbox', onchange: e => setFilter('attn', e.target.checked) });
  attnToggle.checked = Boolean(prefs.attn);
  const search = h('input', { type: 'text', placeholder: T.searchPlaceholder, 'aria-label': T.searchPlaceholder,
    oninput: e => setFilter('q', e.target.value) });
  search.value = prefs.q || '';

  const cols = h('div', { class: 'cols' });
  const columns = new Map();
  for (const s of STATUSES) columns.set(s.id, column(s));

  root.replaceChildren(h('div', { class: 'app' },
    h('header', { class: 'top' },
      h('div', { class: 'row1' },
        ctx.brand(), h('span', { class: 'divider' }),
        h('div', { class: 'proj' }, title, sub),
        stepper, h('span', { class: 'spacer' }), sync, projectBtn, ctx.themeButton()),
      h('div', { class: 'row2' },
        h('div', { class: 'progress' }, h('div', { class: 'ptxt' }, doneText, restText), bar, pills),
        artifacts),
      attn, banner),
    h('div', { class: 'toolbar' },
      h('div', { class: 'seg', role: 'group', 'aria-label': T.viewAria },
        h('button', { type: 'button', 'aria-pressed': 'true' }, icon('board'), T.board),
        h('button', { type: 'button', 'aria-pressed': 'false', onclick: () => ctx.openGraph() }, icon('graph'), T.graph)),
      repoSelect, typeSeg,
      h('label', { class: 'toggle' }, attnToggle, ' ' + T.onlyMine),
      h('div', { class: 'search' }, icon('search'), search),
      h('span', { class: 'grow' }),
      h('button', { class: 'btn primary', type: 'button', onclick: () => ctx.openNewTask() }, icon('plus'), T.newTask)),
    h('div', { class: 'work' }, cols)));

  function column(s) {
    const n = h('span', { class: 'n' }, '0');
    const cb = h('div', { class: 'cb' });
    const collapseBtn = h('button', { class: 'btn ghost sm icon collapse', type: 'button', 'aria-label': T.collapse,
      onclick: e => { e.stopPropagation(); setCollapsed(s.id, true); } }, icon('x'));
    const col = h('section', { class: 'col', 'data-st': s.id, 'aria-label': s.label },
      h('div', { class: 'ch' }, sdot(s.id), h('span', { class: 'lbl' }, s.label),
        T.colHint[s.id] ? h('span', { class: 'hint' }, T.colHint[s.id]) : null, n,
        s.id === 'cancelled' ? collapseBtn : null),
      cb);
    col.addEventListener('click', () => { if (col.classList.contains('collapsed')) setCollapsed(s.id, false); });
    col.addEventListener('dragover', e => { e.preventDefault(); col.classList.add('over'); });
    col.addEventListener('dragleave', e => { if (!col.contains(e.relatedTarget)) col.classList.remove('over'); });
    col.addEventListener('drop', e => {
      e.preventDefault();
      col.classList.remove('over');
      const key = e.dataTransfer.getData('text/plain');
      const entry = cards.get(key);
      if (entry && entry.model.status !== s.id) moveTask(ctx.client, key, s.id);
    });
    cols.append(col);
    return { col, n, cb, collapseBtn };
  }

  function setCollapsed(id, on) {
    prefs.collapsed = { ...prefs.collapsed, [id]: on };
    savePrefs(prefsKey, prefs);
    paintCollapsed();
  }

  function paintCollapsed() {
    for (const [id, c] of columns) {
      const on = Boolean(prefs.collapsed && prefs.collapsed[id]);
      c.col.classList.toggle('collapsed', on);
      c.collapseBtn.hidden = on;
    }
  }

  function setFilter(name, value) {
    prefs[name] = value;
    savePrefs(prefsKey, prefs);
    typeSeg.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.type === prefs.type)));
    const snap = store.get().snapshot;
    if (snap) paintColumns(snap, new Set());
  }

  function jumpTo(id) {
    if (prefs.collapsed && prefs.collapsed[id]) setCollapsed(id, false);
    const c = columns.get(id).col;
    c.scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' });
    c.classList.add('over');
    setTimeout(() => c.classList.remove('over'), 700);
  }

  // --- карточки ---

  function cardNode(key) {
    const node = h('article', { class: 'card', draggable: 'true', tabindex: '0' });
    node.addEventListener('click', () => ctx.openCard(key));
    node.addEventListener('keydown', e => { if (e.key === 'Enter') ctx.openCard(key); });
    node.addEventListener('dragstart', e => {
      e.dataTransfer.setData('text/plain', key);
      e.dataTransfer.effectAllowed = 'move';
      node.classList.add('dragging');
    });
    node.addEventListener('dragend', () => node.classList.remove('dragging'));
    node.addEventListener('contextmenu', e => {
      e.preventDefault();
      openMenu(node, key, e.clientX || e.clientY ? { x: e.clientX, y: e.clientY } : null);
    });
    node.addEventListener('animationend', () => node.classList.remove('flash'));
    return node;
  }

  function cardParts(m) {
    const parts = [
      h('div', { class: 'ct' },
        h('span', { class: 'key' }, m.key),
        h('span', { class: 'ty', title: m.kind === 'research' ? T.kindResearch : T.kindCode }, icon(m.kind === 'research' ? 'flask' : 'code')),
        m.repo ? h('span', { class: 'chip repo' }, m.repo) : null,
        m.estimate !== null ? h('span', { class: 'est' }, T.estimate(m.estimate)) : null),
      h('div', { class: 'ttl' }, m.title),
    ];
    if (m.why) {
      parts.push(h('div', { class: 'why' },
        m.why.deps.length ? h('div', { class: 'w' }, icon('clock'), T.waits, m.why.deps.map(d =>
          h('span', { class: 'dk', title: d.status ? statusInfo(d.status).label : null }, d.status ? sdot(d.status) : null, d.key))) : null,
        m.why.external ? h('div', { class: 'w m' }, icon('lock'), h('span', null, m.why.external)) : null));
    }
    if (m.queue) {
      const slots = Math.min(m.queue.position + 1, MAX_SLOTS);
      parts.push(h('div', { class: 'qline' }, icon('clock'), T.queuePos(m.queue.resource, m.queue.position),
        h('span', { class: 'slots' }, Array.from({ length: slots }, (_, i) =>
          h('i', { class: i === Math.min(m.queue.position, MAX_SLOTS - 1) ? 'me' : null })))));
    }
    for (const r of m.holds) parts.push(h('div', { class: 'qline' }, icon('lock'), T.holds(r)));
    if (m.agent) {
      parts.push(h('div', { class: 'ag' }, m.agent.live ? h('span', { class: 'live' }) : null, h('span', null, m.agent.text)));
    }
    const chips = [];
    if (m.error) chips.push(h('span', { class: 'chip err', title: m.error }, icon('warn'), T.agentError));
    if (m.questions) chips.push(h('span', { class: 'chip q', title: T.questionsTitle }, '? ' + m.questions));
    if (m.unread) chips.push(h('span', { class: 'chip unread', title: T.unreadTitle }, icon('chat'), String(m.unread)));
    if (m.chain) chips.push(h('span', { class: 'chip', title: T.chainTitle }, icon('branch'), T.chainFrom(m.chain)));
    if (m.proposal) chips.push(h('span', { class: 'chip pa', title: T.proposalTitle }, icon('spark'), T.proposalChip));
    if (chips.length) parts.push(h('div', { class: 'meta' }, chips));
    return parts;
  }

  // Узел карточки переиспользуется и перерисовывается, только если изменилось то, что она показывает.
  function ensureCard(task, modelCtx) {
    const model = cardModel(task, modelCtx);
    const sig = JSON.stringify(model);
    let entry = cards.get(task.key);
    if (!entry) {
      entry = { node: cardNode(task.key), sig: null, model };
      cards.set(task.key, entry);
    }
    if (entry.sig !== sig) {
      entry.sig = sig;
      entry.model = model;
      entry.node.className = 'card st-' + model.status;
      entry.node.replaceChildren(...cardParts(model));
    }
    return entry.node;
  }

  function flash(node) {
    node.classList.remove('flash');
    void node.offsetWidth;
    node.classList.add('flash');
  }

  function paintColumns(snap, changed) {
    const byKey = new Map(snap.tasks.map(t => [t.key, t]));
    const modelCtx = {
      byKey,
      proposalKeys: new Set((snap.proposals || []).filter(p => p.status === 'open').map(proposalKey).filter(Boolean)),
      repoBase: new Map((snap.config.repos || []).map(r => [r.name, r.base || DEFAULT_BASE])),
    };
    for (const c of columnsOf(snap, prefs)) {
      const ref = columns.get(c.id);
      ref.n.textContent = String(c.tasks.length);
      const node = t => ensureCard(t, modelCtx);
      patchList(ref.cb, c.tasks, 'key', node, (_, t) => node(t));
      if (!c.tasks.length) ref.cb.append(h('div', { class: 'empty' }, T.dropHere));
    }
    for (const key of [...cards.keys()]) if (!byKey.has(key)) cards.delete(key);
    for (const key of changed) {
      const entry = cards.get(key);
      if (entry && entry.node.isConnected) flash(entry.node);
    }
  }

  // --- меню статуса на карточке (правая кнопка или клавиша меню) ---

  let menu = null;
  function closeMenu() {
    if (menu) menu.remove();
    menu = null;
  }
  function openMenu(node, key, at) {
    closeMenu();
    const entry = cards.get(key);
    if (!entry) return;
    const rect = node.getBoundingClientRect();
    const pos = at || { x: rect.left + 12, y: rect.top + 28 };
    menu = h('div', { class: 'stmenu floating' }, statusMenu(entry.model.status, to => {
      closeMenu();
      node.focus();
      if (to !== entry.model.status) moveTask(ctx.client, key, to);
    }));
    menu.style.setProperty('left', Math.min(pos.x, window.innerWidth - 230) + 'px');
    menu.style.setProperty('top', Math.min(pos.y, window.innerHeight - 380) + 'px');
    document.body.append(menu);
    const first = menu.querySelector('button.cur') || menu.querySelector('button');
    if (first) first.focus();
  }
  const onDocClick = e => { if (menu && !menu.contains(e.target)) closeMenu(); };
  const onKey = e => { if (e.key === 'Escape') closeMenu(); };
  document.addEventListener('click', onDocClick);
  document.addEventListener('keydown', onKey);
  cols.addEventListener('scroll', closeMenu, true);
  cleanups.push(() => {
    document.removeEventListener('click', onDocClick);
    document.removeEventListener('keydown', onKey);
    closeMenu();
  });

  // --- шапка ---

  function paintHeader(snap) {
    const cfg = snap.config;
    document.title = cfg.title + ' — ' + T.appName;
    title.textContent = cfg.title;
    sub.textContent = T.projectSub((cfg.repos || []).length, cfg.max_agents, cfg.chains);
    paintStepper(stepper, cfg.stage);

    const live = snap.tasks.filter(t => statusOf(t) !== 'cancelled');
    const count = id => live.filter(t => statusOf(t) === id).length;
    doneText.textContent = T.doneOf(count('done'), live.length);
    restText.textContent = T.progressRest(live.filter(t => SHIPPED.has(statusOf(t))).length,
      live.filter(t => WORKING.has(statusOf(t))).length);
    bar.replaceChildren(...BAR_ORDER.filter(count).map(id =>
      h('span', { title: statusInfo(id).label + ': ' + count(id), style: { '--c': color(id), flex: String(count(id)) } })));
    pills.replaceChildren(...STATUSES.map(s => {
      const n = snap.tasks.filter(t => statusOf(t) === s.id).length;
      return h('button', { type: 'button', class: n ? 'pill' : 'pill zero', onclick: () => jumpTo(s.id) },
        sdot(s.id), s.label + ' ', h('b', null, String(n)));
    }));

    artifacts.replaceChildren(h('span', { class: 'lbl' }, T.planning),
      ...(cfg.links || []).map(link => {
        const external = /^https?:\/\//i.test(link.href);
        const href = external ? link.href : '/api/p/' + encodeURIComponent(slug) + '/file?path=' + encodeURIComponent(link.href);
        return h('a', { href, target: '_blank', rel: 'noopener noreferrer', title: link.href }, icon('doc'), link.title);
      }),
      h('a', { href: '#/p/' + encodeURIComponent(slug) + '/decomposition' }, icon('graph'), T.decompositionLink));

    const open = (snap.proposals || []).filter(p => p.status === 'open').length;
    projectBtn.replaceChildren(icon('spark'), T.projectPanel,
      ...(open ? [h('span', { class: 'chip pa' }, T.proposalsCount(open))] : []));

    const repos = unique([...(cfg.repos || []).map(r => r.name), ...snap.tasks.map(t => t.repo).filter(Boolean)]).sort();
    if (prefs.repo && !repos.includes(prefs.repo)) repos.push(prefs.repo);
    const wanted = JSON.stringify(repos);
    if (repoSelect.dataset.repos !== wanted) {
      repoSelect.dataset.repos = wanted;
      repoSelect.replaceChildren(h('option', { value: '' }, T.allRepos), ...repos.map(r => h('option', { value: r }, r)));
    }
    repoSelect.value = prefs.repo || '';
  }

  const ATTN_LOOK = {
    question: ['q', 'chat'], proposal: ['pa', 'spark'], review: ['rv', 'eye'],
    error: ['er', 'warn'], loop: ['er', 'chat'], violation: ['er', 'warn'],
  };

  function attnText(item) {
    if (item.kind === 'question') return T.attn.question(item.count);
    if (item.kind === 'proposal') return T.attn.proposal[item.proposal] || T.attn.proposal.add_task;
    if (item.kind === 'error') return item.key ? T.attn.error : T.attn.projectError;
    if (item.kind === 'violation') return T.attn.violation(item.count);
    return T.attn[item.kind];
  }

  function paintAttn(snap) {
    const items = waitingForYou(snap);
    attn.hidden = !items.length;
    attn.replaceChildren(h('span', { class: 'lbl' }, icon('warn'), T.waitingForYou + ' · ' + items.length),
      ...items.map(item => {
        const [cls, ic] = ATTN_LOOK[item.kind];
        const showKey = item.key && !(item.kind === 'proposal' && item.proposal === 'add_task');
        return h('button', {
          type: 'button', class: 'it ' + cls, title: item.error || null,
          onclick: () => (item.open.type === 'card' ? ctx.openCard(item.open.key) : ctx.openProject()),
        }, icon(ic), showKey ? h('span', { class: 'k' }, item.key) : null, attnText(item));
      }));
  }

  function paintConnection(state) {
    const failed = Boolean(state.error);
    syncText.textContent = failed ? T.syncError : T.connection[state.connection] || state.connection;
    sync.classList.toggle('err', failed);
    sync.classList.toggle('busy', !failed && state.connection !== 'live');
    banner.hidden = !failed;
    banner.replaceChildren(...(failed ? [ctx.errorNotice(state.error)] : []));
  }

  function paint(state, changes) {
    paintConnection(state);
    const snap = state.snapshot;
    if (!snap || (painted && !changes.snapshot)) return;
    paintHeader(snap);
    paintAttn(snap);
    paintColumns(snap, painted ? changes.tasks : new Set());
    painted = true;
  }

  paintCollapsed();
  const unsubscribe = store.subscribe(paint);
  paint(store.get(), { tasks: new Set(), snapshot: true });

  return function destroy() {
    unsubscribe();
    cleanups.forEach(fn => fn());
  };
}
