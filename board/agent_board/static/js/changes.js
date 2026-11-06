// Вкладка «Изменения»: разбор диффа задачи по файлам и ссылка на отчёт исследования.
import { h, icon } from './dom.js';
import { T } from './i18n.js';
import { sec } from './pane-kit.js';

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

export function changesPane(pc) {
  const { key, client } = pc;
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
    if (!pc.alive()) return;
    render();
    pc.paintTabs();
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
      if ((e.type === 'turn_finished' || e.type === 'turn_failed') && e.data.key === key && pc.isVisible('changes')) load();
    },
  };
}
