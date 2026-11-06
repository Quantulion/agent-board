// Карточка задачи: выезжающая панель поверх доски с вкладками «Обзор / Тред / Ревью / Изменения».
// Панель и вкладки создаются один раз на открытую карточку; живые обновления правят их на месте.
import { actionsSignature, cardActions, registerCardAction } from './actions.js';
import { spill, statusOf } from './chips.js';
import { h, icon, patchList, safeMarkdown, safeUrl } from './dom.js';
import { T, statusInfo } from './i18n.js';
import { externalBlockerDialog, requestMove, statusMenu } from './status.js';
import { proposalNote, proposalSignature, proposalsTouching } from './project.js';
import { createThread, draftsFor, formatTime } from './thread.js';
import { toast } from './ui.js';

export { authorOf, createDrafts, formatTime } from './thread.js';

const WIDTH_KEY = 'ab-dw';
const TAB_KEY = 'ab-card-tab';
const MIN_WIDTH = 420;

const isResearch = t => t.kind === 'research';

registerCardAction({
  id: 'close-research',
  label: T.card.closeResearch,
  icon: 'check',
  visible: isResearch,
  run: (ctx, task) => requestMove(ctx, task, 'done'),
});

// --- Чистые функции ---

export function tabsFor(task, filesCount) {
  const files = filesCount ?? (task.changed_files || []).length;
  const round = task.review && task.review.round;
  const tabs = [
    { id: 'overview', label: T.card.tabs.overview, count: task.open_questions > 0 ? '? ' + task.open_questions : '', cls: 'q' },
    { id: 'thread', label: T.card.tabs.thread, count: task.unread > 0 ? String(task.unread) : '', cls: 'unread' },
    { id: 'review', label: T.card.tabs.review, count: round ? 'р' + round : '', cls: '' },
    { id: 'changes', label: T.card.tabs.changes, count: files ? String(files) : '', cls: '' },
  ];
  return isResearch(task) ? tabs.slice(0, 2) : tabs;
}

// Открытый вопрос важнее всего; иначе — вкладка, на которой пользователь был в прошлый раз.
export function initialTab(task, remembered) {
  if (task.open_questions > 0) return 'overview';
  return tabsFor(task).some(t => t.id === remembered) ? remembered : 'overview';
}

const FILE_HEADER = /^(index |new file mode|deleted file mode|old mode|new mode|similarity |rename |copy |Binary files|--- |\+\+\+ )/;

function gitPath(raw) {
  let p = raw.trim();
  if (p.startsWith('"') && p.endsWith('"')) p = p.slice(1, -1);
  return p.replace(/^[ab]\//, '');
}

// Строки unified diff с типом: file, meta, hunk, add, del, ctx. Заголовки файла узнаются только до первого @@.
export function diffLines(text) {
  const raw = String(text || '').split('\n');
  if (raw.length && raw[raw.length - 1] === '') raw.pop();
  const out = [];
  let inHunk = false;
  for (const line of raw) {
    if (line.startsWith('diff --git ')) {
      inHunk = false;
      const m = line.match(/ b\/(.+)$/);
      out.push({ type: 'file', text: line, path: m ? gitPath(m[1]) : '' });
    } else if (line.startsWith('@@')) {
      inHunk = true;
      out.push({ type: 'hunk', text: line });
    } else if (!inHunk) {
      out.push({ type: 'meta', text: line });
    } else if (line.startsWith('+')) {
      out.push({ type: 'add', text: line });
    } else if (line.startsWith('-')) {
      out.push({ type: 'del', text: line });
    } else if (line.startsWith('\\')) {
      out.push({ type: 'meta', text: line });
    } else {
      out.push({ type: 'ctx', text: line });
    }
  }
  return out;
}

// Файлы диффа со статусом (A/M/D), числом добавленных и удалённых строк и строками для показа.
// Порядок — из списка изменённых файлов; файл вне (обрезанного) диффа остаётся без строк и счётчиков.
export function diffFiles(paths, text) {
  const parsed = new Map();
  let cur = null;
  for (const line of diffLines(text)) {
    if (line.type === 'file') {
      cur = { path: line.path, status: 'M', add: 0, del: 0, lines: [] };
      parsed.set(cur.path, cur);
      continue;
    }
    if (!cur) continue;
    if (line.type === 'meta' && !cur.lines.length) {
      if (line.text.startsWith('new file mode')) cur.status = 'A';
      else if (line.text.startsWith('deleted file mode')) cur.status = 'D';
      else if (line.text.startsWith('+++ ') && !line.text.startsWith('+++ /dev/null')) {
        const p = gitPath(line.text.slice(4));
        if (p !== cur.path) {
          parsed.delete(cur.path);
          cur.path = p;
          parsed.set(p, cur);
        }
      }
      if (FILE_HEADER.test(line.text) && !line.text.startsWith('Binary files')) continue;
    }
    if (line.type === 'add') cur.add++;
    if (line.type === 'del') cur.del++;
    cur.lines.push(line);
  }
  const order = [...(paths || []), ...[...parsed.keys()].filter(p => !(paths || []).includes(p))];
  return order.map(p => parsed.get(p) || { path: p, status: 'M', add: null, del: null, lines: [] });
}

// Ссылка на отчёт исследования: внешняя — как есть, локальный файл — через file; относительный путь — от worktree.
export function reportHref(slug, task) {
  const report = typeof task.report === 'string' ? task.report.trim() : '';
  if (!report) return null;
  if (/^https?:\/\//i.test(report)) return report;
  const path = report.startsWith('/') || !task.worktree ? report : task.worktree.replace(/\/+$/, '') + '/' + report;
  return '/api/p/' + encodeURIComponent(slug) + '/file?path=' + encodeURIComponent(path);
}

// --- Мелкие помощники DOM ---

function storageGet(store, key) {
  try {
    return globalThis[store].getItem(key);
  } catch (e) {
    return null;
  }
}

function storageSet(store, key, value) {
  try {
    globalThis[store].setItem(key, value);
  } catch (e) {
    // хранилище недоступно — настройка живёт до перезагрузки
  }
}

const md = (text, cls) => h('div', { class: cls ? 'md ' + cls : 'md' }, safeMarkdown(text));
const sec = (title, ...children) => h('section', { class: 'sec' }, h('h3', null, title), ...children);

// Регион перерисовывается, только если изменилась сигнатура его данных.
function region(tag, cls) {
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

async function copyText(text) {
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

const copyButton = text => h('button', {
  class: 'btn ghost sm icon', type: 'button', title: T.card.copy, 'aria-label': T.card.copy,
  onclick: () => copyText(text),
}, icon('copy'));

// --- Открытая карточка ---

let current = null;

export function openCard(ctx, key) {
  if (current && current.key === key && current.ctx === ctx) return;
  const returnFocus = current ? current.returnFocus : document.activeElement;
  closeCard(false);
  current = createCard(ctx, key, returnFocus);
}

export function closeCard(restoreFocus = true) {
  if (!current) return;
  const card = current;
  current = null;
  card.destroy(restoreFocus);
}

function createCard(ctx, key, returnFocus) {
  const { store, client, slug } = ctx;
  const drafts = draftsFor(slug);
  const cleanups = [];
  const self = { key, ctx, returnFocus, destroy };
  let ui = null;

  const findTask = () => {
    const snap = store.get().snapshot;
    return snap ? snap.tasks.find(t => t.key === key) || null : undefined;
  };

  function onState(state) {
    const t = findTask();
    if (t === undefined) return;
    if (t === null) {
      if (!ui) {
        toast(T.card.notFound(key), { error: true });
        ctx.closeCard({ replace: true });
      }
      return;
    }
    if (!ui) ui = build(t);
    ui.paint(t, state.snapshot);
  }

  function destroy(restoreFocus) {
    cleanups.forEach(fn => fn());
    if (ui) ui.host.remove();
    if (restoreFocus && returnFocus && returnFocus.isConnected && typeof returnFocus.focus === 'function') {
      returnFocus.focus({ preventScroll: true });
    }
  }

  cleanups.push(store.subscribe(onState));
  cleanups.push(store.onEvent(e => { if (ui) ui.onEvent(e); }));
  onState(store.get());
  return self;

  // --- построение панели ---

  function build(first) {
    const scrim = h('div', { class: 'dscrim', onclick: () => ctx.closeCard() });
    const drawer = h('div', { class: 'drawer', role: 'dialog', 'aria-label': key, 'data-key': key, tabindex: '-1' });
    const savedWidth = Number(storageGet('localStorage', WIDTH_KEY));
    if (savedWidth >= MIN_WIDTH) drawer.style.setProperty('--dw', savedWidth + 'px');
    const handle = h('div', { class: 'dhandle', title: T.card.resize });
    const head = header(first);
    const banners = region('div', 'banners');
    const tabsBar = h('div', { class: 'tabs', role: 'tablist' });
    const body = h('div', { class: 'dbody', role: 'tabpanel' });
    drawer.append(handle, head.el, banners.el, tabsBar, body);
    const host = h('div', { class: 'card-host' }, scrim, drawer);
    document.body.append(host);
    bindResize(handle, drawer);

    const panes = {};
    const builders = { overview: overviewPane, thread: threadPane, review: reviewPane, changes: changesPane };
    const scrolls = {};
    let task = first;
    let snapshot = store.get().snapshot;
    let tab = null;

    const tabButtons = new Map();
    for (const t of tabsFor(first)) {
      const count = h('span', { class: 'tb', hidden: true });
      const btn = h('button', {
        type: 'button', role: 'tab', 'data-tab': t.id, 'aria-selected': 'false', tabindex: '-1',
        onclick: () => showTab(t.id),
        onkeydown: e => {
          if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
          const ids = [...tabButtons.keys()];
          const next = ids[(ids.indexOf(t.id) + (e.key === 'ArrowRight' ? 1 : ids.length - 1)) % ids.length];
          showTab(next);
          tabButtons.get(next).btn.focus();
        },
      }, t.label, count);
      tabButtons.set(t.id, { btn, count });
      tabsBar.append(btn);
    }

    function paintTabs() {
      const files = panes.changes ? panes.changes.filesCount() : null;
      for (const t of tabsFor(task, files)) {
        const ref = tabButtons.get(t.id);
        if (!ref) continue;
        ref.count.textContent = t.count;
        ref.count.className = t.cls ? 'tb ' + t.cls : 'tb';
        ref.count.hidden = !t.count;
      }
    }

    function showTab(id) {
      if (!tabButtons.has(id)) id = 'overview';
      if (id === tab) return;
      if (tab) scrolls[tab] = body.scrollTop;
      tab = id;
      storageSet('localStorage', TAB_KEY, id);
      if (!panes[id]) {
        panes[id] = builders[id]();
        panes[id].paint(task, snapshot);
      }
      body.replaceChildren(panes[id].el);
      for (const [tid, ref] of tabButtons) {
        ref.btn.setAttribute('aria-selected', String(tid === id));
        ref.btn.tabIndex = tid === id ? 0 : -1;
      }
      body.scrollTop = scrolls[id] ?? 0;
      if (panes[id].shown) panes[id].shown();
    }

    // Esc закрывает сначала меню статуса, потом карточку; модальные окна перехватывают Esc раньше.
    const onKey = e => {
      // Esc во время набора через IME отменяет композицию, а не закрывает карточку
      if (e.key !== 'Escape' || e.defaultPrevented || e.isComposing || e.keyCode === 229) return;
      if (head.menuOpen()) {
        head.closeMenu(true);
        return;
      }
      ctx.closeCard();
    };
    document.addEventListener('keydown', onKey);
    cleanups.push(() => document.removeEventListener('keydown', onKey));
    const onDocClick = e => { if (!e.target.closest || !e.target.closest('.dh .stmenu')) head.closeMenu(false); };
    document.addEventListener('click', onDocClick);
    cleanups.push(() => document.removeEventListener('click', onDocClick));

    showTab(initialTab(first, storageGet('localStorage', TAB_KEY)));
    drawer.focus({ preventScroll: true });

    return {
      host,
      paint(t, snap) {
        task = t;
        snapshot = snap;
        head.paint(t, snap);
        banners.paint(bannerModel(t, snap), () => bannerNodes(t, snap));
        for (const p of Object.values(panes)) p.paint(t, snap);
        paintTabs();
      },
      onEvent(e) {
        for (const p of Object.values(panes)) if (p.onEvent) p.onEvent(e);
      },
    };

    // --- вкладки ---

    function isVisible(id) {
      return tab === id && document.visibilityState !== 'hidden';
    }

    function overviewPane() {
      const questions = h('div', { class: 'qlist' });
      const proposals = h('div', { class: 'qlist' });
      const parts = {
        error: region('div'), violations: region('div'), task: region('div'), summary: region('div'),
        deps: region('div'), work: region('div'), tracker: region('div'),
      };
      const el = h('div', { class: 'dgrid' },
        h('main', null, questions, proposals, parts.error.el, parts.violations.el, parts.task.el, parts.summary.el),
        h('aside', null, parts.deps.el, parts.work.el, parts.tracker.el));
      return {
        el,
        paint(t, snap) {
          const open = (t.questions || []).filter(q => !q.answer);
          patchList(questions, open, 'id', q => questionForm(q));
          questions.hidden = !open.length;
          const byKey = new Map(snap.tasks.map(x => [x.key, x]));
          const touching = proposalsTouching(snap.proposals || [], key);
          // узел пересоздаётся только при смене описания: состояние кнопок переживает прочие обновления снимка
          patchList(proposals, touching, p => proposalSignature(p, byKey), p => proposalNote(ctx, p, byKey));
          proposals.hidden = !touching.length;
          parts.error.paint(t.turn_error, () => t.turn_error ? h('section', { class: 'sec err' },
            h('h3', null, icon('warn'), T.card.turnError), h('pre', { class: 'errtext' }, t.turn_error)) : null);
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
          paintDeps(parts.deps, t, snap);
          paintWork(parts.work, t);
          paintTracker(parts.tracker, t);
        },
      };
    }

    function questionForm(q) {
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

    function paintDeps(part, t, snap) {
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
          h('button', { class: 'btn ghost sm', type: 'button', onclick: () => externalBlockerDialog(ctx, task) }, T.card.editBlocker)) : null,
        blocks.length ? [h('div', { class: 'muted sub' }, T.card.blocks), h('div', { class: 'deps' }, blocks.map(d => depRow(d, false)))] : null));
    }

    function paintWork(part, t) {
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
        const url = t.tracker_url && safeUrl(t.tracker_url);
        rows.push([T.card.trackerKey, url ? h('a', { href: url, target: '_blank', rel: 'noopener noreferrer' }, t.tracker_key) : t.tracker_key]);
      }
      if (t.session_id) rows.push([T.card.session, h('span', { class: 'mono' }, t.session_id)]);
      if (t.harness) rows.push([T.card.harness, t.harness]);
      if (t.last_turn_at) rows.push([T.card.lastTurn, formatTime(t.last_turn_at)]);
      part.paint([t.tracker_key, t.tracker_url, t.session_id, t.harness, t.last_turn_at], () => rows.length
        ? sec(T.card.tracker, h('dl', { class: 'kv' }, rows.map(([k, v]) => [h('dt', null, k), h('dd', null, v)]))) : null);
    }

    function threadPane() {
      const sessionNote = h('span', { class: 'r faint' });
      const thread = createThread({
        client, thread: key, drafts, agentName: key,
        scroller: () => body,
        visible: () => isVisible('thread'),
        alive: () => current === self,
        placeholder: T.card.composer(key),
      });
      const el = h('div', { class: 'pane' },
        h('section', { class: 'sec' }, h('h3', null, T.card.threadTitle, sessionNote), thread.list, thread.composer));
      return {
        el,
        paint(t) {
          sessionNote.textContent = t.session_id ? T.card.sessionShort(t.session_id.slice(0, 8)) : '';
          thread.paint({ size: t.thread_size, unread: t.unread });
        },
        shown: thread.shown,
      };
    }

    function reviewPane() {
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

    function changesPane() {
      const el = h('div', { class: 'pane' });
      let data = null;
      let files = [];
      let error = '';
      let loading = false;
      let again = false;
      let opened = null;

      async function load() {
        if (loading) {
          again = true;
          return;
        }
        loading = true;
        try {
          data = await client.get('/tasks/' + encodeURIComponent(key) + '/diff');
          files = diffFiles(data.files, data.diff);
          error = '';
        } catch (err) {
          error = (err && err.message) || String(err);
        } finally {
          loading = false;
        }
        if (!current || current !== self) return;
        render();
        paintTabs();
        if (again) {
          again = false;
          load();
        }
      }

      function render() {
        if (error) {
          el.replaceChildren(h('section', { class: 'sec' }, h('div', { class: 'ferr', role: 'alert' }, T.card.changesFailed + error)));
          return;
        }
        if (!data) {
          el.replaceChildren(h('section', { class: 'sec' }, h('p', { class: 'muted' }, T.card.loadingChanges)));
          return;
        }
        if (!files.length) {
          el.replaceChildren(h('section', { class: 'sec' }, h('p', { class: 'muted' }, T.card.noChanges)));
          return;
        }
        if (opened === null) opened = new Set([files[0].path]);
        const counted = files.filter(f => f.add !== null);
        const add = counted.length ? counted.reduce((s, f) => s + f.add, 0) : null;
        const del = counted.reduce((s, f) => s + (f.del || 0), 0);
        el.replaceChildren(sec([T.card.changesTitle(data.base), h('span', { class: 'r faint' }, T.card.changesStat(files.length, add, del))],
          data.truncated ? h('div', { class: 'trunc', role: 'note' }, icon('warn'), T.card.truncated) : null,
          h('div', { class: 'files' }, files.map(fileBlock))));
      }

      function fileBlock(f) {
        const isOpen = opened.has(f.path);
        const toggle = () => {
          if (opened.has(f.path)) opened.delete(f.path);
          else opened.add(f.path);
          render();
        };
        return [
          h('div', {
            class: isOpen ? 'file open' : 'file', role: 'button', tabindex: '0', 'aria-expanded': String(isOpen), 'data-path': f.path,
            onclick: toggle,
            onkeydown: e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); } },
          },
          h('span', { class: 'fs ' + f.status }, f.status),
          h('span', { class: 'fp', title: f.path }, h('bdi', null, f.path)),
          f.add === null ? h('span', { class: 'st' }) : h('span', { class: 'st' }, h('span', { class: 'a' }, '+' + f.add), ' ', h('span', { class: 'd' }, '−' + f.del))),
          isOpen ? (f.lines.length ? h('div', { class: 'diff' }, f.lines.map(l => l.type === 'hunk'
            ? h('div', { class: 'hh' }, l.text)
            : h('div', { class: l.type === 'add' ? 'l a' : l.type === 'del' ? 'l d' : 'l' }, l.text || ' ')))
            : h('p', { class: 'muted small' }, T.card.notInDiff)) : null,
        ];
      }

      render();
      return {
        el,
        paint() {},
        shown: load,
        filesCount: () => (data ? files.length : null),
        onEvent(e) {
          if ((e.type === 'turn_finished' || e.type === 'turn_failed') && e.data.key === key && isVisible('changes')) load();
        },
      };
    }
  }

  // --- шапка ---

  function header(first) {
    const keySlot = h('span', { class: 'slot' });
    const chips = h('span', { class: 'slot' });
    const statusBtn = h('button', { class: 'stbtn', type: 'button', 'aria-haspopup': 'menu', 'aria-label': T.statusMenuAria });
    const menuSlot = h('div', { class: 'slot' });
    const stmenu = h('div', { class: 'stmenu' }, statusBtn, menuSlot);
    const title = h('h2');
    const terminal = h('button', { class: 'btn', type: 'button', onclick: () => { if (lastTask.terminal_command) copyText(lastTask.terminal_command); } },
      icon('term'), T.card.terminal);
    const acts = h('span', { class: 'slot' });
    let actsSig = null;
    const el = h('div', { class: 'dh' },
      h('div', { class: 'l1' }, keySlot, chips, stmenu, h('span', { class: 'sp' }),
        h('button', { class: 'btn icon ghost', type: 'button', 'aria-label': T.close, title: T.close, onclick: () => ctx.closeCard() }, icon('x'))),
      title,
      h('div', { class: 'acts' }, acts, h('span', { class: 'grow' }), terminal));
    let lastTask = first;
    let sig = null;
    let menu = null;

    // кнопки действий пересобираются, только когда меняется их набор или состояние
    function paintActions(t, snap) {
      const list = cardActions(t, snap);
      const next = actionsSignature(list);
      if (next === actsSig) return;
      actsSig = next;
      acts.replaceChildren(...list.map(a => h('button', {
        class: 'btn', type: 'button', 'data-action': a.id, disabled: !a.enabled, title: a.title,
        onclick: () => a.run(ctx, lastTask),
      }, a.icon ? icon(a.icon) : null, a.label)));
    }

    function closeMenu(refocus) {
      if (!menu) return;
      menu = null;
      menuSlot.replaceChildren();
      statusBtn.setAttribute('aria-expanded', 'false');
      if (refocus) statusBtn.focus();
    }

    statusBtn.addEventListener('click', e => {
      e.stopPropagation();
      if (menu) {
        closeMenu(false);
        return;
      }
      menu = statusMenu(statusOf(lastTask), to => {
        closeMenu(true);
        requestMove(ctx, lastTask, to);
      });
      menu.addEventListener('focusout', ev => { if (menu && !stmenu.contains(ev.relatedTarget)) closeMenu(false); });
      menuSlot.replaceChildren(menu);
      statusBtn.setAttribute('aria-expanded', 'true');
      const cur = menu.querySelector('button.cur') || menu.querySelector('button');
      if (cur) cur.focus();
    });

    return {
      el,
      menuOpen: () => Boolean(menu),
      closeMenu,
      paint(t, snap) {
        lastTask = t;
        paintActions(t, snap);
        const next = JSON.stringify([t.title, t.tracker_url, t.repo, t.kind, t.estimate, statusOf(t), t.terminal_command]);
        if (next === sig) return;
        sig = next;
        const trackerUrl = t.tracker_url && safeUrl(t.tracker_url);
        keySlot.replaceChildren(trackerUrl
          ? h('a', { class: 'key', href: trackerUrl, target: '_blank', rel: 'noopener noreferrer', title: T.card.openTracker }, key, icon('ext'))
          : h('span', { class: 'key' }, key));
        // replaceChildren превратил бы null в текст «null»
        chips.replaceChildren(...[
          t.repo ? h('span', { class: 'chip repo' }, t.repo) : null,
          h('span', { class: 'chip' }, icon(isResearch(t) ? 'flask' : 'code'), isResearch(t) ? T.kindResearch : T.kindCode),
          t.estimate !== null && t.estimate !== undefined ? h('span', { class: 'chip' }, T.estimate(t.estimate)) : null,
        ].filter(Boolean));
        statusBtn.replaceChildren(spill(statusOf(t)), icon('chev'));
        title.textContent = t.title;
        terminal.disabled = !t.terminal_command;
        terminal.title = t.terminal_command || T.card.noTerminal;
      },
    };
  }

  // --- плашки над вкладками: очередь, причины блокировки, остановленная переписка ---

  function bannerModel(t, snap) {
    const byKey = new Map(snap.tasks.map(x => [x.key, statusOf(x)]));
    return [statusOf(t), t.locks, t.open_deps, (t.open_deps || []).map(k => byKey.get(k)), t.external_blocker, t.agent_parked];
  }

  function bannerNodes(t, snap) {
    const nodes = [];
    const byKey = new Map(snap.tasks.map(x => [x.key, x]));
    const waiting = (t.locks || []).find(l => l.position > 0);
    if (waiting) nodes.push(h('div', { class: 'banner queue' }, icon('clock'), h('span', null, T.card.queue(waiting.resource, waiting.position))));
    for (const l of (t.locks || []).filter(x => x.position === 0)) {
      nodes.push(h('div', { class: 'banner queue' }, icon('lock'), h('span', null, T.card.holds(l.resource))));
    }
    const ext = typeof t.external_blocker === 'string' ? t.external_blocker.trim() : '';
    if (statusOf(t) === 'blocked' || ext) {
      const deps = (t.open_deps || []).map(k => {
        const d = byKey.get(k);
        return d ? k + ' (' + statusInfo(statusOf(d)).label + ')' : k;
      });
      const reasons = [deps.length ? T.card.waits(deps.join(', ')) : '', ext ? T.card.external(ext) : ''].filter(Boolean);
      nodes.push(h('div', { class: 'banner blocked' }, icon('lock'),
        h('span', null, h('b', null, T.card.whyBlocked), ' ', reasons.join('; ') || T.card.depsClosed)));
    }
    if (t.agent_parked) nodes.push(h('div', { class: 'banner parked' }, icon('chat'), h('span', null, T.card.parked)));
    return nodes;
  }

  function bindResize(handle, drawer) {
    handle.addEventListener('pointerdown', e => {
      e.preventDefault();
      handle.setPointerCapture(e.pointerId);
      handle.classList.add('drag');
      document.body.classList.add('resizing');
      const move = ev => {
        const w = Math.min(window.innerWidth - 40, Math.max(MIN_WIDTH, window.innerWidth - ev.clientX));
        drawer.style.setProperty('--dw', Math.round(w) + 'px');
      };
      const up = () => {
        handle.removeEventListener('pointermove', move);
        handle.removeEventListener('pointerup', up);
        handle.removeEventListener('pointercancel', up);
        handle.classList.remove('drag');
        document.body.classList.remove('resizing');
        storageSet('localStorage', WIDTH_KEY, String(Math.round(drawer.getBoundingClientRect().width)));
      };
      handle.addEventListener('pointermove', move);
      handle.addEventListener('pointerup', up);
      handle.addEventListener('pointercancel', up);
    });
  }
}
