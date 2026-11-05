// Модальное окно «Новая задача»: поля задачи, зависимости с поиском, проверка до отправки и ошибки сервера под полями.
import { h, icon } from './dom.js';
import { STATUSES, T, statusInfo } from './i18n.js';
import { openModal, toast } from './ui.js';

const KEY_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/;
const NUMBERED = /^(.*?)(\d+)$/;
const KEYS = new Intl.Collator('ru', { numeric: true });
// зависимость в этих статусах уже не держит новую задачу
const SATISFIED = new Set(['approved', 'sent', 'done']);

const statusOf = t => t.display_status || t.status;
const parseEstimate = v => Number(String(v ?? '').trim().replace(',', '.'));

// --- Чистые функции ---

// Следующий ключ: самый частый префикс задач проекта и номер больше всех с этим префиксом.
export function suggestKey(keys, slug) {
  const groups = new Map();
  for (const key of keys) {
    const m = NUMBERED.exec(key);
    if (!m) continue;
    const g = groups.get(m[1]) || { count: 0, max: -1, width: 0 };
    g.count++;
    if (Number(m[2]) > g.max) {
      g.max = Number(m[2]);
      g.width = m[2].length;
    }
    groups.set(m[1], g);
  }
  let best = null;
  for (const [prefix, g] of groups) {
    if (!best || g.count > best[1].count || (g.count === best[1].count && prefix < best[0])) best = [prefix, g];
  }
  const taken = new Set(keys.map(k => k.toLowerCase()));
  const [prefix, start, width] = best ? [best[0], best[1].max + 1, best[1].width] : [String(slug || 'task').toUpperCase() + '-', 1, 0];
  // ключи сравниваются без учёта регистра, как на сервере
  let n = start;
  while (taken.has((prefix + String(n).padStart(width, '0')).toLowerCase())) n++;
  return prefix + String(n).padStart(width, '0');
}

// Ошибки полей формы до отправки; пустой объект — можно отправлять.
export function validateNewTask(form, existingKeys) {
  const errors = {};
  const key = String(form.key || '').trim();
  if (!key) errors.key = T.nt.needKey;
  else if (!KEY_RE.test(key) || key.includes('..')) errors.key = T.nt.badKey;
  else {
    const clash = existingKeys.find(k => k.toLowerCase() === key.toLowerCase());
    if (clash) errors.key = T.nt.keyTaken(clash);
  }
  if (!String(form.title || '').trim()) errors.title = T.nt.needTitle;
  const est = String(form.estimate ?? '').trim();
  if (est && !(parseEstimate(est) >= 0)) errors.estimate = T.nt.badEstimate;
  return errors;
}

export function taskPayload(form) {
  const task = {
    key: String(form.key).trim(),
    title: String(form.title).trim(),
    description: String(form.description || '').trim(),
    done_when: String(form.done_when || '').trim(),
    kind: form.kind === 'research' ? 'research' : 'code',
    repo: form.repo || '',
  };
  if (String(form.estimate ?? '').trim()) task.estimate = parseEstimate(form.estimate);
  task.deps = [...(form.deps || [])];
  return task;
}

// Под каким полем показать ошибку сервера; null — общая ошибка формы.
export function errorField(message) {
  const m = String(message || '');
  if (/Ключ|идентификатор|уже существует/i.test(m)) return 'key';
  if (/Зависим|цикл|deps/i.test(m)) return 'deps';
  if (/Оценка/i.test(m)) return 'estimate';
  if (/Вид задачи/i.test(m)) return 'kind';
  if (/Заголов|title/i.test(m)) return 'title';
  return null;
}

// --- Окно ---

const SNAPSHOT_WAIT_MS = 5000;

// true — задача появилась в снимке, false — не дождались.
function whenInSnapshot(store, key) {
  const has = state => Boolean(state.snapshot && state.snapshot.tasks.some(t => t.key === key));
  if (has(store.get())) return Promise.resolve(true);
  return new Promise(resolve => {
    const done = found => {
      clearTimeout(timer);
      unsubscribe();
      resolve(found);
    };
    const unsubscribe = store.subscribe(state => { if (has(state)) done(true); });
    const timer = setTimeout(() => done(false), SNAPSHOT_WAIT_MS);
  });
}

const color = id => 'var(' + statusInfo(id).color + ')';
const spill = id => h('span', { class: 'spill', style: { '--c': color(id) } },
  h('span', { class: 'sdot', style: { '--c': color(id) } }), statusInfo(id).label);

export function openNewTaskDialog(ctx) {
  const snap = ctx.store.get().snapshot;
  if (!snap) return null;
  const tasks = [...snap.tasks].sort((a, b) => KEYS.compare(a.key, b.key));
  const keys = tasks.map(t => t.key);
  const repos = (snap.config.repos || []).map(r => r.name);
  const errs = {};
  const err = name => {
    errs[name] = h('div', { class: 'ferr', role: 'alert', hidden: true });
    return errs[name];
  };
  const field = (id, label, input, extra) => h('div', { class: 'field' }, h('label', { for: id }, label), input, extra || null, err(id.replace('nt-', '')));

  const keyInput = h('input', { type: 'text', id: 'nt-key', class: 'mono', autocomplete: 'off', spellcheck: 'false' });
  keyInput.value = suggestKey(keys, ctx.slug);
  const titleInput = h('input', { type: 'text', id: 'nt-title', placeholder: T.nt.titlePh });
  const descInput = h('textarea', { id: 'nt-description', placeholder: T.nt.descriptionPh });
  const doneInput = h('input', { type: 'text', id: 'nt-done_when', placeholder: T.nt.doneWhenPh });
  const estInput = h('input', { type: 'text', id: 'nt-estimate', inputmode: 'decimal', placeholder: T.nt.estimatePh });
  const repoSelect = h('select', { id: 'nt-repo' },
    repos.length ? null : h('option', { value: '' }, T.nt.noRepo),
    repos.map(r => h('option', { value: r }, r)));
  let kind = 'code';
  const kindHint = h('span', { class: 'hint' }, T.nt.kindCodeHint);
  const kindSeg = h('div', { class: 'seg', role: 'group', 'aria-label': T.nt.kind },
    [['code', T.kindCode, 'code'], ['research', T.kindResearch, 'flask']].map(([id, label, ic]) =>
      h('button', { type: 'button', 'data-v': id, 'aria-pressed': String(id === kind), onclick: () => setKind(id) }, icon(ic), label)));
  function setKind(id) {
    kind = id;
    kindSeg.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.v === id)));
    kindHint.textContent = id === 'code' ? T.nt.kindCodeHint : T.nt.kindResearchHint;
  }

  // зависимости: отмеченные остаются отмеченными, поиск только прячет строки
  const picked = new Set();
  const search = h('input', { type: 'text', class: 'pick-search', placeholder: T.nt.depsSearch, 'aria-label': T.nt.depsSearch });
  const nothing = h('div', { class: 'muted pick-none', hidden: true }, T.nt.depsNone);
  const rows = tasks.filter(t => t.status !== 'cancelled').map(t => {
    const box = h('input', { type: 'checkbox', value: t.key });
    box.addEventListener('change', () => {
      if (box.checked) picked.add(t.key);
      else picked.delete(t.key);
      preview();
    });
    const row = h('label', { 'data-q': (t.key + ' ' + t.title).toLowerCase() }, box,
      h('span', { class: 'k' }, t.key), h('span', { class: 'pt', title: t.title }, t.title),
      h('span', { class: 'ps' }, spill(statusOf(t))));
    return row;
  });
  const picker = h('div', { class: 'picker', id: 'nt-deps', role: 'group', 'aria-label': T.nt.deps }, rows, nothing);
  search.addEventListener('input', () => {
    const q = search.value.trim().toLowerCase();
    let shown = 0;
    for (const r of rows) {
      r.hidden = Boolean(q) && !r.dataset.q.includes(q);
      if (!r.hidden) shown++;
    }
    nothing.hidden = shown > 0;
  });

  const prev = h('div', { class: 'preview' });
  function preview() {
    const open = [...picked].filter(k => {
      const d = tasks.find(t => t.key === k);
      return d && !SATISFIED.has(statusOf(d));
    }).sort(KEYS.compare);
    prev.replaceChildren(T.nt.willBlock, ' ',
      open.length ? spill('blocked') : spill(STATUSES[0].id),
      ' ', open.length ? T.nt.waitsFor(open) : T.nt.startsWhenFree);
  }
  preview();

  const general = h('div', { class: 'ferr', role: 'alert', hidden: true });
  const createBtn = h('button', { class: 'btn primary', type: 'button', onclick: submit }, T.nt.create);
  const body = [
    field('nt-title', T.nt.title, titleInput),
    field('nt-description', T.nt.description, descInput),
    field('nt-done_when', T.nt.doneWhen, doneInput),
    h('div', { class: 'two' },
      h('div', { class: 'field' }, h('span', { class: 'lbl' }, T.nt.kind), kindSeg, kindHint, err('kind')),
      field('nt-repo', T.nt.repo, repoSelect)),
    h('div', { class: 'two' },
      field('nt-key', T.nt.key, keyInput, h('span', { class: 'hint' }, T.nt.keyHint)),
      field('nt-estimate', T.nt.estimate, estInput)),
    h('div', { class: 'field' }, h('span', { class: 'lbl' }, T.nt.deps), search, picker, err('deps')),
    prev,
    general,
  ];
  const { close, modal } = openModal(T.newTask, body, [
    h('span', { class: 'muted foot-note' }, T.nt.footer),
    h('button', { class: 'btn', type: 'button', onclick: () => close() }, T.cancel),
    createBtn,
  ]);
  modal.classList.add('newtask');

  const inputs = { key: keyInput, title: titleInput, estimate: estInput };
  function showErrors(errors) {
    for (const [name, node] of Object.entries(errs)) {
      node.textContent = errors[name] || '';
      node.hidden = !errors[name];
      if (inputs[name]) inputs[name].setAttribute('aria-invalid', String(Boolean(errors[name])));
    }
    general.textContent = errors.general || '';
    general.hidden = !errors.general;
    const first = Object.keys(inputs).find(n => errors[n]);
    if (first) inputs[first].focus();
  }
  for (const [name, input] of Object.entries(inputs)) {
    input.addEventListener('input', () => {
      errs[name].hidden = true;
      input.removeAttribute('aria-invalid');
    });
  }

  let sending = false;
  function submit() {
    if (sending) return;
    const form = {
      key: keyInput.value, title: titleInput.value, description: descInput.value, done_when: doneInput.value,
      kind, repo: repoSelect.value, estimate: estInput.value, deps: [...picked].sort(KEYS.compare),
    };
    const errors = validateNewTask(form, keys);
    showErrors(errors);
    if (Object.keys(errors).length) return;
    sending = true;
    createBtn.disabled = true;
    ctx.client.post('/tasks', { task: taskPayload(form), actor: 'user' })
      .then(res => {
        const key = (res && res.key) || form.key.trim();
        close();
        toast(T.nt.created(key));
        // карточка открывается, когда задача появится в снимке: иначе она сочла бы задачу несуществующей;
        // если пользователь уже ушёл на другой адрес, его не уводим
        const from = window.location.hash;
        whenInSnapshot(ctx.store, key).then(found => {
          if (window.location.hash !== from) return;
          if (found) ctx.openCard(key);
          else toast(T.nt.notYetVisible(key));
        });
      })
      .catch(e => {
        const message = (e && e.message) || String(e);
        showErrors({ [errorField(message) || 'general']: message });
      })
      .finally(() => {
        sending = false;
        createBtn.disabled = false;
      });
  }
  titleInput.focus();
  return close;
}
