// Модальное окно «Новая задача»: поля задачи, зависимости с поиском, проверка до отправки и ошибки сервера под полями.
import { api } from './api.js';
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

// Ввод в поле репозитория — путь к папке на диске, а не имя репозитория проекта.
const isPath = text => text.startsWith('/') || text.startsWith('~');

// Ошибки полей формы до отправки; пустой объект — можно отправлять.
// В проекте без плана (free) ключ выдаёт доска, а репозиторий — имя из проекта или путь к папке.
export function validateNewTask(form, existingKeys, { free = false, repos = [] } = {}) {
  const errors = {};
  const key = String(form.key || '').trim();
  if (free) {
    const repo = String(form.repo || '').trim();
    if (repo && !isPath(repo) && !repos.includes(repo)) errors.repo = T.nt.badRepo;
  } else if (!key) errors.key = T.nt.needKey;
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

export function taskPayload(form, { free = false } = {}) {
  const task = {
    title: String(form.title).trim(),
    description: String(form.description || '').trim(),
    done_when: String(form.done_when || '').trim(),
    kind: form.kind === 'research' ? 'research' : 'code',
  };
  const repo = String(form.repo || '').trim();
  if (!free) task.key = String(form.key).trim();
  if (free && isPath(repo)) task.repo_path = repo;
  else task.repo = repo;
  if (form.priority) task.priority = form.priority;
  if (String(form.estimate ?? '').trim()) task.estimate = parseEstimate(form.estimate);
  task.deps = [...(form.deps || [])];
  return task;
}

// Подсказки поля репозитория: сначала репозитории проекта, подходящие под ввод, затем папки с сервера.
// Папка не git недоступна для выбора.
export function repoSuggestions(repos, folders, text) {
  const q = String(text || '').trim().toLowerCase();
  const own = repos
    .filter(r => !q || r.name.toLowerCase().includes(q) || String(r.path || '').toLowerCase().startsWith(q))
    .map(r => ({ type: 'repo', value: r.name, path: r.path || '' }));
  const dirs = folders.map(f => ({ type: 'folder', value: f.path, git: f.git, branch: f.branch || null, disabled: !f.git }));
  return [...own, ...dirs];
}

// Текст, который подставится в поле при выборе подсказки; null — подсказку выбрать нельзя.
export function pickRepo(option) {
  return option && !option.disabled ? option.value : null;
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
  if (/репозитор/i.test(m)) return 'repo';
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

const SUGGEST_DELAY_MS = 200;

// Поле репозитория с подсказками: репозитории проекта и папки на машине доски. Список живёт поверх окна,
// чтобы прокрутка окна его не обрезала.
function repoCombo(repos, fsClient) {
  const input = h('input', { type: 'text', id: 'nt-repo', class: 'mono', autocomplete: 'off', spellcheck: 'false',
    placeholder: T.nt.repoPh, role: 'combobox', 'aria-autocomplete': 'list', 'aria-expanded': 'false', 'aria-controls': 'nt-repo-list' });
  const list = h('div', { class: 'repo-dd', id: 'nt-repo-list', role: 'listbox', hidden: true });
  let options = [];
  let active = -1;
  let folders = [];
  let timer = null;
  let inflight = null;

  const hide = () => {
    list.hidden = true;
    input.setAttribute('aria-expanded', 'false');
    active = -1;
  };
  // список шириной во весь ряд полей: пути длинные
  const place = () => {
    const r = input.getBoundingClientRect();
    const row = (input.closest('.two') || input).getBoundingClientRect();
    list.style.setProperty('left', r.left + 'px');
    list.style.setProperty('top', r.bottom + 4 + 'px');
    list.style.setProperty('width', Math.max(row.right - r.left, 320) + 'px');
  };
  // введённая часть имени папки выделяется, как в макете
  const label = (value, text) => {
    const cut = text.lastIndexOf('/') + 1;
    if (!isPath(text) || !value.toLowerCase().startsWith(text.toLowerCase()) || cut === 0) return value;
    return [value.slice(0, cut), h('b', null, value.slice(cut, text.length)), value.slice(text.length)];
  };
  function render() {
    const text = input.value.trim();
    options = repoSuggestions(repos, folders, text);
    if (!options.length) return hide();
    active = Math.min(Math.max(active, 0), options.length - 1);
    if (options[active].disabled) active = options.findIndex(o => !o.disabled);
    const rows = [];
    options.forEach((o, i) => {
      if (i === 0 && o.type === 'repo') rows.push(h('div', { class: 'g' }, T.nt.repoGroupOwn));
      if (o.type === 'folder' && (i === 0 || options[i - 1].type !== 'folder')) rows.push(h('div', { class: 'g' }, T.nt.repoGroupFolders));
      const tag = o.type === 'repo' ? h('span', { class: 'tag' }, o.path)
        : h('span', { class: 'tag ' + (o.git ? 'ok' : 'no') }, o.git ? T.nt.repoGit(o.branch) : T.nt.repoNotGit);
      rows.push(h('div', { class: 'o' + (i === active ? ' hl' : ''), role: 'option', 'data-i': String(i),
        'aria-selected': String(i === active), 'aria-disabled': o.disabled ? 'true' : null,
        // фокус остаётся в поле ввода
        onmousedown: e => e.preventDefault(),
        onclick: () => choose(o) },
      h('span', { class: 'p', title: o.value }, h('bdi', null, o.type === 'repo' ? o.value : label(o.value, text))), tag));
    });
    list.replaceChildren(...rows);
    place();
    list.hidden = false;
    input.setAttribute('aria-expanded', 'true');
  }
  function choose(o) {
    const value = pickRepo(o);
    if (value === null) return;
    input.value = value;
    input.dispatchEvent(new Event('input'));
    clearTimeout(timer);
    folders = [];
    hide();
  }
  // папки запрашиваются с задержкой, ответ на устаревший ввод отбрасывается
  function fetchFolders() {
    clearTimeout(timer);
    if (inflight) inflight.abort();
    const text = input.value.trim();
    if (!isPath(text)) {
      folders = [];
      return;
    }
    timer = setTimeout(() => {
      const ctl = new AbortController();
      inflight = ctl;
      fsClient.get('/fs/complete?path=' + encodeURIComponent(text), { signal: ctl.signal })
        .then(res => {
          if (ctl.signal.aborted || input.value.trim() !== text) return;
          folders = (res && res.items) || [];
          if (document.activeElement === input) render();
        })
        .catch(() => {});
    }, SUGGEST_DELAY_MS);
  }
  input.addEventListener('input', e => {
    if (!e.isTrusted) return;
    active = 0;
    folders = folders.filter(f => f.path.toLowerCase().startsWith(input.value.trim().toLowerCase()));
    fetchFolders();
    render();
  });
  input.addEventListener('focus', () => render());
  input.addEventListener('blur', () => hide());
  const move = step => {
    for (let i = active + step; i >= 0 && i < options.length; i += step) {
      if (!options[i].disabled) {
        active = i;
        return render();
      }
    }
  };
  input.addEventListener('keydown', e => {
    if (e.key === 'ArrowDown' && list.hidden) {
      e.preventDefault();
      render();
    } else if (list.hidden) {
      return;
    } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      move(e.key === 'ArrowDown' ? 1 : -1);
    } else if (e.key === 'Enter' && options[active]) {
      e.preventDefault();
      choose(options[active]);
    }
  });
  // Escape при открытом списке закрывает только список, а не окно
  const onEscape = e => {
    if (e.key !== 'Escape' || list.hidden) return;
    e.stopPropagation();
    e.preventDefault();
    hide();
  };
  window.addEventListener('keydown', onEscape, true);
  window.addEventListener('resize', hide);
  const dispose = () => {
    clearTimeout(timer);
    if (inflight) inflight.abort();
    window.removeEventListener('keydown', onEscape, true);
    window.removeEventListener('resize', hide);
    list.remove();
  };
  return { input, list, hide, place, dispose };
}

export function openNewTaskDialog(ctx) {
  const snap = ctx.store.get().snapshot;
  if (!snap) return null;
  const tasks = [...snap.tasks].sort((a, b) => KEYS.compare(a.key, b.key));
  const keys = tasks.map(t => t.key);
  const free = snap.config.kind === 'free';
  const projectRepos = (snap.config.repos || []).filter(r => r && typeof r.name === 'string');
  const repos = projectRepos.map(r => r.name);
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
  const combo = free ? repoCombo(projectRepos, ctx.fsClient || api(null)) : null;
  const repoSelect = free ? combo.input : h('select', { id: 'nt-repo' },
    repos.length ? null : h('option', { value: '' }, T.nt.noRepo),
    repos.map(r => h('option', { value: r }, r)));
  let priority = 'normal';
  const prioSeg = h('div', { class: 'seg', role: 'group', 'aria-label': T.nt.priority },
    ['high', 'normal', 'low'].map(id =>
      h('button', { type: 'button', 'data-v': id, 'aria-pressed': String(id === priority), onclick: () => setPriority(id) }, T.nt.priorities[id])));
  function setPriority(id) {
    priority = id;
    prioSeg.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.v === id)));
  }
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
  const createBtn = h('button', { class: 'btn primary', type: 'button', 'data-action': 'create-task', onclick: submit },
    free ? T.nt.createAndStart : T.nt.create);
  const depsField = h('div', { class: 'field' }, h('span', { class: 'lbl' }, T.nt.deps), depsPick.search, depsPick.picker, err('deps'));
  const blocksField = h('div', { class: 'field' }, h('span', { class: 'lbl' }, T.nt.blocks), blocksPick.search, blocksPick.picker,
    h('span', { class: 'hint' }, T.nt.blocksHint), err('blocks'));
  const kindField = h('div', { class: 'field' }, h('span', { class: 'lbl' }, T.nt.kind), kindSeg, kindHint, err('kind'));
  // в проекте без плана ключ выдаёт доска, а редкие поля убраны под «Дополнительно»
  const body = free ? [
    field('nt-title', T.nt.title, titleInput),
    field('nt-description', T.nt.whatToDo, descInput),
    h('div', { class: 'two' }, field('nt-repo', T.nt.repo, repoSelect, h('span', { class: 'hint' }, T.nt.repoHint)), kindField),
    h('details', { class: 'np-adv nt-adv' },
      h('summary', null, T.nt.advanced),
      h('div', { class: 'np-adv-body' },
        field('nt-done_when', T.nt.doneWhen, doneInput),
        h('div', { class: 'two' },
          h('div', { class: 'field' }, h('span', { class: 'lbl' }, T.nt.priority), prioSeg),
          field('nt-estimate', T.nt.estimate, estInput)),
        depsField,
        blocksField)),
    prev,
    general,
  ] : [
    field('nt-title', T.nt.title, titleInput),
    field('nt-description', T.nt.description, descInput),
    field('nt-done_when', T.nt.doneWhen, doneInput),
    h('div', { class: 'two' }, kindField, field('nt-repo', T.nt.repo, repoSelect)),
    h('div', { class: 'two' },
      field('nt-key', T.nt.key, keyInput, h('span', { class: 'hint' }, T.nt.keyHint)),
      field('nt-estimate', T.nt.estimate, estInput)),
    depsField,
    blocksField,
    prev,
    general,
  ];
  const { close, modal } = openModal(T.newTask, body, [
    h('span', { class: 'muted foot-note' }, T.nt.footer),
    h('button', { class: 'btn', type: 'button', onclick: () => close() }, T.cancel),
    createBtn,
  ], () => { if (combo) combo.dispose(); });
  modal.classList.add('newtask');
  if (combo) {
    modal.parentElement.append(combo.list);
    // список следует за полем при прокрутке окна
    modal.addEventListener('scroll', () => { if (!combo.list.hidden) combo.place(); });
  }

  const inputs = free ? { title: titleInput, repo: repoSelect, estimate: estInput } : { key: keyInput, title: titleInput, estimate: estInput };
  const opts = { free, repos };
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
      blocks: [...blocksPick.picked].sort(KEYS.compare), priority: free ? priority : undefined,
    };
    const errors = validateNewTask(form, keys, opts);
    // ошибка в свёрнутом блоке не должна остаться незамеченной
    const adv = modal.querySelector('.nt-adv');
    if (adv && ['estimate', 'deps', 'blocks'].some(n => errors[n])) adv.open = true;
    showErrors(errors);
    if (Object.keys(errors).length) return;
    sending = true;
    createBtn.disabled = true;
    ctx.client.post('/tasks', { task: taskPayload(form, opts), actor: 'user' })
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
        const at = errorField(message);
        // у ключа в проекте без плана поля нет — такая ошибка показывается общей
        showErrors({ [at && errs[at] ? at : 'general']: message });
        if (adv && ['estimate', 'deps'].includes(at)) adv.open = true;
      })
      .finally(() => {
        sending = false;
        createBtn.disabled = false;
      });
  }
  titleInput.focus();
  return close;
}
