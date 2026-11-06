import { test } from 'node:test';
import assert from 'node:assert/strict';
import { authorOf, createDrafts, formatTime, initialTab, tabsFor } from '../../agent_board/static/js/card.js';
import { diffFiles, diffLines, reportHref } from '../../agent_board/static/js/changes.js';

function task(extra = {}) {
  return {
    key: 'PAY-1', title: 'Задача', kind: 'code', status: 'in_progress', open_questions: 0, unread: 0,
    review: null, changed_files: [], worktree: null, report: null, ...extra,
  };
}

const ids = tabs => tabs.map(t => t.id);

test('tabsFor: у кода четыре вкладки, у исследования — обзор и тред', () => {
  assert.deepEqual(ids(tabsFor(task())), ['overview', 'thread', 'review', 'changes']);
  assert.deepEqual(ids(tabsFor(task({ kind: 'research' }))), ['overview', 'thread']);
  assert.deepEqual(tabsFor(task()).map(t => t.label), ['Обзор', 'Тред', 'Ревью', 'Изменения']);
});

test('tabsFor считает вопросы, непрочитанное, раунд ревью и файлы', () => {
  // arrange
  const t = task({ open_questions: 2, unread: 3, review: { round: 2, state: 'open' }, changed_files: ['a', 'b'] });
  // act
  const tabs = tabsFor(t);
  const loaded = tabsFor(t, 5);
  // assert
  assert.deepEqual(tabs.map(x => [x.count, x.cls]), [['? 2', 'q'], ['3', 'unread'], ['р2', ''], ['2', '']]);
  assert.equal(loaded[3].count, '5', 'число файлов из загруженного диффа важнее поля карточки');
  assert.deepEqual(tabsFor(task()).map(x => x.count), ['', '', '', '']);
});

test('initialTab: открытый вопрос ведёт на обзор, иначе — последняя вкладка, если она есть у задачи', () => {
  assert.equal(initialTab(task({ open_questions: 1 }), 'changes'), 'overview');
  assert.equal(initialTab(task(), 'changes'), 'changes');
  assert.equal(initialTab(task({ kind: 'research' }), 'changes'), 'overview');
  assert.equal(initialTab(task(), null), 'overview');
  assert.equal(initialTab(task(), 'мусор'), 'overview');
});

const DIFF = [
  'diff --git a/src/app.py b/src/app.py',
  'index 1111111..2222222 100644',
  '--- a/src/app.py',
  '+++ b/src/app.py',
  '@@ -1,3 +1,3 @@ def main():',
  ' context',
  '-old',
  '--- not a header, removed line',
  '+new',
  '\\ No newline at end of file',
  'diff --git a/notes.md b/notes.md',
  'new file mode 100644',
  'index 0000000..3333333',
  '--- /dev/null',
  '+++ b/notes.md',
  '@@ -0,0 +1 @@',
  '+hello',
  'diff --git a/gone.txt b/gone.txt',
  'deleted file mode 100644',
  '--- a/gone.txt',
  '+++ /dev/null',
  '@@ -1 +0,0 @@',
  '-bye',
  '',
].join('\n');

test('diffLines размечает строки диффа по типам и не путает удалённую строку «---» с заголовком', () => {
  // act
  const lines = diffLines(DIFF);
  // assert
  assert.deepEqual(lines.slice(0, 10).map(l => l.type),
    ['file', 'meta', 'meta', 'meta', 'hunk', 'ctx', 'del', 'del', 'add', 'meta']);
  assert.equal(lines[0].path, 'src/app.py');
  assert.equal(lines[7].text, '--- not a header, removed line');
  assert.equal(lines.at(-1).type, 'del', 'перевод строки в конце не даёт пустой строки');
  assert.deepEqual(diffLines(''), []);
});

test('diffFiles собирает файлы со статусом и счётчиками, порядок — из списка изменённых файлов', () => {
  // act
  const files = diffFiles(['gone.txt', 'notes.md', 'src/app.py', 'big.bin'], DIFF);
  // assert
  assert.deepEqual(files.map(f => [f.path, f.status, f.add, f.del]), [
    ['gone.txt', 'D', 0, 1],
    ['notes.md', 'A', 1, 0],
    ['src/app.py', 'M', 1, 2],
    ['big.bin', 'M', null, null],
  ]);
  assert.deepEqual(files[1].lines.map(l => l.type), ['hunk', 'add']);
  assert.deepEqual(files[3].lines, [], 'файл за пределами обрезанного диффа — без строк');
  assert.deepEqual(diffFiles([], DIFF).map(f => f.path), ['src/app.py', 'notes.md', 'gone.txt'],
    'файлы из диффа без списка тоже показываются');
});

test('authorOf подписывает авторов сообщений', () => {
  assert.deepEqual(['user', 'agent', 'project_agent', 'system', 'tracker'].map(a => authorOf(a)), [
    { cls: 'user', label: 'Вы' },
    { cls: 'agent', label: 'Агент' },
    { cls: 'pa', label: 'Агент проекта' },
    { cls: 'sys', label: 'Система' },
    { cls: 'sys', label: 'Система' },
  ]);
});

test('formatTime: сегодня — только время, иначе дата и время', () => {
  const now = new Date(2026, 10, 5, 18, 0);
  assert.equal(formatTime(new Date(2026, 10, 5, 9, 7).toISOString(), now), '09:07');
  assert.equal(formatTime(new Date(2026, 10, 3, 14, 2).toISOString(), now), '3 нояб 14:02');
  assert.equal(formatTime('', now), '');
  assert.equal(formatTime('не дата', now), '');
});

test('createDrafts хранит черновик по задаче в памяти и в хранилище, переживая его недоступность', () => {
  // arrange
  const data = new Map();
  const storage = { getItem: k => (data.has(k) ? data.get(k) : null), setItem: (k, v) => data.set(k, v), removeItem: k => data.delete(k) };
  const broken = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); }, removeItem() { throw new Error('denied'); } };
  // act
  const drafts = createDrafts(storage, 'pay');
  drafts.set('PAY-1', 'черновик');
  const reloaded = createDrafts(storage, 'pay');
  const offline = createDrafts(broken, 'pay');
  offline.set('PAY-1', 'в памяти');
  // assert
  assert.equal(drafts.get('PAY-1'), 'черновик');
  assert.equal(reloaded.get('PAY-1'), 'черновик', 'после перезагрузки страницы черновик берётся из хранилища');
  assert.equal(reloaded.get('PAY-2'), '');
  assert.equal(offline.get('PAY-1'), 'в памяти');
  drafts.set('PAY-1', '');
  assert.equal(createDrafts(storage, 'pay').get('PAY-1'), '', 'пустой черновик удаляется');
});

test('reportHref ведёт отчёт через file, относительный путь — от worktree задачи', () => {
  assert.equal(reportHref('pay', task({ report: 'reports/PAY-1.md', worktree: '/r/.agent-board-worktrees/PAY-1' })),
    '/api/p/pay/file?path=%2Fr%2F.agent-board-worktrees%2FPAY-1%2Freports%2FPAY-1.md');
  assert.equal(reportHref('pay', task({ report: '/abs/r.md', worktree: '/wt' })), '/api/p/pay/file?path=%2Fabs%2Fr.md');
  assert.equal(reportHref('pay', task({ report: 'r.md' })), '/api/p/pay/file?path=r.md');
  assert.equal(reportHref('pay', task({ report: 'https://wiki/x' })), 'https://wiki/x');
  assert.equal(reportHref('pay', task()), null);
});
