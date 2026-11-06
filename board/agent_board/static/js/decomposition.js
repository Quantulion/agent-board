// Согласование разбивки: задачи с нарушениями правил и комментариями, граф по шагам, общий тред с агентом проекта
// и утверждение, которое переводит проект на следующую стадию. После утверждения страница только для чтения.
import { statusOf } from './chips.js';
import { connectionIndicator, paintStageAction, paintStepper } from './header.js';
import { h, icon, patchList, safeMarkdown } from './dom.js';
import { criticalPath, layoutGraph, laneLabels, mountGraph } from './graph.js';
import { T } from './i18n.js';
import { PROJECT_THREAD, agentStateLine, proposalCard, proposalSignature } from './project.js';
import { boardHref } from './router.js';
import { createThread, draftsFor } from './thread.js';
import { openModal, toast } from './ui.js';

const KEYS = new Intl.Collator('ru', { numeric: true });
const REPO_PALETTE = ['--s-progress', '--s-approved', '--s-answer', '--s-sent', '--s-review', '--s-queue', '--s-done'];
const HIGHLIGHT_MS = 1600;

const text = v => (typeof v === 'string' ? v.trim() : '');
const estimateOf = t => (typeof t.estimate === 'number' && Number.isFinite(t.estimate) && t.estimate >= 0 ? t.estimate : null);
const violationsOf = t => (Array.isArray(t.rule_violations) ? t.rule_violations : [])
  .filter(v => v && typeof v === 'object')
  .map(v => ({ rule: String(v.rule ?? ''), text: String(v.text ?? '') }));

// --- Чистые функции ---

// Стадия после утверждения: заведение задач в трекере, если он подключён, иначе сразу работа агентов.
// Черновик комментария к задаче разбивки — свой, не общий с полем ввода в карточке задачи.
export const commentDraftKey = key => 'decomp:' + key;

// Правила, по которым проверяется разбивка.
export const rulesText = config => text(config && config.decomposition_rules);

export function nextStage(config) {
  const type = config && config.tracker && config.tracker.type;
  return type && type !== 'none' ? 'tracker' : 'work';
}

// Цвет репозитория на графе и в списке: по порядку репозиториев проекта, затем — встреченных у задач.
export function repoColors(repos, tasks) {
  const names = [...(repos || []), ...(tasks || []).map(t => t.repo)].filter(Boolean);
  const colors = new Map();
  for (const name of names) {
    if (!colors.has(name)) colors.set(name, 'var(' + REPO_PALETTE[colors.size % REPO_PALETTE.length] + ')');
  }
  return colors;
}

// Всё, что страница показывает о разбивке; отменённые задачи в неё не входят.
export function decompositionSummary(snapshot) {
  const config = snapshot.config || {};
  const tasks = (snapshot.tasks || []).filter(t => statusOf(t) !== 'cancelled').sort((a, b) => KEYS.compare(a.key, b.key));
  const violations = tasks.flatMap(t => violationsOf(t).map(v => ({ key: t.key, rule: v.rule, text: v.text })));
  const layout = layoutGraph(tasks);
  const lanes = laneLabels(layout);
  return {
    tasks,
    violations,
    missingDod: tasks.filter(t => !text(t.done_when)).map(t => t.key),
    estimate: tasks.reduce((sum, t) => sum + (estimateOf(t) ?? 0), 0),
    critical: criticalPath(tasks),
    steps: lanes.length,
    parallel: Math.max(0, ...lanes.map(l => l.count)),
    repos: [...new Set(tasks.map(t => t.repo).filter(Boolean))].sort(),
    messages: tasks.reduce((sum, t) => sum + (t.thread_size || 0), 0),
    // стартуют первыми после утверждения: без открытых зависимостей и внешнего блокера
    startKeys: tasks.filter(t => !(t.open_deps || []).length && !text(t.external_blocker)).map(t => t.key),
    readOnly: config.stage !== 'decomposition',
    next: nextStage(config),
  };
}

// --- Вид ---

export function mountDecomposition(root, store, ctx) {
  const slug = ctx.slug;
  const drafts = draftsFor(slug);
  const expanded = new Set();
  const entries = new Map();
  let alive = true;
  let summary = null;
  let colors = new Map();
  let highlightTimer = null;

  const title = h('h1', null, h('span', { class: 'skel title' }));
  const sub = h('span', { class: 'sub' }, T.dc.sub);
  const stepper = h('div', { class: 'stepper', 'aria-label': T.stageAria });
  const stageGo = h('span', { class: 'stage-go', hidden: true });
  const syncText = h('span');
  const sync = h('div', { class: 'sync static busy', role: 'status' }, h('span', { class: 'sdot' }), syncText);
  const banner = h('div', { class: 'banner-slot', hidden: true });
  const lead = h('p');
  const sources = h('nav', { class: 'src', 'aria-label': T.dc.sources });
  const doneBanner = h('div', { class: 'done-banner', role: 'status', hidden: true });
  const stats = h('div', { class: 'stats' });

  const vsumCount = h('h3');
  const vsumList = h('div', { class: 'rules' });
  const vsum = h('section', { class: 'panel vsum', hidden: true, 'aria-label': T.dc.violationsTitle },
    h('header', null, icon('warn'), vsumCount, h('span', { class: 'grow' }), h('span', { class: 'muted small-note' }, T.dc.violationsNote)),
    vsumList);

  const legend = h('div', { class: 'legend' });
  const critToggle = h('input', { type: 'checkbox', checked: true, onchange: () => paintGraph() });
  const graphHost = h('div', { class: 'graph-wrap' });
  const graphEmpty = h('p', { class: 'muted', hidden: true }, T.graphEmpty);
  const graph = mountGraph(graphHost, key => focusTask(key));

  const taskList = h('div', { class: 'tasks' });
  const rulesBody = h('div', { class: 'rtext' });

  const gScroll = h('div', { class: 'gscroll' });
  const proposals = h('div', { class: 'popen' });
  const general = createThread({
    client: ctx.client, thread: PROJECT_THREAD, drafts,
    scroller: () => gScroll,
    visible: () => alive && document.visibilityState !== 'hidden',
    alive: () => alive,
    placeholder: T.dc.generalPh,
    hint: T.dc.generalHint,
  });
  gScroll.append(...general.list, proposals);
  // писать можно только после того, как снимок покажет, что разбивка ещё не утверждена
  general.setLocked(true);

  const footState = h('div', { class: 'st' });
  const approveBtn = h('button', { class: 'btn primary', type: 'button', onclick: () => confirmApprove() });

  const scroll = h('main', { class: 'dscroll' },
    h('div', { class: 'dwrap' },
      banner,
      h('div', { class: 'intro' }, h('div', null, h('h2', null, T.dc.heading), lead), sources),
      doneBanner,
      stats,
      vsum,
      h('section', { class: 'panel' },
        h('header', null, h('h3', null, T.graphAria), h('span', { class: 'grow' }), legend,
          h('label', { class: 'toggle' }, critToggle, ' ' + T.dc.criticalToggle)),
        graphEmpty, graphHost),
      h('div', { class: 'dgrid-main' },
        taskList,
        h('aside', null,
          h('section', { class: 'panel' },
            h('header', null, h('h3', null, T.dc.rulesTitle), h('span', { class: 'grow' }), h('span', { class: 'muted small-note' }, T.dc.rulesNote)),
            rulesBody),
          h('section', { class: 'panel gthread' },
            h('header', null, h('h3', null, T.dc.generalTitle), h('span', { class: 'grow' }),
              h('span', { class: 'chip pa' }, icon('spark'), T.authors.project_agent.toLowerCase())),
            gScroll,
            ...general.composer)))));

  root.replaceChildren(h('div', { class: 'app decomp' },
    h('header', { class: 'top' },
      h('div', { class: 'row1' },
        ctx.brand(), h('span', { class: 'divider' }),
        h('div', { class: 'proj' }, title, sub),
        stepper, stageGo, h('span', { class: 'spacer' }), sync,
        h('a', { class: 'btn ghost', href: boardHref(slug) }, icon('board'), T.board),
        ctx.themeButton())),
    scroll,
    h('div', { class: 'footbar' }, h('div', { class: 'in' }, footState, h('span', { class: 'grow' }), approveBtn))));
  general.shown();

  // --- задачи ---

  function focusTask(key) {
    const entry = entries.get(key);
    if (!entry) return;
    setExpanded(key, true);
    entry.node.scrollIntoView({ behavior: 'smooth', block: 'center' });
    for (const e of entries.values()) e.node.classList.remove('hl');
    entry.node.classList.add('hl');
    clearTimeout(highlightTimer);
    highlightTimer = setTimeout(() => entry.node.classList.remove('hl'), HIGHLIGHT_MS);
  }

  const keyLink = key => h('button', { type: 'button', class: 'lnk mono', onclick: () => focusTask(key) }, key);

  function setExpanded(key, on) {
    const entry = entries.get(key);
    if (!entry) return;
    if (on) expanded.add(key);
    else expanded.delete(key);
    entry.node.classList.toggle('open', on);
    entry.comments.hidden = !on;
    entry.cmBtn.setAttribute('aria-expanded', String(on));
    if (on && !entry.thread) {
      // тред задачи создаётся при первом раскрытии и живёт, пока задача на странице
      const cscroll = h('div', { class: 'cscroll' });
      entry.thread = createThread({
        client: ctx.client, thread: key, drafts, draftKey: commentDraftKey(key),
        scroller: () => cscroll,
        visible: () => alive && expanded.has(key),
        alive: () => alive && entries.get(key) === entry,
        placeholder: T.dc.commentPh(key),
        hint: T.dc.commentHint,
        agentName: key,
      });
      cscroll.append(...entry.thread.list);
      entry.comments.append(cscroll, ...entry.thread.composer);
      entry.thread.setLocked(Boolean(summary && summary.readOnly));
      paintThread(entry);
    }
    if (on && entry.thread) entry.thread.shown();
  }

  function paintThread(entry) {
    if (!entry.thread || !entry.task) return;
    entry.thread.paint({ size: entry.task.thread_size, unread: entry.task.unread });
  }

  function taskEntry(key) {
    const head = h('div', { class: 'tmain' });
    const num = h('span', { class: 'num' }, key);
    const cmBtn = h('button', { class: 'btn ghost sm', type: 'button', 'aria-expanded': 'false',
      onclick: () => setExpanded(key, !expanded.has(key)) });
    const deps = h('span', { class: 'deps' });
    const comments = h('div', { class: 'comments', hidden: true });
    const node = h('article', { class: 'task' },
      h('div', { class: 'th' }, num, head,
        h('button', { class: 'btn ghost sm', type: 'button', title: T.dc.openCard, onclick: () => ctx.openCard(key) }, icon('ext'), T.dc.card)),
      h('div', { class: 'tf' }, deps, h('span', { class: 'sp' }), cmBtn),
      comments);
    const entry = { key, node, num, head, cmBtn, deps, comments, thread: null, task: null, sig: null };
    entries.set(key, entry);
    return entry;
  }

  function paintTask(entry, t, onPath) {
    entry.task = t;
    const vios = violationsOf(t);
    const est = estimateOf(t);
    const what = text(t.description);
    const dod = text(t.done_when);
    const sig = JSON.stringify([t.title, t.repo, t.kind, est, what, dod, vios, t.deps, onPath, t.thread_size, colors.get(t.repo)]);
    if (entry.sig !== sig) {
      entry.sig = sig;
      entry.node.classList.toggle('viol', vios.length > 0);
      entry.node.classList.toggle('warnb', !vios.length && !dod);
      entry.num.style.setProperty('--rc', colors.get(t.repo) || 'var(--s-todo)');
      const chips = [
        t.repo ? h('span', { class: 'chip repo' }, t.repo) : null,
        t.kind === 'research' ? h('span', { class: 'chip' }, icon('flask'), T.dc.research) : h('span', { class: 'chip' }, icon('code'), T.dc.code),
        est !== null ? h('span', { class: 'chip' }, icon('clock'), T.estimate(est)) : null,
        onPath ? h('span', { class: 'chip crit-tag' }, T.dc.onCritical) : null,
      ].filter(Boolean);
      entry.head.replaceChildren(
        h('div', { class: 'tt' }, t.title),
        h('div', { class: 'chips' }, chips),
        h('div', { class: 'tb' },
          h('div', { class: 'f what' }, h('b', null, T.dc.what), what ? h('div', { class: 'md' }, safeMarkdown(what)) : h('span', { class: 'muted' }, T.card.noDescription)),
          h('div', { class: dod ? 'f' : 'f missing' }, h('b', null, T.card.doneWhen), dod || T.dc.noDod)),
        ...vios.map(v => h('div', { class: 'vio e' }, icon('warn'),
          h('div', null, h('div', { class: 'vr' }, T.dc.rule(v.rule)), v.text ? h('div', { class: 'fix' }, v.text) : null))));
      const depKeys = t.deps || [];
      entry.deps.replaceChildren(...(depKeys.length
        ? [T.dc.after, ...depKeys.flatMap((k, i) => (i ? [', ', keyLink(k)] : [keyLink(k)]))]
        : [T.dc.noDeps]));
      const n = t.thread_size || 0;
      entry.cmBtn.replaceChildren(icon('chat'), n ? T.dc.commentsCount(n) : T.dc.comment);
    }
    paintThread(entry);
  }

  function paintTasks(s) {
    const onPath = new Set(s.critical.path);
    patchList(taskList, s.tasks, 'key',
      t => {
        const entry = taskEntry(t.key);
        paintTask(entry, t, onPath.has(t.key));
        return entry.node;
      },
      (_, t) => paintTask(entries.get(t.key), t, onPath.has(t.key)));
    for (const key of [...entries.keys()]) {
      if (!s.tasks.some(t => t.key === key)) {
        entries.delete(key);
        expanded.delete(key);
      }
    }
    if (!s.tasks.length) taskList.replaceChildren(h('p', { class: 'muted' }, T.noTasks));
  }

  // --- граф ---

  function paintGraph() {
    if (!summary) return;
    graphEmpty.hidden = summary.tasks.length > 0;
    graphHost.hidden = !summary.tasks.length;
    graph.paint(summary.tasks, {
      lanes: true,
      critical: critToggle.checked,
      nodeColor: t => colors.get(t.repo) || 'var(--s-todo)',
      nodeLabel: t => [estimateOf(t) !== null ? T.estimate(estimateOf(t)) : null, t.kind === 'research' ? T.dc.researchShort : T.dc.code]
        .filter(Boolean).join(' · '),
    });
  }

  function paintLegend() {
    legend.replaceChildren(
      ...[...colors].map(([repo, c]) => h('span', null, h('span', { class: 'sdot', style: { '--c': c } }), h('span', { class: 'mono' }, repo))),
      h('span', null, h('i', { class: 'ln' }), T.dc.criticalLegend),
      h('span', null, h('i', { class: 'ln dash' }), T.dc.violationLegend));
  }

  // --- сводка, нарушения, правила ---

  function stat(value, label, cls) {
    return h('div', { class: cls ? 'stat ' + cls : 'stat' }, h('span', { class: 'v' }, String(value)), h('span', { class: 'l' }, label));
  }

  function paintStats(s) {
    const nv = s.violations.length;
    const nd = s.missingDod.length;
    stats.replaceChildren(
      stat(s.tasks.length, T.dc.statTasks(s.tasks.length, s.repos.length)),
      stat(T.days(s.estimate), T.dc.statEstimate),
      stat(T.days(s.critical.total), T.dc.statCritical),
      stat(s.steps, T.dc.statSteps(s.steps, s.parallel)),
      stat(nv, T.dc.statViolations(nv), nv ? 'bad' : ''),
      stat(nd, T.dc.statNoDod, nd ? 'warn' : ''),
      stat(s.messages, T.dc.statMessages(s.messages)));
  }

  function paintViolations(s) {
    vsum.hidden = !s.violations.length;
    vsumCount.textContent = T.dc.violationsTitle + ' · ' + s.violations.length;
    vsumList.replaceChildren(...s.violations.map(v => h('div', { class: 'rule bad' },
      h('span', { class: 'ic' }, icon('x')),
      h('span', null, h('b', null, v.rule), v.text ? h('span', { class: 'muted' }, ' — ' + v.text) : null),
      h('span', { class: 'who' }, keyLink(v.key)))));
  }

  function paintRules(cfg) {
    const rules = rulesText(cfg);
    rulesBody.replaceChildren(rules ? h('div', { class: 'md' }, safeMarkdown(rules)) : h('p', { class: 'muted' }, T.dc.noRules));
  }

  function paintSources(cfg) {
    sources.replaceChildren(...(cfg.links || []).map(link => {
      const external = /^https?:\/\//i.test(link.href);
      const href = external ? link.href : '/api/p/' + encodeURIComponent(slug) + '/file?path=' + encodeURIComponent(link.href);
      return h('a', { href, target: '_blank', rel: 'noopener noreferrer', title: link.href }, icon('doc'), link.title);
    }));
    sources.hidden = !(cfg.links || []).length;
  }

  // --- утверждение ---

  function paintApproval(snap, s) {
    const stageLabel = T.dc.stageName[snap.config.stage] || snap.config.stage;
    lead.textContent = s.readOnly ? T.dc.leadDone : T.dc.lead(s.tasks.length, s.next);
    sub.textContent = s.readOnly ? T.dc.subDone : T.dc.sub;
    doneBanner.hidden = !s.readOnly;
    if (s.readOnly) {
      doneBanner.replaceChildren(
        h('b', null, icon('check'), T.dc.approved),
        h('span', null, T.dc.readOnly(stageLabel)),
        h('a', { href: boardHref(slug) }, T.dc.toBoard));
    }
    approveBtn.disabled = s.readOnly || approved;
    approveBtn.replaceChildren(icon('check'), s.readOnly ? T.dc.approvedShort : T.dc.approve);
    const pa = agentStateLine(snap.project_agent);
    const nv = s.violations.length;
    const nd = s.missingDod.length;
    footState.replaceChildren(...[
      nv ? h('span', { class: 'chip err' }, icon('warn'), T.dc.statViolations(nv) + ': ' + nv) : null,
      nd ? h('span', { class: 'chip q' }, T.dc.statNoDod + ': ' + nd) : null,
      h('span', { class: 'muted pastate' }, h('span', { class: 'sdot ' + pa.cls }), T.project.agent + ' · ' + pa.text),
    ].filter(Boolean));
    general.setLocked(s.readOnly);
    for (const entry of entries.values()) if (entry.thread) entry.thread.setLocked(s.readOnly);
  }

  let approving = false;
  // утверждено этой страницей: кнопка гаснет сразу, не дожидаясь снимка с новой стадией
  let approved = false;
  function confirmApprove() {
    if (!summary || summary.readOnly || approving || approved) return;
    const s = summary;
    const error = h('div', { class: 'ferr', role: 'alert', hidden: true });
    const okBtn = h('button', { class: 'btn primary', type: 'button', onclick: approve },
      s.violations.length ? T.dc.approveAnyway : T.dc.approveOk);
    const backBtn = h('button', { class: 'btn', type: 'button', onclick: () => close() }, T.dc.back);
    const { close } = openModal(T.dc.confirmTitle, [
      h('p', { class: 'mtext' }, s.next === 'tracker' ? T.dc.confirmTracker(s.tasks.length) : T.dc.confirmWork(s.tasks.length),
        ...(s.next === 'work' && s.startKeys.length ? [' ', T.dc.startsFirst, ...s.startKeys.flatMap((k, i) => (i ? [', ', h('span', { class: 'mono' }, k)] : [h('span', { class: 'mono' }, k)]))] : [])),
      s.violations.length ? h('div', { class: 'vlist' },
        h('div', { class: 'qh' }, icon('warn'), T.dc.unresolved(s.violations.length)),
        s.violations.map(v => h('div', null, h('span', { class: 'mono' }, v.key), ' — ' + v.rule))) : null,
      error,
    ], [backBtn, okBtn]);
    okBtn.focus();

    function approve() {
      if (approving) return;
      approving = true;
      okBtn.disabled = true;
      error.hidden = true;
      ctx.client.post('/stage', { stage: s.next, actor: 'user' })
        .then(() => {
          approved = true;
          approveBtn.disabled = true;
          close();
          toast(T.dc.approvedToast(T.dc.stageName[s.next]));
          scroll.scrollTo({ top: 0, behavior: 'smooth' });
        })
        .catch(err => {
          okBtn.disabled = false;
          error.textContent = (err && err.message) || String(err);
          error.hidden = false;
        })
        .finally(() => { approving = false; });
    }
  }

  // --- отрисовка ---

  function paintConnection(state) {
    const ind = connectionIndicator(state);
    syncText.textContent = ind.text;
    sync.classList.toggle('err', ind.failed);
    sync.classList.toggle('busy', ind.busy);
    banner.hidden = !ind.failed;
    banner.replaceChildren(...(ind.failed ? [ctx.errorNotice(state.error)] : []));
  }

  let painted = false;
  function paint(state, changes) {
    paintConnection(state);
    const snap = state.snapshot;
    if (!snap || (painted && changes && !changes.snapshot)) return;
    painted = true;
    const cfg = snap.config;
    document.title = cfg.title + ' — ' + T.dc.heading;
    title.textContent = cfg.title;
    paintStepper(stepper, cfg);
    paintStageAction(stageGo, ctx, snap);
    summary = decompositionSummary(snap);
    colors = repoColors((cfg.repos || []).map(r => r.name), summary.tasks);
    paintSources(cfg);
    paintStats(summary);
    paintViolations(summary);
    paintLegend();
    paintGraph();
    paintTasks(summary);
    paintRules(cfg);
    const byKey = new Map(snap.tasks.map(t => [t.key, t]));
    const open = summary.readOnly ? [] : (snap.proposals || []).filter(p => p.status === 'open');
    patchList(proposals, open, p => proposalSignature(p, byKey), p => proposalCard(ctx, p, byKey));
    general.paint({ size: snap.project_thread_size, unread: snap.project_unread });
    paintApproval(snap, summary);
    ctx.applyFocus();
  }

  // переход сюда к задаче (например, из «Ждут вас»): раскрыть её, когда она появится в списке
  ctx.applyFocus = () => {
    const key = ctx.pendingFocus;
    if (!key || !entries.has(key)) return;
    ctx.pendingFocus = null;
    focusTask(key);
  };

  const unsubscribe = store.subscribe(paint);
  paint(store.get(), { tasks: new Set(), snapshot: true });

  return function destroy() {
    ctx.applyFocus = () => {};
    alive = false;
    clearTimeout(highlightTimer);
    unsubscribe();
  };
}
