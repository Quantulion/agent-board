// Окно «Настройки проекта»: поля из схемы настроек, проверка на сервере перед сохранением и ошибки у полей.
import { h, icon } from './dom.js';
import { STAGES, T } from './i18n.js';
import { openModal, toast } from './ui.js';

// порядок простых полей в форме; поля, которых здесь нет, идут следом в порядке схемы
const ORDER = ['title', 'language', 'max_agents', 'global_max_agents', 'chains', 'auto_approve', 'branch_template',
  'project_brief', 'agent_rules', 'decomposition_rules'];
const LONG_TEXT = new Set(['project_brief', 'agent_rules', 'decomposition_rules']);
const KINDS = { boolean: 'bool', integer: 'int', object: 'json', array: 'json' };

// --- Чистые функции ---

// Поля формы по схеме настроек: сначала простые, затем составные (JSON).
export function settingsFields(schema) {
  const list = (schema && Array.isArray(schema.fields) ? schema.fields : []).map((f, i) => ({
    name: f.name,
    type: f.type,
    kind: KINDS[f.type] || (LONG_TEXT.has(f.name) ? 'textarea' : 'text'),
    description: f.description || '',
    example: f.shape && f.shape.example !== undefined ? f.shape.example : null,
    default: f.default,
    index: i,
  }));
  const rank = f => (ORDER.includes(f.name) ? ORDER.indexOf(f.name) : ORDER.length);
  return list.sort((a, b) => (a.kind === 'json') - (b.kind === 'json') || rank(a) - rank(b) || a.index - b.index);
}

// Начальное значение поля в форме: составные — отформатированным JSON, числа — строкой.
export function formValue(field, value) {
  const v = value === undefined ? field.default : value;
  if (field.kind === 'json') return JSON.stringify(v === undefined ? null : v, null, 2);
  if (field.kind === 'bool') return Boolean(v);
  if (field.kind === 'int') return v === undefined || v === null ? '' : String(v);
  return typeof v === 'string' ? v : '';
}

// Значение поля из формы: {value} или {error}, если его нельзя отправить на сервер.
export function parseField(field, raw) {
  if (field.kind === 'bool') return { value: Boolean(raw) };
  if (field.kind === 'int') {
    const text = String(raw ?? '').trim();
    return /^\d+$/.test(text) && Number(text) >= 1 ? { value: Number(text) } : { error: T.settings.needInt };
  }
  if (field.kind !== 'json') return { value: String(raw ?? '') };
  let value;
  try {
    value = JSON.parse(String(raw ?? ''));
  } catch (e) {
    return { error: T.settings.badJson((e && e.message) || String(e)) };
  }
  if (field.type === 'array' && !Array.isArray(value)) return { error: T.settings.needArray };
  if (field.type === 'object' && (!value || typeof value !== 'object' || Array.isArray(value))) return { error: T.settings.needObject };
  return { value };
}

// Изменённые поля для сервера и ошибки разбора; неизменённые поля не отправляются.
export function collectChanges(fields, raws, config) {
  const changes = {};
  const errors = {};
  for (const f of fields) {
    if (!(f.name in raws)) continue;
    const parsed = parseField(f, raws[f.name]);
    if (parsed.error) errors[f.name] = parsed.error;
    else {
      const before = config[f.name] === undefined ? f.default : config[f.name];
      if (JSON.stringify(parsed.value) !== JSON.stringify(before)) changes[f.name] = parsed.value;
    }
  }
  return { changes, errors };
}

// подсказки по тексту ошибки сервера, когда в нём нет имени поля
const HINTS = [
  [/репозитори|repositor/i, 'repos'],
  [/ресурс|resource/i, 'resources'],
  [/ссылк|link/i, 'links'],
  [/харнесс|harness/i, 'harness'],
  [/ревью|review/i, 'review'],
  [/команд\S* отправки|плейсхолдер|heredoc|ANSI/i, 'ship'],
];

// Под каким полем показать ошибку сервера; null — общая ошибка формы.
export function errorField(message, names) {
  const text = String(message || '');
  const known = [...names].sort((a, b) => b.length - a.length);
  const named = known.find(n => new RegExp('(^|[^A-Za-z0-9_])' + n + '([^A-Za-z0-9_]|$)').test(text));
  if (named) return named;
  const hint = HINTS.find(([re, name]) => names.includes(name) && re.test(text));
  return hint ? hint[1] : null;
}

// --- Окно ---

function control(field, value) {
  const id = 'st-' + field.name;
  if (field.kind === 'bool') {
    const box = h('input', { type: 'checkbox', id });
    box.checked = value;
    return box;
  }
  if (field.kind === 'int') {
    const input = h('input', { type: 'text', id, inputmode: 'numeric', autocomplete: 'off', class: 'num' });
    input.value = value;
    return input;
  }
  if (field.kind === 'text') {
    const input = h('input', { type: 'text', id, autocomplete: 'off', class: field.name === 'branch_template' ? 'mono' : null });
    input.value = value;
    return input;
  }
  const json = field.kind === 'json';
  const area = h('textarea', { id, class: json ? 'mono json' : null, spellcheck: json ? 'false' : null });
  area.value = value;
  area.rows = Math.min(14, Math.max(json ? 3 : 4, value.split('\n').length + 1));
  return area;
}

function readValue(field, input) {
  return field.kind === 'bool' ? input.checked : input.value;
}

export function openSettingsDialog(ctx) {
  const content = h('div', { class: 'settings' }, h('span', { class: 'muted' }, T.loading));
  const general = h('div', { class: 'ferr', role: 'alert', hidden: true });
  const saveBtn = h('button', { class: 'btn primary', type: 'button', disabled: true, onclick: () => save() }, T.settings.save);
  const { close, modal } = openModal(T.settings.title, [content, general], [
    h('span', { class: 'muted foot-note' }, T.settings.footer),
    h('button', { class: 'btn', type: 'button', onclick: () => close() }, T.cancel),
    saveBtn,
  ]);
  modal.classList.add('settingsmodal');

  let fields = [];
  let config = {};
  const inputs = {};
  const errs = {};
  let sending = false;

  function showErrors(errors) {
    for (const f of fields) {
      const msg = errors[f.name] || '';
      errs[f.name].textContent = msg;
      errs[f.name].hidden = !msg;
      if (msg) inputs[f.name].setAttribute('aria-invalid', 'true');
      else inputs[f.name].removeAttribute('aria-invalid');
    }
    general.textContent = errors.general || '';
    general.hidden = !errors.general;
    const first = fields.find(f => errors[f.name]);
    if (first) {
      inputs[first.name].focus();
      errs[first.name].scrollIntoView({ block: 'nearest' });
    } else if (errors.general) general.scrollIntoView({ block: 'nearest' });
  }

  function row(f) {
    const input = control(f, formValue(f, config[f.name]));
    const err = h('div', { class: 'ferr', role: 'alert', hidden: true });
    inputs[f.name] = input;
    errs[f.name] = err;
    input.addEventListener(f.kind === 'bool' ? 'change' : 'input', () => {
      err.hidden = true;
      input.removeAttribute('aria-invalid');
    });
    const help = T.settings.help(f.name) || f.description;
    const label = [T.settings.labels[f.name] || f.name, ' ', h('code', { class: 'fname' }, f.name)];
    const example = f.kind === 'json' && f.example !== null
      ? h('span', { class: 'hint mono' }, T.settings.example + ': ' + JSON.stringify(f.example)) : null;
    if (f.kind === 'bool') {
      return h('div', { class: 'field', 'data-field': f.name },
        h('label', { class: 'toggle', for: input.id }, input, label),
        help ? h('span', { class: 'hint' }, help) : null, err);
    }
    return h('div', { class: 'field', 'data-field': f.name },
      h('label', { for: input.id }, label), input,
      help ? h('span', { class: 'hint' }, help) : null, example, err);
  }

  function render(schema) {
    fields = settingsFields(schema);
    const stage = STAGES.find(s => s.id === config.stage);
    const fixed = h('div', { class: 'two ro' },
      h('div', { class: 'field' }, h('span', { class: 'lbl' }, T.settings.slug),
        h('code', { class: 'roval', 'data-field': 'slug' }, config.slug || ctx.slug)),
      h('div', { class: 'field' }, h('span', { class: 'lbl' }, T.settings.stage),
        h('span', { class: 'roval', 'data-field': 'stage' }, stage ? stage.label : String(config.stage || '')),
        h('span', { class: 'hint' }, T.settings.stageHint)));
    const simple = fields.filter(f => f.kind !== 'json');
    const compound = fields.filter(f => f.kind === 'json');
    content.replaceChildren(
      fixed,
      h('h3', { class: 'sgroup' }, T.settings.simple), ...simple.map(row),
      h('h3', { class: 'sgroup' }, T.settings.compound), h('p', { class: 'hint' }, T.settings.jsonHint), ...compound.map(row));
    saveBtn.disabled = false;
    if (simple.length) inputs[simple[0].name].focus();
  }

  Promise.all([ctx.client.get('/config/schema'), ctx.client.get('/config?actor=user')])
    .then(([schema, res]) => {
      config = (res && res.config) || {};
      render(schema);
    })
    .catch(e => {
      content.replaceChildren();
      showErrors({ general: T.settings.loadFailed((e && e.message) || String(e)) });
    });

  async function save() {
    if (sending || !fields.length) return;
    const raws = Object.fromEntries(fields.map(f => [f.name, readValue(f, inputs[f.name])]));
    const { changes, errors } = collectChanges(fields, raws, config);
    showErrors(errors);
    if (Object.keys(errors).length) return;
    const names = Object.keys(changes);
    if (!names.length) {
      close();
      toast(T.settings.noChanges);
      return;
    }
    sending = true;
    saveBtn.disabled = true;
    saveBtn.textContent = T.settings.checking;
    try {
      // сначала только проверка: при ошибке форма остаётся как есть, ничего не записано
      await ctx.client.post('/config', { actor: 'user', fields: changes, dry_run: true });
      await ctx.client.post('/config', { actor: 'user', fields: changes });
      close();
      toast(T.settings.saved(names));
    } catch (e) {
      const message = (e && e.message) || String(e);
      showErrors({ [errorField(message, names) || 'general']: message });
    } finally {
      sending = false;
      saveBtn.disabled = false;
      saveBtn.textContent = T.settings.save;
    }
  }

  return close;
}

// Кнопка настроек для шапки проекта.
export function settingsButton(ctx) {
  return h('button', {
    class: 'btn icon ghost sm', type: 'button', 'data-action': 'settings', 'aria-label': T.settings.open, title: T.settings.open,
    onclick: () => openSettingsDialog(ctx),
  }, icon('gear'));
}
