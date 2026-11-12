// Окно «Новый проект». Пока создание проекта из интерфейса не готово, окно подсказывает команду терминала.
import { h } from './dom.js';
import { T } from './i18n.js';
import { openModal } from './ui.js';

export function openNewProjectDialog() {
  const { close } = openModal(T.home.stubTitle,
    [h('p', { class: 'mtext' }, T.home.stubText), h('p', null, h('code', { class: 'inline-code' }, 'agent-board init <key> --kind free --title "…"'))],
    [h('span', { class: 'grow' }), h('button', { class: 'btn primary', type: 'button', onclick: () => close() }, T.close)]);
}
