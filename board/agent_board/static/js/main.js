import { api } from './api.js';
import { h, icon, patchList } from './dom.js';
import { STAGES, T, statusInfo, tasksCount } from './i18n.js';
import { boardHref, parseRoute } from './router.js';
import { createStore } from './store.js';

const THEME_KEY = 'ab-theme';
const root = document.getElementById('app');
let view = null;

dropTokenFromAddress();
applyStoredTheme();
window.addEventListener('hashchange', route);
route();

// Токен уже обменян сервером на куку — убираем его из адресной строки, сохраняя hash.
function dropTokenFromAddress() {
  const url = new URL(window.location.href);
  if (!url.searchParams.has('token')) return;
  url.searchParams.delete('token');
  window.history.replaceState(null, '', url.pathname + url.search + url.hash);
}

function storage(key, value) {
  try {
    if (value === undefined) return window.localStorage.getItem(key);
    window.localStorage.setItem(key, value);
  } catch (e) {
    return null;
  }
  return null;
}

function applyStoredTheme() {
  const t = storage(THEME_KEY);
  if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t;
}

// Переключатель темы: авто → светлая → тёмная.
function themeButton() {
  const btn = h('button', { class: 'btn icon ghost', type: 'button', 'aria-label': T.theme });
  const paint = () => {
    const t = document.documentElement.dataset.theme;
    btn.title = T.theme + ': ' + (t === 'light' ? T.themeLight : t === 'dark' ? T.themeDark : T.themeAuto);
    btn.replaceChildren(icon(t === 'light' ? 'sun' : t === 'dark' ? 'moon' : 'auto'));
  };
  paint();
  btn.addEventListener('click', () => {
    const cur = document.documentElement.dataset.theme;
    const next = !cur ? 'light' : cur === 'light' ? 'dark' : '';
    if (next) document.documentElement.dataset.theme = next;
    else delete document.documentElement.dataset.theme;
    storage(THEME_KEY, next || 'auto');
    paint();
  });
  return btn;
}

function brand() {
  return h('a', { class: 'brand', href: '#/' }, h('span', { class: 'logo' }, icon('logo')), T.appName);
}

function route() {
  const r = parseRoute(window.location.hash);
  // доска и разбивка одного проекта делят одно живое состояние
  if (view && view.slug && view.slug === r.slug) {
    view.setMode(r.name);
    return;
  }
  if (view) view.destroy();
  view = null;
  if (r.name === 'projects') view = projectsView();
  else if (r.name === 'board' || r.name === 'decomposition') view = projectView(r.slug, r.name);
  else root.replaceChildren(notice(T.notFound, T.notFoundHint, false));
}

function notice(title, text, isError) {
  return h('div', { class: isError ? 'notice err' : 'notice' },
    h('h2', null, title),
    text ? h('p', null, text) : null,
    h('p', null, h('a', { href: '#/' }, T.toProjects)));
}

function errorNotice(err) {
  if (err && err.status === 401) return notice(T.unauthorized, T.unauthorizedHint, true);
  if (err && err.status === 404) return notice(T.projectNotFound, err.message, true);
  return notice(T.loadError, err && err.message, true);
}

// --- Список проектов ---

function projectsView() {
  let alive = true;
  const list = h('div', { class: 'screens' });
  const page = h('div', { class: 'wrap' },
    h('div', { class: 'hd' }, brand(), themeButton()),
    h('div', null, h('h1', null, T.projects), h('p', { class: 'lead' }, T.projectsLead)),
    list);
  root.replaceChildren(page);
  list.append(h('span', { class: 'muted' }, T.loading));
  api(null).get('/projects').then(async ({ projects }) => {
    if (!alive) return;
    if (!projects.length) {
      list.replaceChildren(h('span', { class: 'muted' }, T.noProjects));
      return;
    }
    const states = await Promise.all(projects.map(slug => api(slug).get('/state').catch(() => null)));
    if (!alive) return;
    patchList(list, projects.map((slug, i) => ({ slug, state: states[i] })), 'slug', projectCard);
  }).catch(err => {
    if (alive) page.replaceChildren(errorNotice(err));
  });
  return { slug: null, setMode() {}, destroy() { alive = false; } };
}

function projectCard({ slug, state }) {
  const title = state ? state.config.title : slug;
  const stage = state ? STAGES.find(s => s.id === state.config.stage) : null;
  const sub = state ? [tasksCount(state.tasks.length), stage && stage.label].filter(Boolean).join(' · ') : '';
  return h('a', { class: 'scr', href: boardHref(slug) },
    h('span', { class: 'ic' }, icon('board')),
    h('b', null, title),
    sub ? h('span', null, sub) : null,
    h('code', null, slug));
}

// --- Проект: шапка и заготовка доски ---

function projectView(slug, mode) {
  const store = createStore(slug, api(slug));
  const title = h('h1', null, h('span', { class: 'skel title' }));
  const sub = h('span', { class: 'sub' }, h('span', { class: 'skel w60' }));
  const stepper = h('div', { class: 'stepper', 'aria-label': T.stageAria });
  const syncText = h('span');
  const sync = h('div', { class: 'sync static busy', role: 'status' }, h('span', { class: 'sdot' }), syncText);
  const note = h('p', { class: 'muted' });
  const list = h('div', { class: 'tasklist' },
    [1, 2, 3].map(() => h('div', { class: 'row skeleton' }, h('span', { class: 'skel w40' }))));
  const body = h('div', { class: 'placeholder' }, note, list);
  const app = h('div', { class: 'app' },
    h('header', { class: 'top' },
      h('div', { class: 'row1' },
        brand(), h('span', { class: 'divider' }),
        h('div', { class: 'proj' }, title, sub),
        stepper, h('span', { class: 'spacer' }), sync, themeButton())),
    body);
  root.replaceChildren(app);

  let currentMode = mode;
  const paintMode = () => { note.textContent = currentMode === 'decomposition' ? T.decompositionSoon : T.boardSoon; };
  paintMode();

  const paintConnection = connection => {
    syncText.textContent = T.connection[connection] || connection;
    sync.classList.toggle('busy', connection !== 'live');
  };

  const paint = (state, changes) => {
    paintConnection(state.connection);
    if (state.error && !state.snapshot) {
      body.replaceChildren(errorNotice(state.error));
      return;
    }
    if (!state.snapshot || !changes.snapshot) return;
    const snap = state.snapshot;
    document.title = snap.config.title + ' — ' + T.appName;
    title.textContent = snap.config.title;
    sub.textContent = slug + ' · ' + tasksCount(snap.tasks.length);
    paintStepper(stepper, snap.config.stage);
    if (!snap.tasks.length) {
      list.replaceChildren(h('span', { class: 'muted' }, T.noTasks));
      return;
    }
    patchList(list, snap.tasks, 'key', task => h('div', { class: 'row' }, taskRowParts(task)), (node, task) => {
      if (changes.tasks.has(task.key)) node.replaceChildren(...taskRowParts(task));
    });
  };
  paintConnection(store.get().connection);
  const unsubscribe = store.subscribe(paint);
  store.start();

  return {
    slug,
    setMode(next) { currentMode = next; paintMode(); },
    destroy() { unsubscribe(); store.stop(); },
  };
}

function taskRowParts(task) {
  const st = statusInfo(task.display_status || task.status);
  return [
    h('span', { class: 'key' }, task.key),
    h('span', { class: 'ttl' }, task.title),
    h('span', { class: 'spill', style: { '--c': 'var(' + st.color + ')' } }, h('span', { class: 'sdot' }), st.label),
  ];
}

function paintStepper(stepper, stage) {
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
