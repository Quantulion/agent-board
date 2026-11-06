// Ревью задачи: состояние раунда, чип на карточке доски и действия шапки карточки
// «Начать ревью», «Открыть ревью», «Одобрить», «Вернуть в работу…».
import { registerCardAction } from './actions.js';
import { statusOf } from './chips.js';
import { h, safeUrl } from './dom.js';
import { T } from './i18n.js';
import { isResearch } from './pane-kit.js';
import { confirmDialog, toast } from './ui.js';

// Ссылка открывается, только если это абсолютный адрес http(s).
export const httpUrl = url => (typeof url === 'string' && /^https?:\/\//i.test(url.trim()) ? safeUrl(url) : null);

// Состояния ревью и отправки, которые могут попасть в имя класса; остальное от сервера в разметку не идёт.
const KNOWN_STATES = new Set(['idle', 'open', 'changes', 'fixing', 'approved',
  'queued', 'running', 'done', 'error', 'skipped', 'interrupted', 'cancelled']);
export const stateClass = state => (KNOWN_STATES.has(state) ? state : '');

export const reviewTool = snapshot => {
  const review = snapshot && snapshot.config && snapshot.config.review;
  return review && review.tool === 'none' ? 'none' : 'crit';
};

// Раунд ревью задачи в удобном для вывода виде; null — ревью ещё не было.
export function reviewOf(task) {
  const r = task && task.review;
  if (!r || typeof r !== 'object') return null;
  return {
    round: Number.isInteger(r.round) ? r.round : 0,
    state: typeof r.state === 'string' ? r.state : 'idle',
    url: httpUrl(r.url),
    error: typeof r.error === 'string' && r.error.trim() ? r.error.trim() : null,
    history: (Array.isArray(r.history) ? r.history : []).filter(x => x && typeof x === 'object'),
  };
}

// Чип раунда на карточке доски: только пока задача на ревью или одобрена.
export function reviewChip(task) {
  const status = statusOf(task);
  const r = reviewOf(task);
  if (status === 'approved') return { text: T.review.chipApproved, cls: 'ok' };
  if (!r || status !== 'in_review') return null;
  if (r.state === 'idle') return r.error ? { text: T.review.chipError, cls: 'err' } : null;
  const label = T.review.chip[r.state];
  if (!label || !r.round) return null;
  return { text: T.roundShort(r.round) + ' · ' + label, cls: r.state === 'open' ? 'rv' : '' };
}

const inReview = task => statusOf(task) === 'in_review';
const roundState = task => (reviewOf(task) || { state: 'idle' }).state;
const path = (task, action) => '/tasks/' + encodeURIComponent(task.key) + '/' + action;

export function approveTask(ctx, task) {
  return confirmDialog({
    title: T.review.approveTitle(task.key),
    body: h('p', { class: 'mtext' }, T.review.approveText),
    ok: T.review.approve,
    submit: async () => {
      await ctx.client.post(path(task, 'approve'), { actor: 'user' });
      toast(T.review.approved(task.key));
    },
  });
}

export function returnDialog(ctx, task) {
  const id = 'return-' + task.key;
  const input = h('textarea', { id, rows: '4', placeholder: T.review.returnPh });
  return confirmDialog({
    title: T.review.returnTitle(task.key),
    body: h('div', { class: 'field' }, h('label', { for: id }, T.review.returnLabel), input),
    ok: T.review.returnOk,
    focus: input,
    submit: async () => {
      const text = input.value.trim();
      if (!text) {
        input.focus();
        throw new Error(T.review.returnNeed);
      }
      await ctx.client.post(path(task, 'return'), { actor: 'user', text });
      toast(T.review.returned(task.key));
    },
  });
}

export function startReview(ctx, task) {
  return ctx.client.post(path(task, 'review'), { actor: 'user' }).then(() => toast(T.review.started(task.key)));
}

export function openReview(task) {
  const r = reviewOf(task);
  if (r && r.url) window.open(r.url, '_blank', 'noopener,noreferrer');
}

registerCardAction({
  id: 'review-start',
  label: () => T.review.start,
  icon: 'eye',
  visible: (task, snap) => !isResearch(task) && inReview(task) && reviewTool(snap) === 'crit'
    && !['open', 'fixing'].includes(roundState(task)),
  run: startReview,
});

registerCardAction({
  id: 'review-open',
  label: () => T.review.open,
  icon: 'ext',
  visible: task => !isResearch(task) && roundState(task) === 'open' && Boolean(reviewOf(task).url),
  title: task => (reviewOf(task) || {}).url || null,
  run: (ctx, task) => openReview(task),
});

registerCardAction({
  id: 'review-approve',
  label: () => T.review.approve,
  icon: 'check',
  visible: task => !isResearch(task) && inReview(task),
  run: approveTask,
});

registerCardAction({
  id: 'review-return',
  label: () => T.review.return,
  icon: 'chat',
  visible: task => !isResearch(task) && ['in_review', 'approved', 'sent'].includes(statusOf(task)),
  run: returnDialog,
});
