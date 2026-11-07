// Вкладка «Обзор»: вопросы, предложения, описание, зависимости, ветка и данные трекера.
import { spill, statusOf } from './chips.js';
import { h, icon, patchList } from './dom.js';
import { httpUrl } from './review.js';
import { T, statusInfo } from './i18n.js';
import { reportHref } from './changes.js';
import { copyButton, isResearch, md, region, sec } from './pane-kit.js';
import { proposalNote, proposalSignature, proposalsTouching } from './project.js';
import { shipSection } from './ship.js';
import { slotNote, slotWait } from './slot.js';
import { externalBlockerDialog } from './status.js';
import { retryControl } from './retry.js';
import { formatTime } from './thread.js';
import { toast } from './ui.js';

export function overviewPane(pc) {
  const { key, ctx, client, slug, drafts } = pc;
  const questions = h('div', { class: 'qlist' });
  const proposals = h('div', { class: 'qlist' });
  const parts = {
    slot: region('div'), error: region('div'), ship: region('div'), violations: region('div'), task: region('div'), summary: region('div'),
    deps: region('div'), work: region('div'), tracker: region('div'),
  };
  const el = h('div', { class: 'dgrid' },
    h('main', null, questions, proposals, parts.slot.el, parts.error.el, parts.ship.el, parts.violations.el, parts.task.el, parts.summary.el),
    h('aside', null, parts.deps.el, parts.work.el, parts.tracker.el));
  return {
    el,
    paint(t, snap) {
      const open = (t.questions || []).filter(q => !q.answer);
      patchList(questions, open, 'id', q => questionForm(pc, q));
      questions.hidden = !open.length;
      const byKey = new Map(snap.tasks.map(x => [x.key, x]));
      const touching = proposalsTouching(snap.proposals || [], key);
      // узел пересоздаётся только при смене описания: состояние кнопок переживает прочие обновления снимка
      patchList(proposals, touching, p => proposalSignature(p, byKey), p => proposalNote(ctx, p, byKey));
      proposals.hidden = !touching.length;
      const wait = slotWait(t.agent_state, t.wait_reason, snap.config);
      parts.slot.paint(wait, () => wait ? slotNote(wait) : null);
      const failed = t.agent_state === 'error';
      parts.error.paint([t.turn_error, failed], () => t.turn_error || failed ? h('section', { class: 'sec err' },
        h('h3', null, icon('warn'), T.card.turnError),
        t.turn_error ? h('pre', { class: 'errtext' }, t.turn_error) : null,
        failed ? retryControl(client, key, () => toast(T.agentRetry.started(key))).el : null) : null);
      parts.ship.paint(t.ship || null, () => shipSection(t));
      const violations = t.rule_violations || [];
      parts.violations.paint(violations, () => violations.length ? sec(T.card.violations,
        h('ul', { class: 'violations' }, violations.map(v => h('li', null,
          v.rule ? h('b', null, v.rule) : null, v.rule && v.text ? ' — ' : null, v.text || null)))) : null);
      parts.task.paint([t.description, t.done_when], () => sec(T.card.task,
        t.description ? md(t.description) : h('p', { class: 'muted' }, T.card.noDescription),
        t.done_when ? h('div', { class: 'dod' }, h('b', null, T.card.doneWhen), md(t.done_when)) : null));
      const todo = Array.isArray(t.todo) ? t.todo : [];
      parts.summary.paint([t.summary, todo], () => (t.summary || todo.length) ? sec(T.card.summary,
        t.summary ? md(t.summary, 'summary-txt') : null,
        todo.length ? h('ul', { class: 'todolist' }, todo.map(item => h('li', { class: item && item.done ? 'd' : null },
          h('span', { class: 'cb' }, item && item.done ? icon('check') : null),
          h('span', null, item && typeof item === 'object' ? String(item.text ?? '') : String(item))))) : null) : null);
      paintDeps(pc, parts.deps, t, snap);
      paintWork(pc, parts.work, t);
      paintTracker(parts.tracker, t);
    },
  };
}

function questionForm(pc, q) {
  const { key, client, drafts } = pc;
  const name = 'q-' + key + '-' + q.id;
  const options = Array.isArray(q.options) ? q.options : [];
  // комментарий и выбранный вариант переживают закрытие карточки и перезагрузку, пока ответ не отправлен
  const draftKey = key + '#q:' + q.id;
  const choiceKey = draftKey + ':choice';
  const savedChoice = drafts.get(choiceKey);
  const comment = h('textarea', { placeholder: options.length ? T.card.answerComment : T.card.answerFree });
  comment.value = drafts.get(draftKey);
  comment.addEventListener('input', () => drafts.set(draftKey, comment.value));
  const error = h('div', { class: 'ferr', role: 'alert', hidden: true });
  const send = h('button', { class: 'btn primary sm', type: 'button', onclick: submit }, icon('send'), T.card.answer);
  const radios = [];
  const node = h('section', { class: 'question' },
    h('div', { class: 'qh' }, icon('chat'), T.card.question),
    h('div', { class: 'qt' }, q.text),
    q.context ? h('details', null, h('summary', null, T.card.context), md(q.context, 'ctx')) : null,
    options.length ? h('div', { class: 'opts' }, options.map(opt => {
      const rec = q.recommended === opt;
      const radio = h('input', { type: 'radio', name, value: opt });
      radio.checked = savedChoice && options.includes(savedChoice) ? savedChoice === opt : rec;
      radio.addEventListener('change', () => { if (radio.checked) drafts.set(choiceKey, opt); });
      radios.push(radio);
      return h('label', { class: rec ? 'opt rec' : 'opt' }, radio,
        h('div', null, h('div', { class: 'ot' }, opt, rec ? h('span', { class: 'rtag' }, T.card.recommended) : null)));
    })) : null,
    q.recommended && !options.includes(q.recommended)
      ? h('div', { class: 'rec-why' }, icon('spark'), h('span', null, T.card.recommended + ': ' + q.recommended)) : null,
    h('div', { class: 'composer' }, comment,
      h('div', { class: 'row' }, h('span', { class: 'faint hint' }, T.card.answerHint), send)),
    error);
  comment.addEventListener('keydown', e => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && !e.isComposing) {
      e.preventDefault();
      submit();
    }
  });

  function submit() {
    if (send.disabled) return;
    const picked = radios.find(r => r.checked);
    const text = comment.value.trim();
    if (!picked && !text) {
      error.textContent = T.card.answerNeedText;
      error.hidden = false;
      return;
    }
    error.hidden = true;
    const controls = [send, comment, ...radios];
    controls.forEach(c => { c.disabled = true; });
    client.post('/tasks/' + encodeURIComponent(key) + '/questions/' + encodeURIComponent(q.id) + '/answer',
      { choice: picked ? picked.value : null, comment: text })
      .then(() => {
        drafts.set(draftKey, '');
        drafts.set(choiceKey, '');
        toast(T.card.answered(key));
      })
      .catch(err => {
        controls.forEach(c => { c.disabled = false; });
        error.textContent = (err && err.message) || String(err);
        error.hidden = false;
      });
  }
  return node;
}

function paintDeps(pc, part, t, snap) {
  const { key, ctx } = pc;
  const byKey = new Map(snap.tasks.map(x => [x.key, x]));
  const openDeps = new Set(t.open_deps || []);
  const deps = (t.deps || []).map(k => {
    const d = byKey.get(k);
    return { key: k, title: d ? d.title : '', status: d ? statusOf(d) : null, done: !openDeps.has(k) };
  });
  const blocks = snap.tasks.filter(x => (x.deps || []).includes(key)).map(x => ({ key: x.key, title: x.title, status: statusOf(x) }));
  const ext = typeof t.external_blocker === 'string' ? t.external_blocker.trim() : '';
  const depRow = (d, done) => h('div', {
    class: 'dep', role: 'button', tabindex: '0',
    onclick: () => ctx.openCard(d.key),
    onkeydown: e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); ctx.openCard(d.key); } },
  }, h('span', { class: 'k' }, d.key), h('span', { class: 'tt' }, d.title),
  done ? h('span', { class: 'okm', title: d.status ? statusInfo(d.status).label : null }, icon('check'))
    : d.status ? spill(d.status) : null);
  part.paint([deps, blocks, ext], () => sec(T.card.deps,
    deps.length ? h('div', { class: 'deps' }, deps.map(d => depRow(d, d.done))) : h('p', { class: 'muted' }, T.card.noDeps),
    ext ? h('div', { class: 'xblock' }, icon('lock'), h('span', null, ext),
      h('button', { class: 'btn ghost sm', type: 'button', onclick: () => externalBlockerDialog(ctx, pc.task()) }, T.card.editBlocker)) : null,
    blocks.length ? [h('div', { class: 'muted sub' }, T.card.blocks), h('div', { class: 'deps' }, blocks.map(d => depRow(d, false)))] : null));
}

function paintWork(pc, part, t) {
  const { slug } = pc;
  if (isResearch(t)) {
    const href = reportHref(slug, t);
    part.paint(['r', href, t.report, t.worktree], () => sec(T.card.result,
      href ? h('p', null, h('a', { href, target: '_blank', rel: 'noopener noreferrer', class: 'report' },
        icon('doc'), t.report.split('/').pop())) : h('p', { class: 'muted' }, T.card.noReport),
      t.worktree ? h('div', { class: 'pathrow' }, h('div', { class: 'path' }, t.worktree), copyButton(t.worktree)) : null));
    return;
  }
  part.paint(['c', t.base_branch, t.branch, t.worktree], () => sec(T.card.branch,
    t.branch || t.worktree ? [
      h('div', { class: 'chain' },
        t.base_branch ? h('div', { class: 'b' }, h('i'), t.base_branch, h('span', { class: 'n' }, T.card.base)) : null,
        t.branch ? h('div', { class: 'b me' }, h('i'), t.branch, h('span', { class: 'n' }, T.card.thisTask)) : null),
      t.worktree ? h('div', { class: 'pathrow' }, h('div', { class: 'path' }, t.worktree), copyButton(t.worktree)) : null,
    ] : h('p', { class: 'muted' }, T.card.noWorktree)));
}

function paintTracker(part, t) {
  const rows = [];
  if (t.tracker_key) {
    const url = httpUrl(t.tracker_url);
    rows.push([T.card.trackerKey, url ? h('a', { href: url, target: '_blank', rel: 'noopener noreferrer' }, t.tracker_key) : t.tracker_key]);
  }
  if (t.session_id) rows.push([T.card.session, h('span', { class: 'mono' }, t.session_id)]);
  if (t.harness) rows.push([T.card.harness, t.harness]);
  if (t.last_turn_at) rows.push([T.card.lastTurn, formatTime(t.last_turn_at)]);
  part.paint([t.tracker_key, t.tracker_url, t.session_id, t.harness, t.last_turn_at], () => rows.length
    ? sec(T.card.tracker, h('dl', { class: 'kv' }, rows.map(([k, v]) => [h('dt', null, k), h('dd', null, v)]))) : null);
}
