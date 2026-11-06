// Вкладка «Ревью»: текущий раунд и его состояние, ссылка на страницу ревью и история раундов.
// Без инструмента ревью — подсказка смотреть «Изменения» и кнопки одобрения и возврата.
import { statusOf } from './chips.js';
import { h, icon } from './dom.js';
import { T } from './i18n.js';
import { region, sec } from './pane-kit.js';
import { approveTask, returnDialog, reviewOf, reviewTool } from './review.js';
import { formatTime } from './thread.js';

export function reviewPane(pc) {
  const content = region('div');
  const el = h('div', { class: 'pane' }, content.el);
  return {
    el,
    paint(t, snap) {
      const r = reviewOf(t);
      const tool = reviewTool(snap);
      const status = statusOf(t);
      content.paint([t.review, tool, status], () => [
        tool === 'none' ? noneSection(pc, status) : null,
        current(r, status, tool),
        r && (r.history.length || r.round) ? historySection(r) : null,
      ]);
    },
  };
}

function current(r, status, tool) {
  if (tool === 'none' && !r) return null;
  if (!r) return sec(T.card.reviewTitle, h('p', { class: 'muted' }, status === 'in_review' ? T.review.stateHint.idle : T.review.notYet));
  const label = T.review.state[r.state] || r.state;
  return h('section', { class: 'sec rvcur st-' + r.state },
    h('h3', null, T.card.reviewTitle, r.round ? h('span', { class: 'r' }, T.card.round(r.round)) : null),
    h('div', { class: 'rvstate' }, h('span', { class: 'rvpill ' + r.state }, label),
      T.review.stateHint[r.state] ? h('span', { class: 'muted' }, T.review.stateHint[r.state]) : null),
    r.state === 'open' && r.url
      ? h('a', { href: r.url, target: '_blank', rel: 'noopener noreferrer', class: 'rvlink mono' }, icon('ext'), r.url) : null,
    r.error ? h('div', { class: 'ferr' }, h('b', null, T.review.error + ': '), r.error) : null);
}

function noneSection(pc, status) {
  const acting = status === 'in_review';
  const btn = (label, ic, cls, run) => {
    const b = h('button', { class: 'btn sm ' + cls, type: 'button', onclick: async () => {
      b.disabled = true;
      try {
        await run();
      } finally {
        b.disabled = false;
      }
    } }, icon(ic), label);
    return b;
  };
  return h('section', { class: 'sec rvnone' },
    h('p', null, T.review.noneHint),
    h('div', { class: 'row' },
      btn(T.review.toChanges, 'doc', 'ghost', () => pc.showTab('changes')),
      h('span', { class: 'grow' }),
      acting || ['approved', 'sent'].includes(status) ? btn(T.review.return, 'chat', '', () => returnDialog(pc.ctx, pc.task())) : null,
      acting ? btn(T.review.approve, 'check', 'primary', () => approveTask(pc.ctx, pc.task())) : null));
}

function historySection(r) {
  const rows = [...r.history];
  if (r.round && !rows.some(x => x.round === r.round)) rows.push({ round: r.round, state: r.state });
  return sec(T.review.history, h('div', { class: 'rounds' }, rows.map((x, i) => roundRow(x, i === rows.length - 1))));
}

function roundRow(x, isLast) {
  // записи истории ведёт сервер; посторонние значения выводятся строкой
  const str = v => (v === null || v === undefined ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v));
  const state = T.card.roundState[str(x.state)] || str(x.state);
  const details = [
    Number.isInteger(x.comments) ? (x.comments ? T.review.comments(x.comments) : T.review.noComments) : '',
    str(x.text) || str(x.summary),
    x.at ? formatTime(str(x.at)) : '',
  ].filter(Boolean).join(' · ');
  return h('div', { class: isLast ? 'round cur' : 'round' }, h('span', { class: 'rn' }, x.round ? str(x.round) : '·'),
    h('div', null, h('div', { class: 'rt' }, state || details), state && details ? h('div', { class: 'rd' }, details) : null));
}
