import { api } from './api.js';
import { mountBoard } from './board.js';
import { closeCard, openCard } from './card.js';
import { mountDecomposition } from './decomposition.js';
import { h, icon } from './dom.js';
import { mountHome } from './home.js';
import { T, currentLanguage, setLanguage, uiLanguage } from './i18n.js';
import { decompositionHref, parseRoute, withTask } from './router.js';
import { createStore } from './store.js';

const THEME_KEY = 'ab-theme';
// последний язык интерфейса: до загрузки проекта показываем его, чтобы не мигать другим языком
const LANG_KEY = 'ab-lang';
const root = document.getElementById('app');
let view = null;

setLanguage(storage(LANG_KEY) || 'ru');
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

// Переключает язык интерфейса и пересобирает текущий экран, если язык сменился.
function applyLanguage(language, owner) {
  if (!setLanguage(language)) return;
  storage(LANG_KEY, currentLanguage());
  setTimeout(() => {
    if (view !== owner) return;
    view.destroy();
    view = null;
    route();
  }, 0);
}

function route() {
  const r = parseRoute(window.location.hash);
  // доска и разбивка одного проекта делят одно живое состояние
  if (view && view.slug && view.slug === r.slug) {
    view.setMode(r.name);
    view.setTask(r.task || null);
    view.setFocus(r.focus || null);
    return;
  }
  if (view) view.destroy();
  view = null;
  if (r.name === 'projects') view = projectsView();
  else if (r.name === 'board' || r.name === 'decomposition') {
    view = projectView(r.slug, r.name);
    view.setTask(r.task || null);
    view.setFocus(r.focus || null);
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

// --- Главный экран: доска проектов ---

function projectsView() {
  const self = { slug: null, setMode() {}, setTask() {}, setFocus() {}, destroy() { destroyHome(); } };
  const destroyHome = mountHome(root, {
    brand,
    themeButton,
    errorNotice,
    // общий язык всех проектов становится языком главного экрана
    onStates: states => {
      const langs = new Set(states.filter(s => s.snapshot).map(s => uiLanguage(s.snapshot.config.language)));
      if (langs.size === 1) applyLanguage([...langs][0], self);
    },
  });
  return self;
}

// --- Проект: доска и согласование разбивки на одном живом состоянии ---

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
    // задаёт смонтированная доска: панель «Проект», граф и окно новой задачи живут в ней
    openGraph: () => {},
    openNewTask: () => {},
    openProject: () => {},
    openShip: () => {},
    openSync: () => {},
    // задача, к которой прокрутить страницу разбивки; её забирает смонтированная разбивка
    pendingFocus: null,
    applyFocus: () => {},
    openDecomposition: key => { window.location.hash = decompositionHref(slug, key); },
  };
  let destroyMode = null;
  let current = null;
  const mount = next => {
    if (next === current) return;
    current = next;
    if (destroyMode) destroyMode();
    destroyMode = next === 'board' ? mountBoard(root, store, ctx) : mountDecomposition(root, store, ctx);
  };
  mount(mode);
  const self = {
    slug,
    setMode: mount,
    setTask: key => (key ? openCard(ctx, key) : closeCard()),
    setFocus: key => {
      ctx.pendingFocus = key;
      if (key) ctx.applyFocus();
    },
    destroy() { unsubscribe(); closeCard(false); destroyMode(); store.stop(); },
  };
  const unsubscribe = store.subscribe(st => {
    if (st.snapshot && st.snapshot.config) applyLanguage(st.snapshot.config.language, self);
  });
  store.start();
  return self;
}
