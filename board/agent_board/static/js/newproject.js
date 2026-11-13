// Окно «Новый проект»: название, ключ задач, тип, агенты и дополнительные настройки; создаёт проект и открывает его доску.
import { api } from './api.js';
import { defaultHarnessType } from './boardsettings.js';
import { h } from './dom.js';
import { T } from './i18n.js';
import { boardHref } from './router.js';
import { openModal, toast } from './ui.js';

export const PROJECT_KEY_RE = /^[A-Za-z][A-Za-z0-9]{0,9}$/;
const TASK_PREFIX = /^(.+)-\d+$/;
const DEFAULT_KEY = 'PRJ';
const DEFAULT_MAX_AGENTS = 3;

const CYRILLIC = {
  а: 'a', б: 'b', в: 'v', г: 'g', д: 'd', е: 'e', ё: 'e', ж: 'zh', з: 'z', и: 'i', й: 'y', к: 'k', л: 'l', м: 'm',
  н: 'n', о: 'o', п: 'p', р: 'r', с: 's', т: 't', у: 'u', ф: 'f', х: 'kh', ц: 'ts', ч: 'ch', ш: 'sh', щ: 'sch',
  ъ: '', ы: 'y', ь: '', э: 'e', ю: 'yu', я: 'ya', і: 'i', ї: 'yi', є: 'ye', ґ: 'g',
};

// --- Чистые функции ---

function latinWords(title) {
  const text = [...String(title || '').toLowerCase()].map(c => (c in CYRILLIC ? CYRILLIC[c] : c)).join('')
    .normalize('NFD').replace(/[̀-ͯ]/g, '');
  // слово не может начинаться с цифры: ключ должен начинаться с буквы
  return (text.match(/[a-z0-9]+/g) || []).map(w => w.replace(/^\d+/, '')).filter(Boolean);
}

// Ключ из названия: первые буквы слов (в латинице), у одного слова — его начало; 2–5 символов, пусто — PRJ.
export function keyFromTitle(title) {
  const words = latinWords(title);
  if (!words.length) return DEFAULT_KEY;
  let key = words.length > 1 ? words.map(w => w[0]).join('').slice(0, 5) : words[0].slice(0, 3);
  if (key.length < 2) key = (key + words[0].slice(1)).slice(0, 2);
  if (key.length < 2) return DEFAULT_KEY;
  return key.toUpperCase();
}

// Занятые ключи: имена проектов и префиксы ключей их задач.
export function takenProjectKeys(states) {
  const taken = new Set();
  for (const { slug, snapshot } of states || []) {
    if (slug) taken.add(slug.toUpperCase());
    for (const t of (snapshot && snapshot.tasks) || []) {
      const m = TASK_PREFIX.exec(t.key);
      if (m) taken.add(m[1].toUpperCase());
    }
  }
  return [...taken];
}

// Какой ключ получит проект: занятый ключ получает номер (FIX → FIX2, FIX3…), как на сервере. Регистр не важен.
export function suggestProjectKey(wanted, taken) {
  const base = String(wanted || '').trim().toUpperCase();
  const busy = new Set([...(taken || [])].map(k => String(k).toUpperCase()));
  let key = base;
  for (let n = 2; busy.has(key); n++) key = base + n;
  return { key, renamed: key !== base };
}

// Ошибки полей формы до отправки; пустой объект — можно отправлять.
export function validateNewProject(form) {
  const errors = {};
  if (!String(form.title || '').trim()) errors.title = T.np.needTitle;
  const key = String(form.key || '').trim();
  if (!key) errors.key = T.np.needKey;
  else if (!PROJECT_KEY_RE.test(key)) errors.key = T.np.badKey;
  const limit = String(form.max_agents ?? '').trim();
  if (!/^\d+$/.test(limit) || Number(limit) < 1) errors.max_agents = T.np.badLimit;
  return errors;
}

export function projectPayload(form) {
  return {
    actor: 'user',
    title: String(form.title).trim(),
    key: String(form.key).trim().toUpperCase(),
    kind: 'free',
    harness_type: form.harness === 'codex' ? 'codex' : 'claude',
    max_agents: Number(String(form.max_agents).trim()),
    auto_approve: !form.review,
    project_agent_tasks: Boolean(form.agentTasks),
  };
}

// Под каким полем показать ошибку сервера; null — общая ошибка формы.
export function projectErrorField(message) {
  const m = String(message || '');
  if (/Ключ|task key/i.test(m)) return 'key';
  if (/Название|title/i.test(m)) return 'title';
  if (/max_agents/i.test(m)) return 'max_agents';
  return null;
}

// --- Окно ---

// ctx: takenKeys() — занятые ключи для подсказки; settings — настройки доски, если уже прочитаны;
// client — клиент общего API (для тестов).
export function openNewProjectDialog(ctx = {}) {
  const client = ctx.client || api();
  const taken = () => (ctx.takenKeys ? ctx.takenKeys() : []);
  const errs = {};
  const err = name => {
    errs[name] = h('div', { class: 'ferr', role: 'alert', hidden: true });
    return errs[name];
  };
  const field = (id, label, input, extra) =>
    h('div', { class: 'field' }, h('label', { for: id }, label), input, extra || null, err(id.replace('np-', '')));

  const titleInput = h('input', { type: 'text', id: 'np-title', autocomplete: 'off', placeholder: T.np.titlePh });
  const keyInput = h('input', { type: 'text', id: 'np-key', class: 'mono', autocomplete: 'off', spellcheck: 'false', maxlength: '10' });
  keyInput.value = DEFAULT_KEY;
  const keyHint = h('span', { class: 'hint', id: 'np-key-hint' });
  // ключ подставляется из названия, пока пользователь не поправил его сам
  let keyEdited = false;

  function refreshKeyHint() {
    const key = keyInput.value.trim().toUpperCase();
    const fresh = PROJECT_KEY_RE.test(key) ? suggestProjectKey(key, taken()) : null;
    keyHint.classList.toggle('warn', Boolean(fresh && fresh.renamed));
    keyHint.textContent = fresh && fresh.renamed ? T.np.keyTaken(key, fresh.key) : T.np.keyHint(fresh ? fresh.key : key || DEFAULT_KEY);
  }
  titleInput.addEventListener('input', () => {
    if (!keyEdited) keyInput.value = keyFromTitle(titleInput.value);
    refreshKeyHint();
  });
  keyInput.addEventListener('input', () => {
    keyEdited = keyInput.value.trim() !== '';
    refreshKeyHint();
  });
  refreshKeyHint();

  const typeCard = (id, title, text, enabled) => h('button', {
    type: 'button', class: 'ptype' + (enabled ? ' on' : ''), 'data-kind': id, 'aria-pressed': String(enabled),
    disabled: enabled ? null : true,
  }, h('b', null, h('span', { class: 'radio' }), title), h('span', null, text));
  const types = h('div', { class: 'ptypes', role: 'group', 'aria-label': T.np.type },
    typeCard('free', T.home.kind.free, T.np.freeText, true),
    typeCard('plan', T.home.kind.plan, T.np.planText, false));

  // агент по умолчанию — как у доски: такой выбор не создаёт у проекта своего значения
  let harness = defaultHarnessType(ctx.settings);
  let harnessPicked = false;
  const harnessSeg = h('div', { class: 'seg', role: 'group', 'aria-label': T.np.agents },
    [['claude', 'Claude Code'], ['codex', 'Codex']].map(([id, label]) =>
      h('button', { type: 'button', 'data-v': id, 'aria-pressed': String(id === harness), onclick: () => {
        harnessPicked = true;
        setHarness(id);
      } }, label)));
  function setHarness(id) {
    harness = id;
    harnessSeg.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.v === id)));
  }

  if (!ctx.settings) {
    client.get('/settings')
      .then(res => { if (!harnessPicked && res && res.settings) setHarness(defaultHarnessType(res.settings)); })
      .catch(() => {});
  }

  const limitInput = h('input', { type: 'text', id: 'np-max_agents', inputmode: 'numeric', autocomplete: 'off', class: 'num' });
  limitInput.value = String(DEFAULT_MAX_AGENTS);
  const toggle = (id, label, hint, checked) => {
    const box = h('input', { type: 'checkbox', id });
    box.checked = checked;
    return [box, h('div', { class: 'field' }, h('label', { class: 'toggle', for: id }, box, label), h('span', { class: 'hint' }, hint))];
  };
  const [reviewBox, reviewRow] = toggle('np-review', T.np.review, T.np.reviewHint, true);
  const [tasksBox, tasksRow] = toggle('np-agent_tasks', T.np.agentTasks, T.np.agentTasksHint, true);
  const advanced = h('details', { class: 'np-adv' },
    h('summary', null, T.np.advanced),
    h('div', { class: 'np-adv-body' },
      h('div', { class: 'two' },
        field('np-max_agents', T.np.maxAgents, limitInput, h('span', { class: 'hint' }, T.np.maxAgentsHint))),
      reviewRow, tasksRow));

  const general = h('div', { class: 'ferr', role: 'alert', hidden: true });
  const createBtn = h('button', { class: 'btn primary', type: 'button', 'data-action': 'create-project', onclick: submit }, T.np.create);
  const body = [
    h('div', { class: 'two np-names' },
      field('np-title', T.np.title, titleInput),
      field('np-key', T.np.key, keyInput)),
    keyHint,
    h('div', { class: 'field' }, h('span', { class: 'lbl' }, T.np.type), types),
    h('div', { class: 'field' }, h('span', { class: 'lbl' }, T.np.agents), h('div', null, harnessSeg)),
    advanced,
    general,
  ];
  const { close, modal } = openModal(T.home.newProject, body, [
    h('span', { class: 'grow' }),
    h('button', { class: 'btn', type: 'button', onclick: () => close() }, T.cancel),
    createBtn,
  ]);
  modal.classList.add('newproject');

  const inputs = { title: titleInput, key: keyInput, max_agents: limitInput };
  function showErrors(errors) {
    for (const [name, node] of Object.entries(errs)) {
      node.textContent = errors[name] || '';
      node.hidden = !errors[name];
      if (inputs[name]) inputs[name].setAttribute('aria-invalid', String(Boolean(errors[name])));
    }
    general.textContent = errors.general || '';
    general.hidden = !errors.general;
    // ошибка в свёрнутом «Дополнительно» не должна прятаться
    if (errors.max_agents) advanced.open = true;
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
      title: titleInput.value, key: keyInput.value, harness, max_agents: limitInput.value,
      review: reviewBox.checked, agentTasks: tasksBox.checked,
    };
    const errors = validateNewProject(form);
    showErrors(errors);
    if (Object.keys(errors).length) return;
    sending = true;
    createBtn.disabled = true;
    client.post('/projects', projectPayload(form))
      .then(res => {
        close();
        toast(T.np.created(res.title, res.key));
        window.location.hash = boardHref(res.slug);
      })
      .catch(e => {
        const message = (e && e.message) || String(e);
        showErrors({ [projectErrorField(message) || 'general']: message });
      })
      .finally(() => {
        sending = false;
        createBtn.disabled = false;
      });
  }
  titleInput.focus();
  return close;
}
