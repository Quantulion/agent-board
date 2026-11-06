// Вкладка «Ревью»: раунды ревью и ссылка на него.
import { h, icon, safeUrl } from './dom.js';
import { T } from './i18n.js';
import { region, sec } from './pane-kit.js';

export function reviewPane() {
  const content = region('div');
  const el = h('div', { class: 'pane' }, content.el);
  return {
    el,
    paint(t) {
      const r = t.review && typeof t.review === 'object' ? t.review : null;
      content.paint(r, () => {
        if (!r) return sec(T.card.reviewTitle, h('p', { class: 'muted' }, T.card.reviewEmpty));
        const history = Array.isArray(r.history) ? r.history : [];
        const url = typeof r.url === 'string' && safeUrl(r.url);
        return sec([T.card.reviewTitle, r.round ? h('span', { class: 'r' }, T.card.round(r.round)) : null],
          history.length || r.round ? h('div', { class: 'rounds' }, [
            ...history.map(roundRow),
            ...(r.round && !history.some(x => x && x.round === r.round) ? [roundRow({ round: r.round, state: r.state })] : []),
          ].map((node, i, all) => { if (i === all.length - 1) node.classList.add('cur'); return node; })) : null,
          url ? h('a', { href: url, target: '_blank', rel: 'noopener noreferrer', class: 'mono' }, icon('link'), url) : null);
      });
    },
  };
}

function roundRow(x) {
  const item = x && typeof x === 'object' ? x : { text: x };
  // формат истории ревью ещё не зафиксирован: любые значения выводятся строкой
  const str = v => (v === null || v === undefined ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v));
  const state = T.card.roundState[str(item.state)] || str(item.state);
  const text = str(item.text) || str(item.summary);
  return h('div', { class: 'round' }, h('span', { class: 'rn' }, item.round ? str(item.round) : '·'),
    h('div', null, h('div', { class: 'rt' }, state || text), state && text ? h('div', { class: 'rd' }, text) : null));
}
