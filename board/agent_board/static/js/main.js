import { api } from './api.js';
import { mountBoard, paintStepper, toast } from './board.js';
import { closeCard, openCard } from './card.js';
import { h, icon, patchList } from './dom.js';
import { STAGES, T, tasksCount } from './i18n.js';
import { boardHref, parseRoute, withTask } from './router.js';
import { createStore } from './store.js';

const THEME_KEY = 'ab-theme';
const root = document.getElementById('app');
let view = null;

dropTokenFromAddress();
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
    view.setTask(r.task || null);
    return;
  }
  if (view) view.destroy();
  view = null;
  if (r.name === 'projects') view = projectsView();
  else if (r.name === 'board' || r.name === 'decomposition') {
    view = projectView(r.slug, r.name);
    view.setTask(r.task || null);
  }
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
  return { slug: null, setMode() {}, setTask() {}, destroy() { alive = false; } };
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

// --- Проект: доска и (пока) заготовка разбивки на одном живом состоянии ---

function projectView(slug, mode) {
  const client = api(slug);
  const store = createStore(slug, client);
  const ctx = {
    slug,
    client,
    store,
    brand,
    themeButton,
    errorNotice,
    // карточка открывается и закрывается через адрес: её можно открыть ссылкой и вернуться кнопкой «Назад»
    openCard: key => { window.location.hash = withTask(window.location.hash, key); },
    closeCard: ({ replace = false } = {}) => {
      if (!parseRoute(window.location.hash).task) return;
      const next = withTask(window.location.hash, null);
      if (replace) window.location.replace(next);
      else window.location.hash = next;
    },
    // заполняются задачами графа и панели «Проект»
    openGraph: () => toast(T.soon),
    openNewTask: () => toast(T.soon),
    openProject: () => toast(T.soon),
  };
  let destroyMode = null;
  let current = null;
  const mount = next => {
    if (next === current) return;
    current = next;
    if (destroyMode) destroyMode();
    destroyMode = next === 'board' ? mountBoard(root, store, ctx) : decompositionPlaceholder(slug, store);
  };
  mount(mode);
  store.start();
  return {
    slug,
    setMode: mount,
    setTask: key => (key ? openCard(ctx, key) : closeCard()),
    destroy() { closeCard(false); destroyMode(); store.stop(); },
  };
}

function decompositionPlaceholder(slug, store) {
  const title = h('h1', null, h('span', { class: 'skel title' }));
  const stepper = h('div', { class: 'stepper', 'aria-label': T.stageAria });
  const banner = h('div', { class: 'banner-slot', hidden: true });
  const app = h('div', { class: 'app' },
    h('header', { class: 'top' },
      h('div', { class: 'row1' },
        brand(), h('span', { class: 'divider' }),
        h('div', { class: 'proj' }, title, h('span', { class: 'sub' }, h('a', { href: boardHref(slug) }, T.board))),
        stepper, h('span', { class: 'spacer' }), themeButton())),
    h('div', { class: 'placeholder' }, banner, h('p', { class: 'muted' }, T.decompositionSoon)));
  root.replaceChildren(app);
  const paint = state => {
    banner.hidden = !state.error;
    banner.replaceChildren(...(state.error ? [errorNotice(state.error)] : []));
    if (!state.snapshot) return;
    title.textContent = state.snapshot.config.title;
    paintStepper(stepper, state.snapshot.config.stage);
  };
  paint(store.get());
  return store.subscribe(paint);
}
