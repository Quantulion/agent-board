// Общие чипы статуса: цвет, точка и пилюля; единственный источник для всех видов.
import { h } from './dom.js';
import { statusInfo } from './i18n.js';

export const statusOf = t => t.display_status || t.status;
export const color = id => 'var(' + statusInfo(id).color + ')';
export const sdot = id => h('span', { class: 'sdot', style: { '--c': color(id) } });
export const spill = id => h('span', { class: 'spill', style: { '--c': color(id) } }, sdot(id), statusInfo(id).label);
