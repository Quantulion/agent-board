import { test } from 'node:test';
import assert from 'node:assert/strict';
import { installFakeDocument } from './fake-dom.mjs';
import { esc, h, markdownTokens, safeMarkdown, patchList } from '../../agent_board/static/js/dom.js';

installFakeDocument();

test('esc экранирует спецсимволы html', () => {
  assert.equal(esc(`<a href="x">&'`), '&lt;a href=&quot;x&quot;&gt;&amp;&#39;');
  assert.equal(esc(null), '');
  assert.equal(esc(5), '5');
});

test('h добавляет строки только текстовыми узлами', () => {
  // arrange
  const onClick = () => {};
  // act
  const el = h('div', { class: 'card', 'data-key': 'T-1', onclick: onClick }, '<b>x</b>', 5, null, false, [h('span', null, 'y')]);
  // assert
  assert.equal(el.tagName, 'DIV');
  assert.equal(el.getAttribute('class'), 'card');
  assert.equal(el.getAttribute('data-key'), 'T-1');
  assert.deepEqual(el.listeners.click, [onClick]);
  assert.equal(el.getAttribute('onclick'), null);
  assert.deepEqual(el.childNodes.map(n => n.nodeType), [3, 3, 1]);
  assert.equal(el.textContent, '<b>x</b>5y');
});

test('h не ставит ссылки с опасной схемой', () => {
  // act
  const js = h('a', { href: 'javascript:alert(1)' });
  const data = h('a', { href: ' DATA:text/html,<script>' });
  const ok = h('a', { href: 'https://example.com/a' });
  const rel = h('a', { href: '#/p/pay' });
  // assert
  assert.equal(js.getAttribute('href'), null);
  assert.equal(data.getAttribute('href'), null);
  assert.equal(ok.getAttribute('href'), 'https://example.com/a');
  assert.equal(rel.getAttribute('href'), '#/p/pay');
});

test('h задаёт стиль через свойства, а не атрибут', () => {
  // act
  const el = h('span', { style: { '--c': 'var(--s-done)' } });
  // assert
  assert.equal(el.getAttribute('style'), null);
  assert.equal(el.style.props['--c'], 'var(--s-done)');
});

test('markdownTokens: абзацы и переносы строк', () => {
  // act
  const tokens = markdownTokens('один\nдва\n\n\nтри');
  // assert
  assert.deepEqual(tokens, [
    { type: 'p', inlines: [{ type: 'text', text: 'один' }, { type: 'br' }, { type: 'text', text: 'два' }] },
    { type: 'p', inlines: [{ type: 'text', text: 'три' }] },
  ]);
});

test('markdownTokens: блок кода остаётся текстом, даже с <script>', () => {
  // act
  const tokens = markdownTokens('до\n```html\n<script>alert(1)</script>\n\nещё\n```\nпосле');
  // assert
  assert.deepEqual(tokens, [
    { type: 'p', inlines: [{ type: 'text', text: 'до' }] },
    { type: 'code', text: '<script>alert(1)</script>\n\nещё' },
    { type: 'p', inlines: [{ type: 'text', text: 'после' }] },
  ]);
});

test('markdownTokens: незакрытый блок кода идёт до конца текста', () => {
  assert.deepEqual(markdownTokens('```\na\nb'), [{ type: 'code', text: 'a\nb' }]);
});

test('markdownTokens: инлайн-код и ссылки http(s)', () => {
  // act
  const tokens = markdownTokens('см. `x <y>` и https://ex.com/a?b=1. (HTTP://ex.org)');
  // assert
  assert.deepEqual(tokens[0].inlines, [
    { type: 'text', text: 'см. ' },
    { type: 'code', text: 'x <y>' },
    { type: 'text', text: ' и ' },
    { type: 'link', href: 'https://ex.com/a?b=1', text: 'https://ex.com/a?b=1' },
    { type: 'text', text: '. (' },
    { type: 'link', href: 'HTTP://ex.org', text: 'HTTP://ex.org' },
    { type: 'text', text: ')' },
  ]);
});

test('markdownTokens: javascript: и data: ссылки остаются текстом', () => {
  // act
  const text = 'javascript:alert(1) [x](javascript:alert(1)) data:text/html,<b>1</b> <a href="javascript:x">y</a>';
  const tokens = markdownTokens(text);
  // assert
  assert.deepEqual(tokens, [{ type: 'p', inlines: [{ type: 'text', text }] }]);
});

test('markdownTokens: ссылка внутри кода не становится ссылкой', () => {
  assert.deepEqual(markdownTokens('`https://ex.com`')[0].inlines, [{ type: 'code', text: 'https://ex.com' }]);
});

test('safeMarkdown: <img onerror> выводится текстом', () => {
  // act
  const frag = safeMarkdown('<img src=x onerror="window.pwned=1">');
  // assert
  assert.equal(frag.querySelectorAll('img').length, 0);
  assert.equal(frag.textContent, '<img src=x onerror="window.pwned=1">');
  assert.deepEqual(frag.querySelectorAll('*').map(e => e.tagName), ['P']);
});

test('safeMarkdown: ссылки безопасны, код в pre', () => {
  // act
  const frag = safeMarkdown('см https://ex.com\n\n```\n<script>x</script>\n```\n`a`');
  // assert
  const a = frag.querySelectorAll('a')[0];
  assert.equal(a.getAttribute('href'), 'https://ex.com');
  assert.equal(a.getAttribute('rel'), 'noopener noreferrer');
  assert.equal(a.getAttribute('target'), '_blank');
  assert.equal(frag.querySelectorAll('script').length, 0);
  assert.equal(frag.querySelectorAll('pre')[0].textContent, '<script>x</script>');
  assert.deepEqual(frag.querySelectorAll('*').map(e => e.tagName), ['P', 'A', 'PRE', 'CODE', 'P', 'CODE']);
});

test('patchList не пересоздаёт неизменные узлы и сохраняет порядок', () => {
  // arrange
  const container = document.createElement('div');
  const rendered = [];
  const updated = [];
  const render = item => { rendered.push(item.key); return h('div', null, item.title); };
  const update = (node, item) => { updated.push(item.key); };
  patchList(container, [{ key: 'a', title: 'A' }, { key: 'b', title: 'B' }, { key: 'c', title: 'C' }], 'key', render, update);
  const [nodeA, , nodeC] = container.children;
  // act
  patchList(container, [{ key: 'c', title: 'C' }, { key: 'a', title: 'A2' }, { key: 'd', title: 'D' }], 'key', render, update);
  // assert
  assert.deepEqual(rendered, ['a', 'b', 'c', 'd']);
  assert.deepEqual(updated, ['c', 'a']);
  assert.deepEqual(container.children.map(n => n.getAttribute('data-key')), ['c', 'a', 'd']);
  assert.equal(container.children[0], nodeC);
  assert.equal(container.children[1], nodeA);
});

test('patchList принимает функцию ключа и очищает список', () => {
  // arrange
  const container = document.createElement('div');
  patchList(container, ['x', 'y'], s => s, s => h('i', null, s));
  // act
  patchList(container, [], s => s, s => h('i', null, s));
  // assert
  assert.equal(container.children.length, 0);
});

test('patchList убирает узлы без ключа (скелет, «Загрузка…»)', () => {
  // arrange
  const container = document.createElement('div');
  container.append(h('span', null, 'Загрузка…'));
  // act
  patchList(container, ['x'], s => s, s => h('i', null, s));
  // assert
  assert.deepEqual(container.children.map(n => n.getAttribute('data-key')), ['x']);
});
