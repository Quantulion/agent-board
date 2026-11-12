// Главный экран — доска проектов: проект сам переезжает между колонками по тому, что в нём происходит.
import { api } from './api.js';
import { ATTN_LOOK, BAR_ORDER, attnText, columnCollapse, savePrefs, waitingForYou } from './board.js';
import { color, sdot, statusOf } from './chips.js';
import { h, icon, patchList } from './dom.js';
import { STAGES, T, statusInfo } from './i18n.js';
import { suggestKey } from './newtask.js';
import { openNewProjectDialog, takenProjectKeys } from './newproject.js';
import { boardHref, decompositionHref, withTask } from './router.js';
import { nextBackoff } from './store.js';
import { toast } from './ui.js';

export const HOME_COLUMNS = ['waiting', 'working', 'quiet', 'archive'];
const PREFS_KEY = 'ab-home';
const COALESCE_MS = 150;
const BUSY = new Set(['running', 'waiting_turn']);
const IN_WORK = new Set(['in_progress', 'waiting_answer', 'queued']);
const CHIP_KINDS = ['question', 'review', 'error', 'proposal'];
const SHOWN_REPOS = 2;

// --- Чистые функции ---

const kindOf = snapshot => (snapshot.config.kind === 'free' ? 'free' : 'plan');

function isArchived(snapshot) {
  const cfg = snapshot.config;
  return cfg.archived === true || (kindOf(snapshot) === 'plan' && cfg.stage === 'done');
}

// Агенты проекта: сколько ходов идёт и сколько ждёт слота (задачи и агент проекта).
function agentsOf(snapshot) {
  const states = [...snapshot.tasks.map(t => t.agent_state), snapshot.project_agent && snapshot.project_agent.state];
  return {
    running: states.filter(s => s === 'running').length,
    queued: states.filter(s => s === 'waiting_turn').length,
  };
}

// Колонка проекта: архив важнее всего, затем то, что ждёт пользователя, затем работа агентов.
export function projectColumn(snapshot) {
  if (!snapshot) return 'quiet';
  if (isArchived(snapshot)) return 'archive';
  if (waitingForYou(snapshot).length) return 'waiting';
  const busy = snapshot.tasks.some(t => BUSY.has(t.agent_state))
    || Boolean(snapshot.project_agent && BUSY.has(snapshot.project_agent.state));
  return busy ? 'working' : 'quiet';
}

// Ключ проекта — префикс ключей его задач; пока задач нет — slug.
function projectKey(snapshot, slug) {
  return suggestKey(snapshot.tasks.map(t => t.key), slug).replace(/-\d+$/, '');
}

// Всё, что показывает карточка проекта.
export function projectCardModel(snapshot, slug = snapshot.config.slug) {
  const cfg = snapshot.config;
  const kind = kindOf(snapshot);
  const live = snapshot.tasks.filter(t => statusOf(t) !== 'cancelled');
  const count = id => live.filter(t => statusOf(t) === id).length;
  const items = waitingForYou(snapshot);
  const chips = {};
  for (const k of CHIP_KINDS) {
    const of = items.filter(i => i.kind === k);
    chips[k] = k === 'question' ? of.reduce((n, i) => n + (i.count || 1), 0) : of.length;
  }
  const repos = (cfg.repos || []).map(r => r.name);
  const stage = STAGES.find(s => s.id === cfg.stage);
  return {
    slug,
    key: projectKey(snapshot, slug),
    title: cfg.title || slug,
    kind,
    stage: kind === 'plan' && stage ? stage.label : null,
    archived: cfg.archived === true,
    agents: agentsOf(snapshot),
    counts: {
      working: live.filter(t => IN_WORK.has(statusOf(t))).length,
      review: count('in_review'),
      done: count('done'),
    },
    total: live.length,
    progress: kind === 'plan'
      ? { done: count('done'), total: live.length, segments: BAR_ORDER.filter(count).map(id => ({ id, n: count(id) })) }
      : null,
    chips,
    repos: { shown: repos.slice(0, SHOWN_REPOS), more: Math.max(0, repos.length - SHOWN_REPOS) },
  };
}

// Модель экрана: проекты по колонкам, общая полоса «Ждут вас» и сводка агентов в шапке.
export function homeModel(states) {
  const columns = Object.fromEntries(HOME_COLUMNS.map(id => [id, []]));
  const waiting = [];
  let archiveAttention = 0;
  const header = { active: 0, running: 0, limit: 0, queued: 0 };
  for (const entry of states) {
    const snap = entry.snapshot;
    const col = projectColumn(snap);
    columns[col].push(entry);
    if (col !== 'archive') header.active++;
    if (!snap) continue;
    const agents = agentsOf(snap);
    header.running += agents.running;
    header.queued += agents.queued;
    const limit = snap.config.global_max_agents;
    if (Number.isInteger(limit) && limit > header.limit) header.limit = limit;
    const items = waitingForYou(snap);
    if (col === 'archive') archiveAttention += items.length;
    const project = projectKey(snap, entry.slug);
    for (const item of items) waiting.push({ ...item, slug: entry.slug, project });
  }
  return { columns, waiting, archiveAttention, header };
}

// Куда ведёт пункт «Ждут вас» на главном экране.
export function attnHref(item) {
  if (item.open.type === 'card') return withTask(boardHref(item.slug), item.open.key);
  if (item.open.type === 'decomposition') return decompositionHref(item.slug, item.open.key);
  return boardHref(item.slug);
}

// --- Настройки вида ---

function loadPrefs() {
  const defaults = { collapsed: { archive: true } };
  try {
    const saved = JSON.parse(globalThis.localStorage.getItem(PREFS_KEY) || 'null');
    return saved && typeof saved === 'object' ? { ...defaults, ...saved } : defaults;
  } catch (e) {
    return defaults;
  }
}

// --- Вид ---

// ctx: brand, themeButton, errorNotice, onStates(states) — общий язык проектов.
export function mountHome(root, ctx) {
  const prefs = loadPrefs();
  const collapse = columnCollapse(prefs, () => savePrefs(PREFS_KEY, prefs));
  const snapshots = new Map();
  let slugs = [];
  let alive = true;
  let loaded = false;
  const newProject = () => openNewProjectDialog({
    takenKeys: () => takenProjectKeys(slugs.map(slug => ({ slug, snapshot: snapshots.get(slug) || null }))),
  });

  const sub = h('span', { class: 'sub' }, h('span', { class: 'skel w60' }));
  const syncText = h('span');
  const sync = h('div', { class: 'sync static busy', role: 'status' }, h('span', { class: 'sdot' }), syncText);
  const attnLabel = h('span', { class: 'lbl' });
  const attnList = h('span', { class: 'attn-items' });
  const attn = h('div', { class: 'attn', hidden: true }, attnLabel, attnList);
  const banner = h('div', { class: 'banner-slot', hidden: true });
  const cols = h('div', { class: 'cols home-cols' });
  const columns = new Map(HOME_COLUMNS.map(id => [id, column(id)]));

  root.replaceChildren(h('div', { class: 'app home' },
    h('header', { class: 'top' },
      h('div', { class: 'row1' },
        ctx.brand(), h('span', { class: 'divider' }),
        h('div', { class: 'proj' }, h('div', { class: 'ptl' }, h('h1', null, T.projects)), sub),
        h('span', { class: 'spacer' }), sync,
        h('button', { class: 'btn primary', type: 'button', 'data-action': 'new-project', onclick: newProject },
          icon('plus'), T.home.newProject),
        ctx.themeButton()),
      attn, banner),
    cols));
  document.title = T.projects + ' — ' + T.appName;

  function column(id) {
    const n = h('span', { class: 'n' }, '0');
    const flag = h('span', { class: 'chip q flag', hidden: true });
    const cb = h('div', { class: 'cb' });
    const dotColor = { waiting: 'waiting_answer', working: 'in_progress', quiet: 'not_started', archive: 'cancelled' }[id];
    const head = h('div', { class: 'ch' }, sdot(dotColor), h('span', { class: 'lbl' }, T.home.cols[id]),
      T.home.colHint[id] ? h('span', { class: 'hint' }, T.home.colHint[id]) : null, flag, n);
    const col = h('section', { class: 'col', 'data-col': id, 'aria-label': T.home.cols[id] }, head, cb);
    head.append(collapse.attach(id, col));
    if (id === 'quiet') {
      col.append(h('button', { class: 'add', type: 'button', onclick: newProject }, icon('plus'), T.home.newProject));
    }
    cols.append(col);
    return { col, n, cb, flag };
  }

  // --- карточка проекта ---

  function cardNode(slug) {
    const node = h('article', { class: 'card pcard', tabindex: '0', 'data-slug': slug });
    node.addEventListener('click', () => { window.location.hash = boardHref(slug); });
    node.addEventListener('keydown', e => {
      if (e.target !== node || (e.key !== 'Enter' && e.key !== ' ')) return;
      e.preventDefault();
      window.location.hash = boardHref(slug);
    });
    return node;
  }

  function cardParts(m, node) {
    const more = h('button', {
      class: 'more', type: 'button', 'aria-label': T.home.menu, 'aria-haspopup': 'menu', title: T.home.menu,
      onclick: e => { e.stopPropagation(); openMenu(m, more); },
      onkeydown: e => e.stopPropagation(),
    }, icon('dots'));
    const parts = [
      h('div', { class: 'ct' },
        h('span', { class: 'key' }, m.key),
        h('span', { class: 'kind ' + m.kind }, T.home.kind[m.kind]),
        m.stage ? h('span', { class: 'stage' }, '· ' + m.stage) : null,
        more),
      h('div', { class: 'ttl' }, m.title),
    ];
    if (m.progress && m.progress.total) {
      parts.push(h('div', { class: 'bar' }, m.progress.segments.map(s =>
        h('span', { title: statusInfo(s.id).label + ': ' + s.n, style: { '--c': color(s.id), flex: String(s.n) } }))));
    }
    const { running, queued } = m.agents;
    parts.push(running || queued
      ? h('div', { class: 'ag' }, running ? h('span', { class: 'live' }) : null, h('span', null, T.home.agents(running, queued)))
      : h('div', { class: 'ag idle' }, h('span', null, T.home.agentsIdle)));
    const stat = [];
    if (m.progress && m.progress.total) stat.push(h('span', null, h('b', null, T.home.doneOf(m.progress.done, m.progress.total)), ' ' + T.home.done));
    if (m.counts.working) stat.push(h('span', null, h('b', null, String(m.counts.working)), ' ' + T.home.inWork));
    if (m.counts.review) stat.push(h('span', null, h('b', null, String(m.counts.review)), ' ' + T.home.inReview));
    if (!m.progress && m.counts.done) stat.push(h('span', null, h('b', null, String(m.counts.done)), ' ' + T.home.done));
    if (!m.total) stat.push(h('span', null, T.home.noTasks));
    parts.push(h('div', { class: 'stat' }, stat));
    const chips = [];
    if (m.chips.question) chips.push(h('span', { class: 'chip q' }, T.home.chip.question(m.chips.question)));
    if (m.chips.review) chips.push(h('span', { class: 'chip rvw rv' }, icon('eye'), T.home.chip.review(m.chips.review)));
    if (m.chips.error) chips.push(h('span', { class: 'chip err' }, icon('warn'), T.home.chip.error(m.chips.error)));
    if (m.chips.proposal) chips.push(h('span', { class: 'chip pa' }, icon('spark'), T.home.chip.proposal(m.chips.proposal)));
    if (chips.length) parts.push(h('div', { class: 'meta' }, chips));
    if (m.repos.shown.length) {
      parts.push(h('div', { class: 'meta' }, m.repos.shown.map(r => h('span', { class: 'chip repo' }, r)),
        m.repos.more ? h('span', { class: 'chip repo' }, '+' + m.repos.more) : null));
    }
    return parts;
  }

  function brokenParts(slug) {
    return [
      h('div', { class: 'ct' }, h('span', { class: 'key' }, slug.toUpperCase())),
      h('div', { class: 'ttl' }, slug),
      h('div', { class: 'ag idle' }, icon('warn'), h('span', null, T.home.unreadable)),
    ];
  }

  function paintCard(node, entry) {
    const m = entry.snapshot ? projectCardModel(entry.snapshot, entry.slug) : null;
    node._model = m;
    const sig = JSON.stringify(m);
    if (node.dataset.sig === sig) return;
    node.dataset.sig = sig;
    node.classList.toggle('quiet', !m || (!m.agents.running && !m.agents.queued));
    node.replaceChildren(...(m ? cardParts(m, node) : brokenParts(entry.slug)));
  }

  // --- меню карточки: в архив и обратно ---

  let menu = null;
  let menuOwner = null;
  function closeMenu(refocus) {
    if (!menu) return;
    const [node, owner] = [menu, menuOwner];
    menu = null;
    menuOwner = null;
    node.remove();
    if (refocus && owner && owner.isConnected) owner.focus();
  }
  function openMenu(m, owner) {
    closeMenu();
    const archive = !m.archived;
    menuOwner = owner;
    menu = h('div', { class: 'stmenu floating' }, h('div', { class: 'menu', role: 'menu' },
      h('button', { type: 'button', role: 'menuitem', 'data-action': archive ? 'archive' : 'unarchive',
        onclick: () => { closeMenu(true); setArchived(m, archive); } },
      icon(archive ? 'lock' : 'board'), archive ? T.home.archive : T.home.unarchive)));
    menu.addEventListener('focusout', e => { if (menu && !menu.contains(e.relatedTarget)) closeMenu(); });
    document.body.append(menu);
    const rect = owner.getBoundingClientRect();
    const box = menu.getBoundingClientRect();
    menu.style.setProperty('left', Math.max(8, Math.min(rect.right - box.width, window.innerWidth - box.width - 8)) + 'px');
    menu.style.setProperty('top', Math.max(8, Math.min(rect.bottom + 4, window.innerHeight - box.height - 8)) + 'px');
    menu.querySelector('button').focus();
  }
  const onDocClick = e => { if (menu && !menu.contains(e.target)) closeMenu(); };
  const onKey = e => { if (e.key === 'Escape' && menu) { e.stopPropagation(); closeMenu(true); } };
  document.addEventListener('click', onDocClick);
  document.addEventListener('keydown', onKey);

  function setArchived(m, archived) {
    api(m.slug).post('/config', { actor: 'user', fields: { archived } })
      .then(() => {
        toast(archived ? T.home.archivedToast(m.title) : T.home.unarchivedToast(m.title));
        readProjects([m.slug]);
      })
      .catch(err => toast((err && err.message) || String(err), { error: true }));
  }

  // --- «Ждут вас» по всем проектам ---

  function paintAttnItem(node, item) {
    const [cls, ic] = ATTN_LOOK[item.kind];
    const showKey = item.key && !(item.kind === 'proposal' && item.proposal === 'add_task');
    const sig = JSON.stringify(item);
    node.href = attnHref(item);
    if (node.dataset.sig === sig) return;
    node.dataset.sig = sig;
    node.className = 'it ' + cls;
    if (item.error || item.text) node.title = item.error || item.text;
    else node.removeAttribute('title');
    node.replaceChildren(...[h('span', { class: 'p' }, item.project), icon(ic),
      showKey ? h('span', { class: 'k' }, item.key) : null, attnText(item)].filter(Boolean));
  }

  function paintAttn(items) {
    attn.hidden = !items.length;
    attnLabel.replaceChildren(icon('warn'), T.waitingForYou + ' · ' + items.length);
    patchList(attnList, items, item => item.slug + ':' + item.kind + ':' + (item.key || item.id || ''), item => {
      const node = h('a', { class: 'it' });
      paintAttnItem(node, item);
      return node;
    }, paintAttnItem);
  }

  // --- отрисовка ---

  function paint() {
    if (!alive || !loaded) return;
    const states = slugs.map(slug => ({ slug, snapshot: snapshots.get(slug) || null }));
    const model = homeModel(states);
    const h0 = model.header;
    sub.textContent = T.home.summary(h0.active, h0.running, h0.limit, h0.queued);
    paintAttn(model.waiting);
    for (const id of HOME_COLUMNS) {
      const c = columns.get(id);
      const list = model.columns[id];
      c.n.textContent = String(list.length);
      patchList(c.cb, list, 'slug', entry => {
        const node = cardNode(entry.slug);
        paintCard(node, entry);
        return node;
      }, paintCard);
      if (!states.length && id === 'quiet') c.cb.append(h('div', { class: 'empty' }, T.home.empty));
    }
    const flag = columns.get('archive').flag;
    flag.hidden = !model.archiveAttention;
    flag.textContent = T.home.archiveAttention(model.archiveAttention);
    flag.title = flag.textContent;
    if (ctx.onStates) ctx.onStates(states);
  }

  function setConnection(state, error) {
    const failed = Boolean(error);
    syncText.textContent = failed ? (error.status === 401 ? T.unauthorized : T.syncError) : T.connection[state] || state;
    sync.classList.toggle('err', failed);
    sync.classList.toggle('busy', !failed && state !== 'live');
    banner.hidden = !failed;
    banner.replaceChildren(...(failed ? [ctx.errorNotice(error)] : []));
  }

  // --- данные: список проектов, снимки и общий поток событий ---

  let listError = null;

  async function readList() {
    const { projects } = await api(null).get('/projects');
    slugs = projects;
    for (const slug of [...snapshots.keys()]) if (!slugs.includes(slug)) snapshots.delete(slug);
    return projects;
  }

  async function readProjects(which) {
    const read = await Promise.all(which.map(slug => api(slug).get('/state').catch(() => null)));
    if (!alive) return;
    which.forEach((slug, i) => {
      // снимок, прочитанный с ошибкой, не затирает последний удачный
      if (read[i] || !snapshots.has(slug)) snapshots.set(slug, read[i]);
    });
    paint();
  }

  async function reloadAll() {
    try {
      const projects = await readList();
      listError = null;
      await readProjects(projects);
      loaded = true;
      paint();
      setConnection(es ? 'live' : 'connecting', null);
    } catch (err) {
      if (!alive) return;
      listError = err;
      setConnection('offline', err);
    }
  }

  let dirty = new Set();
  let listDirty = false;
  let timer = null;
  function schedule() {
    if (timer !== null) return;
    timer = setTimeout(async () => {
      timer = null;
      const which = [...dirty];
      const list = listDirty;
      dirty = new Set();
      listDirty = false;
      if (list) {
        try {
          await readList();
        } catch (err) {
          setConnection('offline', err);
          return;
        }
      }
      await readProjects(list ? slugs.filter(s => which.includes(s) || !snapshots.has(s)) : which);
    }, COALESCE_MS);
  }

  let es = null;
  let attempt = 0;
  let reconnectTimer = null;
  function connect() {
    reconnectTimer = null;
    if (!alive) return;
    const source = new EventSource('/api/stream');
    es = source;
    let opened = false;
    source.onopen = () => {
      opened = true;
      attempt = 0;
      setConnection('live', listError);
      // пока потока не было, события могли пропасть: перечитываем всё
      reloadAll();
    };
    source.onerror = () => {
      source.close();
      if (es !== source || !alive) return;
      es = null;
      setConnection('reconnecting', null);
      reconnectTimer = setTimeout(connect, nextBackoff(attempt++));
      if (!opened) reloadAll();
    };
    source.addEventListener('project', e => {
      let data = {};
      try {
        data = JSON.parse(e.data) || {};
      } catch (err) {
        data = {};
      }
      if (!data.slug) return;
      if (data.type === 'project_created' || !slugs.includes(data.slug)) listDirty = true;
      dirty.add(data.slug);
      schedule();
    });
  }

  collapse.paint();
  setConnection('connecting', null);
  connect();

  return function destroy() {
    alive = false;
    if (es) es.close();
    es = null;
    if (timer !== null) clearTimeout(timer);
    if (reconnectTimer !== null) clearTimeout(reconnectTimer);
    document.removeEventListener('click', onDocClick);
    document.removeEventListener('keydown', onKey);
    closeMenu();
  };
}
