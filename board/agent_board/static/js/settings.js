// Окно «Настройки проекта»: поля из схемы настроек, проверка на сервере перед сохранением и ошибки у полей.
import { h, icon } from './dom.js';
import { STAGES, T } from './i18n.js';
import { openModal, toast } from './ui.js';

// порядок простых полей в форме; поля, которых здесь нет, идут следом в порядке схемы
const ORDER = ['title', 'language', 'max_agents', 'global_max_agents', 'chains', 'project_agent_status',
  'project_agent_tasks', 'archived',
  'branch_template', 'project_brief', 'agent_rules', 'decomposition_rules'];
const LONG_TEXT = new Set(['project_brief', 'agent_rules', 'decomposition_rules']);
const KINDS = { boolean: 'bool', integer: 'int', object: 'json', array: 'json' };
// поля со своим редактором вместо JSON
const CUSTOM = { ship: 'ship' };
// поля блока «что происходит после сдачи задачи»; там же переключатель ship.on_approve
const AFTER = new Set(['auto_approve']);
const GROUP = { simple: 0, ship: 1, after: 2, json: 3 };
const DEFAULT_SHIP_TIMEOUT = 600;

// --- Чистые функции ---

// Поля формы по схеме настроек: простые, отправка в ревью, что происходит после сдачи задачи, составные (JSON).
export function settingsFields(schema) {
  const list = (schema && Array.isArray(schema.fields) ? schema.fields : []).map((f, i) => {
    const kind = CUSTOM[f.name] || KINDS[f.type] || (LONG_TEXT.has(f.name) ? 'textarea' : 'text');
    return {
      name: f.name,
      type: f.type,
      kind,
      group: AFTER.has(f.name) ? 'after' : kind === 'ship' || kind === 'json' ? kind : 'simple',
      description: f.description || '',
      example: f.shape && f.shape.example !== undefined ? f.shape.example : null,
      placeholders: f.shape && Array.isArray(f.shape.placeholders) ? f.shape.placeholders : [],
      default: f.default,
      index: i,
    };
  });
  const rank = f => (ORDER.includes(f.name) ? ORDER.indexOf(f.name) : ORDER.length);
  return list.sort((a, b) => GROUP[a.group] - GROUP[b.group] || rank(a) - rank(b) || a.index - b.index);
}

// Начальное значение поля в форме: составные — отформатированным JSON, числа — строкой.
export function formValue(field, value) {
  const v = value === undefined ? field.default : value;
  if (field.kind === 'json') return JSON.stringify(v === undefined ? null : v, null, 2);
  if (field.kind === 'ship') {
    const ship = v && typeof v === 'object' ? v : {};
    return { command: typeof ship.command === 'string' ? ship.command : '', timeout: String(ship.timeout ?? DEFAULT_SHIP_TIMEOUT),
      on_approve: ship.on_approve === true };
  }
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
  if (field.kind === 'ship') {
    const command = String((raw && raw.command) ?? '').trim();
    const timeout = String((raw && raw.timeout) ?? '').trim();
    if (!/^\d+$/.test(timeout) || Number(timeout) < 1) return { error: T.settings.shipTimeoutInt };
    // без команды отправлять нечего, автоотправка выключается вместе с ней
    return { value: { command: command || null, timeout: Number(timeout), on_approve: Boolean(command && raw.on_approve) } };
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
      const value = f.kind === 'ship' ? shipValue(f, before, parsed.value) : parsed.value;
      if (JSON.stringify(value) !== JSON.stringify(before)) changes[f.name] = value;
    }
  }
  return { changes, errors };
}

// Новое значение ship: меняются только ключи, которые поправили в форме, остальные остаются как были.
function shipValue(field, before, parsed) {
  const base = before && typeof before === 'object' ? before : {};
  const initial = parseField(field, formValue(field, base)).value || {};
  const value = { ...base };
  for (const key of Object.keys(parsed)) if (parsed[key] !== initial[key]) value[key] = parsed[key];
  return value;
}

// Автоотправку можно включить только при заданной команде отправки.
export function canShipOnApprove(command) {
  return String(command ?? '').trim() !== '';
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
  if (field.kind === 'ship') return shipControl(field, value);
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

// Редактор команды отправки: шаблон команды с плейсхолдерами, таймаут и переключатель автоотправки.
function shipControl(field, value) {
  const command = h('textarea', { id: 'st-ship', class: 'mono', spellcheck: 'false', rows: '3',
    placeholder: typeof field.example === 'string' ? field.example : '' });
  command.value = value.command;
  const timeout = h('input', { type: 'text', id: 'st-ship-timeout', inputmode: 'numeric', autocomplete: 'off', class: 'num' });
  timeout.value = value.timeout;
  command._timeout = timeout;
  const onApprove = h('input', { type: 'checkbox', id: 'st-ship-on_approve' });
  onApprove.checked = value.on_approve;
  const note = h('span', { class: 'hint', 'data-role': 'no-command' }, T.settings.after.noCommand);
  const sync = () => {
    const allowed = canShipOnApprove(command.value);
    onApprove.disabled = !allowed;
    if (!allowed) onApprove.checked = false;
    note.hidden = allowed;
  };
  command.addEventListener('input', sync);
  sync();
  command._onApprove = onApprove;
  command._onApproveNote = note;
  return command;
}

function readValue(field, input) {
  if (field.kind === 'ship') return { command: input.value, timeout: input._timeout.value, on_approve: input._onApprove.checked };
  return field.kind === 'bool' ? input.checked : input.value;
}

// focus — имя поля, к которому форма прокручивается и ставит курсор после загрузки.
export function openSettingsDialog(ctx, { focus = null } = {}) {
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
    const help = (f.group === 'after' && T.settings.after.help[f.name]) || T.settings.help(f.name) || f.description;
    const label = [(f.group === 'after' && T.settings.after.labels[f.name]) || T.settings.labels[f.name] || f.name, ' ', h('code', { class: 'fname' }, f.name)];
    const example = f.kind === 'json' && f.example !== null
      ? h('span', { class: 'hint mono' }, T.settings.example + ': ' + JSON.stringify(f.example)) : null;
    if (f.kind === 'ship') {
      input._timeout.addEventListener('input', () => { err.hidden = true; });
      const S = T.settings.shipEditor;
      return h('div', { class: 'field shipfield', 'data-field': f.name },
        h('label', { for: input.id }, S.command, ' ', h('code', { class: 'fname' }, 'ship.command')), input,
        h('span', { class: 'hint' }, S.hint),
        f.placeholders.length ? h('span', { class: 'hint' }, S.placeholders + ': ',
          f.placeholders.map((p, i) => [i ? ' ' : null, h('code', null, '{' + p + '}')])) : null,
        typeof f.example === 'string' ? h('span', { class: 'hint mono' }, T.settings.example + ': ' + f.example) : null,
        h('div', { class: 'shiptimeout' }, h('label', { for: input._timeout.id }, S.timeout), input._timeout),
        err);
    }
    if (f.kind === 'bool') {
      return h('div', { class: 'field', 'data-field': f.name },
        h('label', { class: 'toggle', for: input.id }, input, label),
        help ? h('span', { class: 'hint' }, help) : null, err);
    }
    return h('div', { class: 'field', 'data-field': f.name },
      h('label', { for: input.id }, label), input,
      help ? h('span', { class: 'hint' }, help) : null, example, err);
  }

  // Переключатель ship.on_approve: значение читается вместе с редактором отправки, ошибки — под ним же.
  function onApproveRow(ship) {
    const A = T.settings.after;
    const box = ship._onApprove;
    box.addEventListener('change', () => { errs.ship.hidden = true; });
    return h('div', { class: 'field', 'data-subfield': 'ship.on_approve' },
      h('label', { class: 'toggle', for: box.id }, box, A.onApprove, ' ', h('code', { class: 'fname' }, 'ship.on_approve')),
      h('span', { class: 'hint' }, A.onApproveHint), ship._onApproveNote);
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
    const simple = fields.filter(f => f.group === 'simple');
    const ship = fields.filter(f => f.group === 'ship');
    const after = fields.filter(f => f.group === 'after');
    const compound = fields.filter(f => f.group === 'json');
    const shipRows = ship.map(row);
    const afterRows = [...after.map(row), ...(inputs.ship ? [onApproveRow(inputs.ship)] : [])];
    content.replaceChildren(
      fixed,
      h('h3', { class: 'sgroup' }, T.settings.simple), ...simple.map(row),
      ...(ship.length ? [h('h3', { class: 'sgroup', 'data-group': 'ship' }, T.settings.labels.ship), ...shipRows] : []),
      ...(afterRows.length ? [h('h3', { class: 'sgroup', 'data-group': 'after' }, T.settings.after.title), ...afterRows] : []),
      h('h3', { class: 'sgroup' }, T.settings.compound), h('p', { class: 'hint' }, T.settings.jsonHint), ...compound.map(row));
    saveBtn.disabled = false;
    if (focus && inputs[focus]) {
      inputs[focus].focus();
      inputs[focus].scrollIntoView({ block: 'center' });
    } else if (simple.length) inputs[simple[0].name].focus();
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
