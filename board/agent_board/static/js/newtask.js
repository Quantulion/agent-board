// Модальное окно «Новая задача»: поля задачи, зависимости с поиском, проверка до отправки и ошибки сервера под полями.
import { h, icon } from './dom.js';
import { spill, statusOf } from './chips.js';
import { STATUSES, T } from './i18n.js';
import { openModal, toast } from './ui.js';

const KEY_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/;
const NUMBERED = /^(.*?)(\d+)$/;
const KEYS = new Intl.Collator('ru', { numeric: true });
// зависимость в этих статусах уже не держит новую задачу
const SATISFIED = new Set(['approved', 'sent', 'done']);

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
  const deps = new Set(form.deps || []);
  const both = (form.blocks || []).filter(k => deps.has(k)).sort(KEYS.compare);
  if (both.length) errors.blocks = T.nt.depsBlocksClash(both);
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

// Новые зависимости задач, которые новая задача newKey должна блокировать (неизвестные и уже ждущие пропускаются).
export function blocksUpdates(tasks, picked, newKey) {
  const byKey = new Map(tasks.map(t => [t.key, t]));
  return [...new Set(picked)].sort(KEYS.compare)
    .map(k => byKey.get(k))
    .filter(t => t && !(t.deps || []).includes(newKey))
    .map(t => ({ key: t.key, deps: [...(t.deps || []), newKey] }));
}

// Добавляет новую задачу в зависимости выбранных. Зависимости берутся из свежего снимка с сервера, а не из окна:
// пока оно было открыто, их могли поменять. Возвращает задачи, которые обновить не удалось, с текстом ошибки.
export async function applyBlocks(client, newKey, picked) {
  let tasks;
  try {
    tasks = ((await client.get('/state')) || {}).tasks || [];
  } catch (e) {
    const error = (e && e.message) || String(e);
    return [...new Set(picked)].sort(KEYS.compare).map(key => ({ key, error }));
  }
  const failed = [];
  for (const u of blocksUpdates(tasks, picked, newKey)) {
    try {
      await client.post('/tasks/' + encodeURIComponent(u.key) + '/deps', { deps: u.deps, actor: 'user' });
    } catch (e) {
      failed.push({ key: u.key, error: (e && e.message) || String(e) });
    }
  }
  return failed;
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

// Список задач с галочками и поиском: отмеченные остаются отмеченными, поиск только прячет строки.
function taskPicker(id, label, tasks, onChange) {
  const picked = new Set();
  const search = h('input', { type: 'text', class: 'pick-search', placeholder: T.nt.depsSearch, 'aria-label': label + ': ' + T.nt.depsSearch });
  const nothing = h('div', { class: 'muted pick-none', hidden: true }, T.nt.depsNone);
  const rows = tasks.map(t => {
    const box = h('input', { type: 'checkbox', value: t.key });
    box.addEventListener('change', () => {
      if (box.checked) picked.add(t.key);
      else picked.delete(t.key);
      onChange();
    });
    return h('label', { 'data-q': (t.key + ' ' + t.title).toLowerCase() }, box,
      h('span', { class: 'k' }, t.key), h('span', { class: 'pt', title: t.title }, t.title),
      h('span', { class: 'ps' }, spill(statusOf(t))));
  });
  const picker = h('div', { class: 'picker', id, role: 'group', 'aria-label': label }, rows, nothing);
  search.addEventListener('input', () => {
    const q = search.value.trim().toLowerCase();
    let shown = 0;
    for (const r of rows) {
      r.hidden = Boolean(q) && !r.dataset.q.includes(q);
      if (!r.hidden) shown++;
    }
    nothing.hidden = shown > 0;
  });
  return { picked, search, picker };
}

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

  const live = tasks.filter(t => t.status !== 'cancelled');
  const depsPick = taskPicker('nt-deps', T.nt.deps, live, () => preview());
  const blocksPick = taskPicker('nt-blocks', T.nt.blocks, live, () => {});
  const picked = depsPick.picked;

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
    h('div', { class: 'field' }, h('span', { class: 'lbl' }, T.nt.deps), depsPick.search, depsPick.picker, err('deps')),
    h('div', { class: 'field' }, h('span', { class: 'lbl' }, T.nt.blocks), blocksPick.search, blocksPick.picker,
      h('span', { class: 'hint' }, T.nt.blocksHint), err('blocks')),
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
      blocks: [...blocksPick.picked].sort(KEYS.compare),
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
        // задача уже создана: ошибка одной связи не откатывает её и не мешает остальным
        if (form.blocks.length) {
          applyBlocks(ctx.client, key, form.blocks).then(failed => {
            for (const f of failed) toast(T.nt.blockFailed(f.key, key, f.error), { error: true });
          });
        }
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
