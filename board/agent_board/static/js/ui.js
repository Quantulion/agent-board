// Общие элементы интерфейса: тосты и модальные окна.
import { h, icon } from './dom.js';
import { T } from './i18n.js';

const TOAST_GAP = 8;
const TOAST_BOTTOM = 18;

// Пока открыто модальное окно, тосты стоят над его подвалом и не закрывают кнопки.
function placeToasts(host) {
  const footers = document.querySelectorAll('.modal-host .modal footer');
  const footer = footers[footers.length - 1];
  const bottom = footer ? Math.max(TOAST_BOTTOM, window.innerHeight - footer.getBoundingClientRect().top + TOAST_GAP) : TOAST_BOTTOM;
  host.style.setProperty('bottom', bottom + 'px');
}

export function toast(text, options = {}) {
  let host = document.querySelector('.toasts');
  if (!host) {
    host = h('div', { class: 'toasts', role: 'status', 'aria-live': 'polite' });
    document.body.append(host);
  }
  placeToasts(host);
  const el = h('div', { class: options.error ? 'toast err' : 'toast', role: options.error ? 'alert' : null }, h('span', null, text));
  host.append(el);
  setTimeout(() => el.remove(), options.error ? 6000 : 4000);
  return el;
}

// Подтверждение действия: submit выполняется по кнопке, его ошибка показывается в окне, и окно остаётся открытым.
// Промис разрешается true после успешного submit и false при отмене — по нему кнопка действия знает, что всё кончилось.
export function confirmDialog({ title, body = [], ok, submit, focus }) {
  return new Promise(resolve => {
    const error = h('div', { class: 'ferr', role: 'alert', hidden: true });
    let done = false;
    let sending = false;
    const finish = value => {
      if (done) return;
      done = true;
      resolve(value);
    };
    const go = async () => {
      if (sending) return;
      sending = true;
      okBtn.disabled = true;
      error.hidden = true;
      try {
        await submit();
        finish(true);
        close();
      } catch (err) {
        error.textContent = (err && err.message) || String(err);
        error.hidden = false;
      } finally {
        sending = false;
        okBtn.disabled = false;
      }
    };
    const okBtn = h('button', { class: 'btn primary', type: 'button', 'data-ok': '1', onclick: go }, ok);
    const { close } = openModal(title, [...[body].flat(), error],
      [h('span', { class: 'grow' }), h('button', { class: 'btn', type: 'button', onclick: () => close() }, T.cancel), okBtn],
      () => finish(false));
    (focus || okBtn).focus();
  });
}

// Модальное окно поверх всего; Esc и клик мимо закрывают только его. Возвращает {close, modal}.
export function openModal(title, body, footer, onClose) {
  const before = document.activeElement;
  const close = () => {
    document.removeEventListener('keydown', onKey, true);
    host.remove();
    const toasts = document.querySelector('.toasts');
    if (toasts) placeToasts(toasts);
    if (onClose) onClose();
    if (before && before.isConnected && typeof before.focus === 'function') before.focus();
  };
  const onKey = e => {
    if (e.key !== 'Escape' || e.isComposing || e.keyCode === 229) return;
    e.stopPropagation();
    e.preventDefault();
    close();
  };
  const modal = h('div', { class: 'modal', role: 'dialog', 'aria-modal': 'true', 'aria-label': title },
    h('header', null, h('h2', null, title),
      h('button', { class: 'btn icon ghost', type: 'button', 'aria-label': T.close, onclick: close }, icon('x'))),
    h('div', { class: 'body' }, body),
    h('footer', null, footer));
  const host = h('div', { class: 'modal-host' }, h('div', { class: 'scrim', onclick: close }), modal);
  document.addEventListener('keydown', onKey, true);
  document.body.append(host);
  return { close, modal };
}
