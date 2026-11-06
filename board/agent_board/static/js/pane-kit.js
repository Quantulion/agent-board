// Общие помощники панелей карточки: разметка секций, регионы с перерисовкой по сигнатуре, копирование.
import { h, icon, safeMarkdown } from './dom.js';
import { T } from './i18n.js';
import { toast } from './ui.js';

export const isResearch = t => t.kind === 'research';

export const md = (text, cls) => h('div', { class: cls ? 'md ' + cls : 'md' }, safeMarkdown(text));
export const sec = (title, ...children) => h('section', { class: 'sec' }, h('h3', null, title), ...children);

// Регион перерисовывается, только если изменилась сигнатура его данных.
export function region(tag, cls) {
  const el = h(tag, { class: cls || 'slot' });
  let sig = null;
  return {
    el,
    paint(next, build) {
      const s = JSON.stringify(next);
      if (s === sig) return;
      sig = s;
      const nodes = [build()].flat(Infinity).filter(Boolean);
      el.replaceChildren(...nodes);
      el.hidden = !nodes.length;
    },
  };
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    const area = h('textarea', { class: 'offscreen', readonly: true });
    area.value = text;
    document.body.append(area);
    area.select();
    const ok = document.execCommand('copy');
    area.remove();
    if (!ok) {
      toast(T.card.copyFailed, { error: true });
      return;
    }
  }
  toast(T.card.copied(text));
}

export const copyButton = text => h('button', {
  class: 'btn ghost sm icon', type: 'button', title: T.card.copy, 'aria-label': T.card.copy,
  onclick: () => copyText(text),
}, icon('copy'));
