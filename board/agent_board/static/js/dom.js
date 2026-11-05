// Безопасный вывод: любой текст попадает в DOM только текстовыми узлами.

const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

export function esc(s) {
  return s === null || s === undefined ? '' : String(s).replace(/[&<>"']/g, c => ESC[c]);
}

const URL_ATTRS = new Set(['href', 'src', 'action', 'xlink:href']);
const FORBIDDEN_ATTRS = new Set(['srcdoc', 'formaction']);

// Ссылка допустима, только если браузер разберёт её как http(s): схема проверяется тем же разбором URL,
// что и у браузера (он выбрасывает табуляции и переводы строк внутри и управляющие символы по краям).
export function safeUrl(url) {
  const value = String(url).trim();
  const base = (globalThis.location && globalThis.location.href) || 'http://localhost/';
  try {
    const protocol = new URL(value, base).protocol;
    return protocol === 'http:' || protocol === 'https:' ? value : null;
  } catch (e) {
    return null;
  }
}

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [rawName, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    const name = rawName.toLowerCase();
    if (name.startsWith('on')) {
      if (typeof value === 'function') el.addEventListener(name.slice(2), value);
      continue;
    }
    if (FORBIDDEN_ATTRS.has(name)) continue;
    if (name === 'style') {
      // атрибут style запрещён CSP, свойства через CSSOM — нет
      for (const [prop, v] of Object.entries(value)) el.style.setProperty(prop, v);
      continue;
    }
    if (URL_ATTRS.has(name)) {
      const url = safeUrl(value);
      if (url !== null) el.setAttribute(name, url);
      continue;
    }
    el.setAttribute(rawName, value === true ? '' : String(value));
  }
  appendChildren(el, children);
  return el;
}

function appendChildren(el, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    if (Array.isArray(child)) appendChildren(el, child);
    else if (typeof child === 'object') el.appendChild(child);
    else el.appendChild(document.createTextNode(String(child)));
  }
}

const FENCE = /^\s*```/;
const INLINE = /`([^`\n]+)`|(https?:\/\/[^\s<>"'`]+)/gi;
const TRAILING = /[.,;:!?)\]}»"']/;

// Разбор минимального markdown: абзацы, переносы строк, `код`, блоки ``` и ссылки http(s).
export function markdownTokens(text) {
  const lines = String(text ?? '').replace(/\r\n?/g, '\n').split('\n');
  const blocks = [];
  let para = [];
  const flush = () => {
    if (para.length) blocks.push({ type: 'p', inlines: paragraphInlines(para) });
    para = [];
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (FENCE.test(line)) {
      flush();
      const code = [];
      for (i++; i < lines.length && !FENCE.test(lines[i]); i++) code.push(lines[i]);
      blocks.push({ type: 'code', text: code.join('\n') });
    } else if (line.trim() === '') {
      flush();
    } else {
      para.push(line);
    }
  }
  flush();
  return blocks;
}

function paragraphInlines(lines) {
  const out = [];
  lines.forEach((line, i) => {
    if (i > 0) out.push({ type: 'br' });
    out.push(...lineInlines(line));
  });
  return out;
}

function lineInlines(line) {
  const out = [];
  const text = s => { if (s) out.push({ type: 'text', text: s }); };
  let pos = 0;
  for (const m of line.matchAll(INLINE)) {
    text(line.slice(pos, m.index));
    pos = m.index + m[0].length;
    if (m[1] !== undefined) {
      out.push({ type: 'code', text: m[1] });
      continue;
    }
    const href = trimUrl(m[2]);
    out.push({ type: 'link', href, text: href });
    pos -= m[2].length - href.length;
  }
  text(line.slice(pos));
  // соседние текстовые куски склеиваются, чтобы не дробить вывод
  return out.reduce((acc, t) => {
    const prev = acc[acc.length - 1];
    if (t.type === 'text' && prev && prev.type === 'text') prev.text += t.text;
    else acc.push(t);
    return acc;
  }, []);
}

// Пунктуация в конце ссылки — часть предложения; закрывающая скобка остаётся, если в ссылке есть парная.
function trimUrl(url) {
  let end = url.length;
  while (end > 0 && TRAILING.test(url[end - 1])) {
    if (url[end - 1] === ')') {
      const head = url.slice(0, end);
      if ((head.match(/\(/g) || []).length >= (head.match(/\)/g) || []).length) break;
    }
    end--;
  }
  return url.slice(0, end);
}

export function safeMarkdown(text) {
  const frag = document.createDocumentFragment();
  for (const block of markdownTokens(text)) {
    if (block.type === 'code') {
      frag.appendChild(h('pre', null, h('code', null, block.text)));
      continue;
    }
    frag.appendChild(h('p', null, block.inlines.map(t => {
      if (t.type === 'br') return h('br');
      if (t.type === 'code') return h('code', null, t.text);
      if (t.type === 'link') return h('a', { href: t.href, rel: 'noopener noreferrer', target: '_blank' }, t.text);
      return t.text;
    })));
  }
  return frag;
}

// Перерисовка списка по ключам: существующие узлы переиспользуются и только переставляются.
export function patchList(container, items, key, render, update) {
  const keyOf = typeof key === 'function' ? key : item => item[key];
  const existing = new Map();
  for (const node of [...container.children]) {
    const k = node.getAttribute('data-key');
    if (k !== null) existing.set(k, node);
    else node.remove();
  }
  const wanted = items.map(item => {
    const k = String(keyOf(item));
    let node = existing.get(k);
    if (node) {
      existing.delete(k);
      if (update) update(node, item);
    } else {
      node = render(item);
      node.setAttribute('data-key', k);
    }
    return node;
  });
  for (const node of existing.values()) node.remove();
  wanted.forEach((node, i) => {
    const current = container.children[i] || null;
    if (current !== node) container.insertBefore(node, current);
  });
}

const ICONS = {
  logo: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="M5 5v14M12 5v9M19 5v5"/></svg>',
  sun: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
  moon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>',
  auto: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor"/></svg>',
  board: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="5" height="16" rx="1.5"/><rect x="10" y="4" width="5" height="10" rx="1.5"/><rect x="17" y="4" width="4" height="13" rx="1.5"/></svg>',
  graph: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="5" cy="6" r="2.5"/><circle cx="5" cy="18" r="2.5"/><circle cx="19" cy="12" r="2.5"/><path d="M7.5 6.5c5 0 5 5 9 5.5M7.5 17.5c5 0 5-5 9-5.5"/></svg>',
  warn: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18v.5"/></svg>',
  plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
  x: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  chat: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M4 5h16v11H9l-5 4z"/></svg>',
  clock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
  lock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>',
  check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
  search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/></svg>',
  code: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 7l-5 5 5 5M16 7l5 5-5 5"/></svg>',
  flask: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 3h6M10 3v6l-5.5 9.5A1.6 1.6 0 0 0 6 21h12a1.6 1.6 0 0 0 1.5-2.5L14 9V3"/><path d="M7.5 15h9"/></svg>',
  spark: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l2.2 6.3L20 10l-5.8 1.7L12 18l-2.2-6.3L4 10l5.8-1.7z"/></svg>',
  send: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12l16-8-6 16-2.5-6.5z"/></svg>',
  branch: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="8" r="2"/><path d="M6 7v10M18 10c0 5-12 3-12 7"/></svg>',
  doc: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4M9 12h6M9 16h6"/></svg>',
  chev: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>',
  eye: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>',
  link: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/></svg>',
};

// Иконки — статичная разметка без данных.
export function icon(name) {
  const tpl = document.createElement('template');
  tpl.innerHTML = ICONS[name] || '';
  return tpl.content.firstChild || document.createTextNode('');
}
