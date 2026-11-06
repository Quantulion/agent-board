// Трекер: индикатор синхронизации статусов в шапке, заведение задач на стадии «Трекер» и ссылки на задачи трекера.
import { h, icon } from './dom.js';
import { T } from './i18n.js';
import { httpUrl } from './review.js';
import { formatTime } from './thread.js';
import { toast } from './ui.js';

const NAMES = { jira: 'Jira', github: 'GitHub' };
const SYNC_STAGES = new Set(['work', 'done']);
// снимок с итогом синхронизации перечитывается не реже этого: проход без изменений не пишет событий
export const SYNC_REFRESH_MS = 30000;
const TICK_MS = 5000;

export function trackerInfo(config) {
  const tracker = config && config.tracker;
  if (!tracker || !NAMES[tracker.type]) return null;
  let host = '';
  if (tracker.type === 'github') host = typeof tracker.repo === 'string' ? 'github.com/' + tracker.repo : 'github.com';
  else {
    try {
      host = new URL(String(tracker.url)).host;
    } catch (e) {
      host = '';
    }
  }
  return { type: tracker.type, name: NAMES[tracker.type], host };
}

export function relTime(iso, now = Date.now()) {
  const at = Date.parse(iso);
  if (!Number.isFinite(at)) return '';
  const sec = Math.max(0, Math.round((now - at) / 1000));
  if (sec < 5) return T.tracker.justNow;
  if (sec < 60) return T.tracker.secAgo(sec);
  if (sec < 3600) return T.tracker.minAgo(Math.floor(sec / 60));
  return T.tracker.hourAgo(Math.floor(sec / 3600));
}

// Состояние индикатора синхронизации; null — индикатор не нужен (нет трекера или стадия до работы).
export function syncModel(snapshot, now = Date.now()) {
  const info = snapshot && trackerInfo(snapshot.config);
  if (!info || !SYNC_STAGES.has(snapshot.config.stage)) return null;
  const s = snapshot.tracker_sync;
  const base = { host: info.host, name: info.name };
  if (!s || typeof s !== 'object') return { state: 'never', text: T.tracker.never(info.name), ...base };
  const rel = relTime(s.at, now);
  if (s.paused) return { state: 'paused', text: T.tracker.paused(info.name), ...base };
  if (!s.ok) return { state: 'err', text: T.tracker.err(info.name, rel), ...base };
  if (Array.isArray(s.errors) && s.errors.length) return { state: 'warn', text: T.tracker.warn(info.name, rel), ...base };
  if (s.warning) return { state: 'warn', text: T.tracker.insecure(info.name, rel), warning: String(s.warning), ...base };
  return { state: 'ok', text: T.tracker.ok(info.name, rel), ...base };
}

export function trackerLink(task) {
  if (!task || typeof task.tracker_key !== 'string' || !task.tracker_key) return null;
  return { key: task.tracker_key, url: httpUrl(task.tracker_url) };
}

// Чип ключа трекера: ссылка открывается в новой вкладке и не открывает карточку.
export function trackerChip(link) {
  if (!link) return null;
  if (!link.url) return h('span', { class: 'chip trk', title: T.tracker.key }, link.key);
  return h('a', {
    class: 'chip trk', href: link.url, target: '_blank', rel: 'noopener noreferrer', title: T.card.openTracker,
    onclick: e => e.stopPropagation(), onkeydown: e => e.stopPropagation(),
  }, link.key, icon('ext'));
}

// --- индикатор синхронизации в шапке ---

export function mountTrackerSync(ctx, store) {
  const text = h('span');
  const btn = h('button', { class: 'tsync', type: 'button', 'aria-haspopup': 'dialog', 'aria-expanded': 'false' },
    h('span', { class: 'sdot' }), text);
  const pop = h('div', { class: 'pop', role: 'dialog', 'aria-label': T.tracker.retry, hidden: true });
  const el = h('div', { class: 'tsync-wrap', hidden: true }, btn, pop);
  let retrying = false;
  let popSig = null;

  const model = () => syncModel(store.get().snapshot);

  function paint() {
    const m = model();
    el.hidden = !m;
    if (!m) {
      setOpen(false);
      return;
    }
    text.textContent = retrying ? m.name + ' · ' + T.tracker.retrying : m.text;
    btn.title = m.host;
    btn.classList.toggle('err', m.state === 'err' || m.state === 'paused');
    btn.classList.toggle('warn', m.state === 'warn');
    btn.classList.toggle('busy', retrying);
    if (!pop.hidden) paintPop(m);
  }

  function paintPop(m) {
    const s = store.get().snapshot.tracker_sync || null;
    const sig = JSON.stringify([s, retrying, m.state]);
    if (sig === popSig) return;
    popSig = sig;
    const rows = [];
    if (s) {
      rows.push([T.tracker.last, formatTime(s.at)]);
      rows.push([T.tracker.result, s.paused ? T.tracker.resultPaused : s.ok ? T.tracker.resultOk : T.tracker.resultErr]);
      if (Array.isArray(s.changed) && s.changed.length) rows.push([T.tracker.changed, s.changed.join(', ')]);
      if (Array.isArray(s.unknown_statuses) && s.unknown_statuses.length) rows.push([T.tracker.unknown, s.unknown_statuses.join(', ')]);
    }
    const errors = s && Array.isArray(s.errors) ? s.errors.filter(e => e && typeof e === 'object') : [];
    const retry = h('button', { class: 'btn sm', type: 'button', 'data-retry': '1', onclick: retryNow, disabled: retrying },
      icon('sync'), retrying ? T.tracker.retrying : T.tracker.retry);
    pop.replaceChildren(...[
      h('h4', null, T.tracker.syncTitle(m.name) + (m.host ? ' · ' + m.host : '')),
      s && s.error ? h('div', { class: 'errbox' }, String(s.error)) : null,
      s && s.warning ? h('div', { class: 'warnbox', 'data-warning': '1' }, String(s.warning)) : null,
      rows.length ? h('dl', null, rows.map(([k, v]) => [h('dt', null, k), h('dd', null, v)])) : null,
      errors.length ? [h('h4', null, T.tracker.taskErrors),
        h('ul', null, errors.map(e => h('li', null, h('span', { class: 'mono' }, String(e.key || '')), h('span', null, String(e.error || '')))))] : null,
      h('div', null, retry),
    ].flat().filter(Boolean));
  }

  function setOpen(open) {
    pop.hidden = !open;
    btn.setAttribute('aria-expanded', String(open));
    popSig = null;
    if (open) paintPop(model());
  }

  async function retryNow() {
    if (retrying) return;
    retrying = true;
    paint();
    try {
      const res = await ctx.client.post('/tracker/sync', { actor: 'user' });
      if (res && res.ok === false) toast(res.error || T.tracker.resultErr, { error: true });
      else toast(T.tracker.synced);
    } catch (err) {
      toast((err && err.message) || String(err), { error: true });
    } finally {
      retrying = false;
      paint();
      if (store.refresh) store.refresh();
    }
  }

  btn.addEventListener('click', e => {
    e.stopPropagation();
    setOpen(pop.hidden);
  });
  const onDoc = e => { if (!pop.hidden && !el.contains(e.target)) setOpen(false); };
  const onKey = e => {
    if (e.key === 'Escape' && !pop.hidden) {
      e.stopPropagation();
      setOpen(false);
      btn.focus();
    }
  };
  document.addEventListener('click', onDoc);
  document.addEventListener('keydown', onKey);
  const unsubscribe = store.subscribe(paint);
  // относительное время тикает само; итог прохода без изменений приходит только с перечитанным снимком
  const tick = setInterval(paint, TICK_MS);
  const refresh = setInterval(() => { if (model() && store.refresh && document.visibilityState !== 'hidden') store.refresh(); }, SYNC_REFRESH_MS);
  paint();
  let opening = null;
  return {
    el,
    // открытие из другого места по клику: тот же клик, дойдя до документа, не должен сразу закрыть окно
    open: () => {
      clearTimeout(opening);
      opening = setTimeout(() => setOpen(true));
    },
    destroy() {
      clearTimeout(opening);
      unsubscribe();
      clearInterval(tick);
      clearInterval(refresh);
      document.removeEventListener('click', onDoc);
      document.removeEventListener('keydown', onKey);
    },
  };
}

// --- заведение задач на стадии «Трекер» ---

// Последний итог заведения и идущий запрос по проекту: переживают уход со страницы доски и возврат на неё.
const creations = new Map();

export function mountTrackerStage(ctx, store) {
  const lead = h('p', { class: 'lead' });
  const createBtn = h('button', { class: 'btn primary sm', type: 'button', 'data-tracker': 'create', onclick: create });
  const checkBtn = h('button', { class: 'btn sm', type: 'button', 'data-tracker': 'check', onclick: check }, T.tracker.check);
  const result = h('div', { class: 'tresult', hidden: true });
  const el = h('div', { class: 'tstage', hidden: true },
    h('div', { class: 'trow' }, icon('sync'), lead, h('span', { class: 'grow' }), checkBtn, createBtn), result);
  let busy = null;
  let alive = true;
  const saved = () => creations.get(ctx.slug) || {};

  function paint() {
    if (!alive) return;
    const snap = store.get().snapshot;
    const info = snap && trackerInfo(snap.config);
    el.hidden = !info || snap.config.stage !== 'tracker';
    if (el.hidden) return;
    const live = snap.tasks.filter(t => t.status !== 'cancelled');
    lead.textContent = T.tracker.stageLead(live.filter(t => t.tracker_key).length, live.length);
    createBtn.replaceChildren(icon('send'), busy === 'create' ? T.tracker.creating : T.tracker.create(info.host));
    checkBtn.textContent = busy === 'check' ? T.tracker.checking : T.tracker.check;
    createBtn.disabled = checkBtn.disabled = Boolean(busy);
    el.classList.toggle('busy', Boolean(busy));
  }

  function showResult(res) {
    const created = Array.isArray(res.created) ? res.created : [];
    const linked = Array.isArray(res.linked) ? res.linked : [];
    const errors = Array.isArray(res.errors) ? res.errors : [];
    const warnings = Array.isArray(res.warnings) ? res.warnings : [];
    const issue = c => {
      const url = httpUrl(c.url);
      return h('li', null, h('span', { class: 'mono' }, String(c.key)), ' → ',
        url ? h('a', { href: url, target: '_blank', rel: 'noopener noreferrer' }, String(c.tracker_key)) : String(c.tracker_key));
    };
    const block = (title, cls, items) => (items.length ? h('div', { class: 'tblock ' + cls }, h('b', null, title),
      h('ul', null, items.map(x => h('li', null, h('span', { class: 'mono' }, String(x.key || '')), ' ', String(x.error || x.warning || ''))))) : null);
    result.replaceChildren(...[
      created.length || linked.length
        ? h('div', { class: 'tblock ok' }, h('b', null, T.tracker.created(created.length) + ' · ' + T.tracker.linked(linked.length)),
          created.length ? h('ul', null, created.map(issue)) : null)
        : !errors.length ? h('div', { class: 'tblock ok' }, T.tracker.nothing) : null,
      block(T.tracker.errors, 'err', errors),
      block(T.tracker.warnings, 'warn', warnings),
      errors.length ? null : h('p', { class: 'muted' }, T.tracker.next),
    ].filter(Boolean));
    result.hidden = false;
  }

  // ждёт идущее заведение (своё или начатое до возврата на страницу) и показывает его итог
  async function follow(pending) {
    busy = 'create';
    paint();
    const res = await pending;
    if (!alive) return;
    busy = null;
    showResult(res);
    paint();
  }

  function create() {
    if (busy || saved().pending) return;
    // заведение идёт столько, сколько отвечает трекер: свой таймаут у браузера не ставим
    const pending = ctx.client.post('/tracker/create', { actor: 'user' })
      .catch(err => ({ errors: [{ key: '', error: (err && err.message) || String(err) }] }))
      .then(res => {
        creations.set(ctx.slug, { result: res, pending: null });
        return res;
      });
    creations.set(ctx.slug, { ...saved(), pending });
    follow(pending);
  }

  async function check() {
    if (busy) return;
    busy = 'check';
    paint();
    try {
      const res = await ctx.client.post('/tracker/check', { actor: 'user' });
      const info = trackerInfo(store.get().snapshot.config);
      if (res && res.ok) toast(T.tracker.checkOk(info && info.host));
      else toast(T.tracker.checkFailed + ((res && res.error) || ''), { error: true });
      if (res && res.warning) toast(String(res.warning), { error: true });
    } catch (err) {
      toast(T.tracker.checkFailed + ((err && err.message) || String(err)), { error: true });
    } finally {
      busy = null;
      paint();
    }
  }

  const unsubscribe = store.subscribe(paint);
  if (saved().result) showResult(saved().result);
  if (saved().pending) follow(saved().pending);
  else paint();
  return {
    el,
    destroy() {
      alive = false;
      unsubscribe();
    },
  };
}
