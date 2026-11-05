// Тред переписки с агентом: список сообщений, поле ввода с черновиком и отметка о прочтении.
// Общий для карточки задачи и панели «Проект».
import { h, icon, patchList, safeMarkdown } from './dom.js';
import { T } from './i18n.js';

// столько пикселей от низа ещё считается «внизу»: новое сообщение прокрутит к себе
const BOTTOM_SLACK = 40;

export function authorOf(author) {
  if (author === 'user') return { cls: 'user', label: T.authors.user };
  if (author === 'agent') return { cls: 'agent', label: T.authors.agent };
  if (author === 'project_agent') return { cls: 'pa', label: T.authors.project_agent };
  return { cls: 'sys', label: T.authors.system };
}

const pad = n => String(n).padStart(2, '0');

export function formatTime(iso, now = new Date()) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const time = pad(d.getHours()) + ':' + pad(d.getMinutes());
  const sameDay = d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  return sameDay ? time : d.getDate() + ' ' + T.card.months[d.getMonth()] + ' ' + time;
}

// Черновики по ключу (тред, вопрос): в памяти и в sessionStorage, чтобы пережить перерисовки, закрытие и перезагрузку.
export function createDrafts(storage, slug) {
  const mem = new Map();
  const id = key => 'ab-draft:' + slug + ':' + key;
  return {
    get(key) {
      if (mem.has(key)) return mem.get(key);
      try {
        return (storage && storage.getItem(id(key))) || '';
      } catch (e) {
        return '';
      }
    },
    set(key, text) {
      mem.set(key, text);
      try {
        if (text) storage.setItem(id(key), text);
        else storage.removeItem(id(key));
      } catch (e) {
        // хранилище недоступно — черновик живёт в памяти вкладки
      }
    },
  };
}

let shared = null;

// Одни черновики на проект для всех тредов и форм ответа.
export function draftsFor(slug) {
  if (!shared || shared.slug !== slug) shared = Object.assign(createDrafts(globalThis.sessionStorage, slug), { slug });
  return shared;
}

const md = (text, cls) => h('div', { class: cls ? 'md ' + cls : 'md' }, safeMarkdown(text));

// agentName — подпись и аватар агента треда (у задачи — её ключ).
export function renderMessage(m, agentName) {
  const who = authorOf(m.author);
  const time = h('span', { class: 't', title: m.at || null }, formatTime(m.at));
  if (who.cls === 'sys') return h('div', { class: 'sys' }, icon('clock'), md(m.text), time);
  const name = agentName || '';
  const avatar = who.cls === 'user' ? T.authors.user
    : who.cls === 'pa' ? icon('spark') : (String(name.split('-').pop()).slice(0, 3) || 'AI');
  return h('div', { class: 'msg ' + who.cls },
    h('span', { class: 'av' }, avatar),
    h('div', null,
      h('div', { class: 'mh' }, h('b', null, who.cls === 'agent' && name ? who.label + ' ' + name : who.label),
        m.question_id ? h('span', { class: 'chip' }, T.card.answerChip) : null, time),
      md(m.text, who.cls === 'pa' ? 'mb bubble' : 'mb')));
}

// Тред thread. scroller — прокручиваемый контейнер списка; visible() — тред сейчас на экране;
// alive() — владелец ещё открыт (ответы сервера после закрытия игнорируются);
// draftKey — под каким ключом хранить черновик, если у того же треда есть другое поле ввода.
export function createThread({ client, thread, drafts, scroller, visible, alive, placeholder, hint, agentName, draftKey = thread }) {
  const list = h('div', { class: 'thread' });
  const empty = h('p', { class: 'muted', hidden: true }, T.card.noMessages);
  const loadError = h('div', { class: 'ferr', role: 'alert', hidden: true });
  const sendError = h('div', { class: 'ferr', role: 'alert', hidden: true });
  const area = h('textarea', { placeholder, 'aria-label': placeholder });
  area.value = drafts.get(draftKey);
  const send = h('button', { class: 'btn primary sm', type: 'button', onclick: submit }, icon('send'), T.card.send);
  const composer = h('div', { class: 'composer' }, area,
    h('div', { class: 'row' }, h('span', { class: 'faint hint' }, hint || T.card.composerHint), send));
  area.addEventListener('input', () => drafts.set(draftKey, area.value));
  area.addEventListener('keydown', e => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && !e.isComposing) {
      e.preventDefault();
      submit();
    }
  });

  const path = '/threads/' + encodeURIComponent(thread);
  let messages = [];
  let size = 0;
  let unread = 0;
  let loading = false;
  let reading = false;
  let firstShown = false;
  let locked = false;
  let sending = false;

  const atBottom = () => scroller().scrollHeight - scroller().scrollTop - scroller().clientHeight < BOTTOM_SLACK;
  const toBottom = () => { scroller().scrollTop = scroller().scrollHeight; };

  async function load() {
    if (loading) return;
    loading = true;
    try {
      const res = await client.get(path);
      if (!alive()) return;
      loadError.hidden = true;
      const stick = visible() && atBottom();
      messages = (res && res.messages) || [];
      patchList(list, messages, 'id', m => renderMessage(m, agentName));
      empty.hidden = messages.length > 0;
      if (stick) toBottom();
    } catch (err) {
      loadError.textContent = T.card.threadLoadFailed + ((err && err.message) || String(err));
      loadError.hidden = false;
    } finally {
      loading = false;
    }
    if (alive() && size > messages.length && loadError.hidden) load();
  }

  function markRead() {
    if (reading || !visible() || !(unread > 0)) return;
    reading = true;
    client.post(path + '/read').catch(() => {}).finally(() => { reading = false; });
  }

  function submit() {
    const text = area.value.trim();
    if (!text || send.disabled || locked) return;
    // поле сразу свободно для следующего сообщения; при ошибке отправленный текст возвращается в него
    const sent = area.value;
    area.value = '';
    drafts.set(draftKey, '');
    send.disabled = true;
    sending = true;
    sendError.hidden = true;
    client.post(path + '/messages', { author: 'user', text })
      .then(() => { if (alive()) toBottom(); })
      .catch(err => {
        area.value = area.value ? sent + '\n\n' + area.value : sent;
        drafts.set(draftKey, area.value);
        sendError.textContent = (err && err.message) || String(err);
        sendError.hidden = false;
      })
      .finally(() => {
        sending = false;
        send.disabled = locked;
      });
  }

  return {
    // список и поле ввода раскладывает владелец: в карточке они рядом, в панели поле прижато к низу
    list: [loadError, list, empty],
    composer: [composer, sendError],
    area,
    // meta — {size, unread}: число сообщений на сервере и непрочитанное
    paint(meta) {
      size = meta.size || 0;
      unread = meta.unread || 0;
      empty.hidden = messages.length > 0 || size > 0;
      if (size > messages.length) load();
      markRead();
    },
    // только чтение: история видна, писать нельзя
    setLocked(on) {
      locked = Boolean(on);
      area.disabled = locked;
      send.disabled = locked || sending;
    },
    shown() {
      if (!firstShown) {
        firstShown = true;
        toBottom();
      }
      markRead();
    },
  };
}
