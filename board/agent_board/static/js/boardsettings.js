// Окно «Настройки доски»: язык и общий лимит агентов для всей доски, агент и ревью по умолчанию для проектов.
import { api } from './api.js';
import { h, icon } from './dom.js';
import { T } from './i18n.js';
import { errorField } from './settings.js';
import { openModal, toast } from './ui.js';

const LANGUAGES = ['ru', 'en'];
// название языка — на нём самом, в любом языке интерфейса
const LANGUAGE_NAMES = { ru: 'Русский', en: 'English' };
const HARNESS_TYPES = ['claude', 'codex'];
const REVIEW_TOOLS = ['crit', 'none'];
const FIELDS = ['language', 'global_max_agents', 'harness', 'review'];

// --- Чистые функции ---

// Варианты выбора: известные значения и текущее, даже если его нет среди известных.
export function choices(known, current) {
  const list = [...known];
  if (current && !list.includes(current)) list.push(current);
  return list;
}

const text = v => (typeof v === 'string' ? v : '');

// Значения формы по настройкам доски.
export function boardForm(settings) {
  const s = settings || {};
  const harness = s.harness && typeof s.harness === 'object' ? s.harness : {};
  const review = s.review && typeof s.review === 'object' ? s.review : {};
  return {
    language: text(s.language),
    global_max_agents: s.global_max_agents === undefined || s.global_max_agents === null ? '' : String(s.global_max_agents),
    harness_type: text(harness.type) || 'claude',
    model: text(harness.model),
    permission_mode: text(harness.permission_mode),
    sandbox: text(harness.sandbox),
    review_tool: text(review.tool) || 'crit',
    review_binary: text(review.binary),
  };
}

// Агент из формы: тип, модель и режим своего CLI; прочие ключи остаются как были.
function harnessValue(form, before) {
  const value = { ...(before && typeof before === 'object' ? before : {}) };
  value.type = form.harness_type;
  const set = (key, raw) => {
    const v = String(raw ?? '').trim();
    if (v) value[key] = v;
    else delete value[key];
  };
  set('model', form.model);
  // режим разрешений — у Claude Code, песочница — у Codex
  set('permission_mode', form.harness_type === 'codex' ? '' : form.permission_mode);
  set('sandbox', form.harness_type === 'codex' ? form.sandbox : '');
  return value;
}

function reviewValue(form, before) {
  const value = { ...(before && typeof before === 'object' ? before : {}) };
  value.tool = form.review_tool;
  const binary = String(form.review_binary ?? '').trim();
  if (binary) value.binary = binary;
  else delete value.binary;
  return value;
}

// Изменённые настройки доски для сервера и ошибки полей; неизменённые не отправляются.
export function boardChanges(form, settings) {
  const s = settings || {};
  const errors = {};
  const next = {};
  const language = String(form.language ?? '').trim();
  if (!language) errors.language = T.bs.needLanguage;
  else next.language = language;
  const limit = String(form.global_max_agents ?? '').trim();
  if (!/^\d+$/.test(limit) || Number(limit) < 1) errors.global_max_agents = T.settings.needInt;
  else next.global_max_agents = Number(limit);
  next.harness = harnessValue(form, s.harness);
  next.review = reviewValue(form, s.review);
  const changes = {};
  for (const name of FIELDS) {
    if (name in next && JSON.stringify(next[name]) !== JSON.stringify(s[name])) changes[name] = next[name];
  }
  return { changes, errors };
}

// Тип агента проекта по умолчанию в окне «Новый проект» — тот, что задан у доски.
export function defaultHarnessType(settings) {
  const type = settings && settings.harness && settings.harness.type;
  return HARNESS_TYPES.includes(type) ? type : 'claude';
}

// Известные значения поля агента из описания настроек доски.
function known(schema, field, key) {
  const f = schema && Array.isArray(schema.fields) ? schema.fields.find(x => x.name === field) : null;
  const shape = f && f.shape && f.shape[key];
  if (!shape) return [];
  return Array.isArray(shape.enum) ? shape.enum : Array.isArray(shape.known) ? shape.known : [];
}

// --- Окно ---

function select(id, values, current, label = v => v) {
  const node = h('select', { id }, choices(values, current).map(v => h('option', { value: v }, label(v))));
  node.value = current;
  return node;
}

// ctx: client — клиент общего API (для тестов); onSaved(settings) — после сохранения.
export function openBoardSettingsDialog(ctx = {}) {
  const client = ctx.client || api(null);
  const B = T.bs;
  const content = h('div', { class: 'settings boardsettings' }, h('span', { class: 'muted' }, T.loading));
  const general = h('div', { class: 'ferr', role: 'alert', hidden: true });
  const saveBtn = h('button', { class: 'btn primary', type: 'button', 'data-action': 'save-board-settings', disabled: true,
    onclick: () => save() }, T.settings.save);
  const { close, modal } = openModal(B.title, [content, general], [
    h('span', { class: 'muted foot-note' }, B.footer),
    h('button', { class: 'btn', type: 'button', onclick: () => close() }, T.cancel),
    saveBtn,
  ]);
  modal.classList.add('settingsmodal', 'boardsettingsmodal');

  let settings = null;
  let read = null;
  const errs = {};
  const focusOf = {};
  let sending = false;

  const err = name => {
    errs[name] = h('div', { class: 'ferr', role: 'alert', hidden: true });
    return errs[name];
  };
  const field = (id, label, input, hint, attrs = {}) => h('div', { class: 'field', ...attrs },
    h('label', { for: id }, label), input, hint ? h('span', { class: 'hint' }, hint) : null);

  function showErrors(errors) {
    for (const name of FIELDS) {
      if (!errs[name]) continue;
      errs[name].textContent = errors[name] || '';
      errs[name].hidden = !errors[name];
    }
    general.textContent = errors.general || '';
    general.hidden = !errors.general;
    const first = FIELDS.find(n => errors[n]);
    if (first && focusOf[first]) focusOf[first].focus();
  }

  function render(schema) {
    const form = boardForm(settings);
    const lang = select('bs-language', LANGUAGES, form.language, v => LANGUAGE_NAMES[v] || v);
    const limit = h('input', { type: 'text', id: 'bs-global_max_agents', inputmode: 'numeric', autocomplete: 'off', class: 'num' });
    limit.value = form.global_max_agents;

    let type = form.harness_type;
    const typeSeg = h('div', { class: 'seg', role: 'group', 'aria-label': B.harnessType, id: 'bs-harness-type' },
      choices(HARNESS_TYPES, type).map(id => h('button', {
        type: 'button', 'data-v': id, 'aria-pressed': String(id === type), onclick: () => setType(id),
      }, B.harnessLabels[id] || id)));
    const model = h('input', { type: 'text', id: 'bs-model', autocomplete: 'off', class: 'mono', placeholder: B.modelPh });
    model.value = form.model;
    const permission = select('bs-permission_mode', ['', ...known(schema, 'harness', 'permission_mode')], form.permission_mode,
      v => v || B.cliDefault);
    const sandbox = select('bs-sandbox', ['', ...known(schema, 'harness', 'sandbox')], form.sandbox, v => v || B.cliDefault);
    const permissionRow = field('bs-permission_mode', B.permissionMode, permission, B.permissionHint, { 'data-for': 'claude' });
    const sandboxRow = field('bs-sandbox', B.sandbox, sandbox, B.sandboxHint, { 'data-for': 'codex' });
    function setType(id) {
      type = id;
      typeSeg.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.v === id)));
      permissionRow.hidden = id === 'codex';
      sandboxRow.hidden = id !== 'codex';
      errs.harness.hidden = true;
    }

    const tools = known(schema, 'review', 'tool');
    const tool = select('bs-review-tool', tools.length ? tools : REVIEW_TOOLS, form.review_tool, v => B.reviewTools[v] || v);
    const binary = h('input', { type: 'text', id: 'bs-review-binary', autocomplete: 'off', class: 'mono', placeholder: 'crit' });
    binary.value = form.review_binary;
    const binaryRow = field('bs-review-binary', B.reviewBinary, binary, B.reviewBinaryHint);
    const syncTool = () => { binaryRow.hidden = tool.value === 'none'; };
    tool.addEventListener('change', syncTool);
    syncTool();

    focusOf.language = lang;
    focusOf.global_max_agents = limit;
    focusOf.harness = model;
    focusOf.review = tool;
    read = () => ({
      language: lang.value, global_max_agents: limit.value, harness_type: type, model: model.value,
      permission_mode: permission.value, sandbox: sandbox.value, review_tool: tool.value, review_binary: binary.value,
    });
    for (const [name, node] of Object.entries(focusOf)) {
      node.addEventListener(node.tagName === 'SELECT' ? 'change' : 'input', () => { if (errs[name]) errs[name].hidden = true; });
    }

    content.replaceChildren(
      h('h3', { class: 'sgroup' }, B.general),
      h('div', { class: 'two' },
        h('div', { 'data-field': 'language' }, field('bs-language', B.language, lang, B.languageHint), err('language')),
        h('div', { 'data-field': 'global_max_agents' },
          field('bs-global_max_agents', B.limit, limit, B.limitHint), err('global_max_agents'))),
      h('h3', { class: 'sgroup' }, B.harness),
      h('p', { class: 'hint' }, B.harnessHint),
      h('div', { class: 'field', 'data-field': 'harness' }, h('span', { class: 'lbl' }, B.harnessType), h('div', null, typeSeg)),
      h('div', { class: 'two' }, field('bs-model', B.model, model, B.modelHint), h('div', null, permissionRow, sandboxRow)),
      err('harness'),
      h('h3', { class: 'sgroup' }, B.review),
      h('p', { class: 'hint' }, B.reviewHint),
      h('div', { class: 'two', 'data-field': 'review' }, field('bs-review-tool', B.reviewTool, tool), binaryRow),
      err('review'));
    setType(type);
    saveBtn.disabled = false;
    lang.focus();
  }

  Promise.all([client.get('/settings'), client.get('/settings/schema')])
    .then(([res, schema]) => {
      settings = (res && res.settings) || {};
      render(schema);
    })
    .catch(e => {
      content.replaceChildren();
      showErrors({ general: T.settings.loadFailed((e && e.message) || String(e)) });
    });

  async function save() {
    if (sending || !read) return;
    const { changes, errors } = boardChanges(read(), settings);
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
    try {
      const res = await client.put('/settings', { actor: 'user', fields: changes });
      close();
      toast(B.saved(names));
      if (ctx.onSaved && res && res.settings) ctx.onSaved(res.settings);
    } catch (e) {
      const message = (e && e.message) || String(e);
      showErrors({ [errorField(message, names) || 'general']: message });
    } finally {
      sending = false;
      saveBtn.disabled = false;
    }
  }

  return close;
}

// Кнопка настроек доски для шапки главного экрана.
export function boardSettingsButton(ctx = {}) {
  return h('button', {
    class: 'btn icon ghost', type: 'button', 'data-action': 'board-settings', 'aria-label': T.bs.open, title: T.bs.open,
    onclick: () => openBoardSettingsDialog(ctx),
  }, icon('gear'));
}
