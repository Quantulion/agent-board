// Граф зависимостей: раскладка по слоям (чистая функция) и SVG-схема, которая правится на месте при обновлениях.
import { h } from './dom.js';
import { T, statusInfo } from './i18n.js';

// Узел фиксированного размера: текст в нём обрезается стилями, а не раздвигает схему.
export const NODE_W = 220;
export const NODE_H = 72;
const COL_GAP = 60;
const ROW_GAP = 14;
const PAD = 18;
const SWEEPS = 4;

const KEYS = new Intl.Collator('ru', { numeric: true });
const statusOf = t => t.display_status || t.status;
// задача без оценки весит один день
const weight = t => (typeof t.estimate === 'number' && Number.isFinite(t.estimate) && t.estimate >= 0 ? t.estimate : 1);

// Компоненты сильной связности (Тарьян): всё, что в одном цикле, — одна компонента.
function components(keys, preds) {
  const index = new Map();
  const low = new Map();
  const comp = new Map();
  const stack = [];
  const onStack = new Set();
  let counter = 0;
  let next = 0;
  const visit = k => {
    index.set(k, counter);
    low.set(k, counter);
    counter++;
    stack.push(k);
    onStack.add(k);
    for (const p of preds.get(k)) {
      if (!index.has(p)) {
        visit(p);
        low.set(k, Math.min(low.get(k), low.get(p)));
      } else if (onStack.has(p)) {
        low.set(k, Math.min(low.get(k), index.get(p)));
      }
    }
    if (low.get(k) === index.get(k)) {
      for (;;) {
        const x = stack.pop();
        onStack.delete(x);
        comp.set(x, next);
        if (x === k) break;
      }
      next++;
    }
  };
  for (const k of keys) if (!index.has(k)) visit(k);
  return comp;
}

// Общая часть раскладки и критического пути: ключи по порядку, связи без петель внутри циклов, глубина.
function structure(tasks) {
  const map = new Map(tasks.map(t => [t.key, t]));
  const keys = [...map.keys()].sort(KEYS.compare);
  const preds = new Map(keys.map(k => [k, [...new Set(map.get(k).deps || [])].filter(d => map.has(d)).sort(KEYS.compare)]));
  const comp = components(keys, preds);
  const size = new Map();
  for (const c of comp.values()) size.set(c, (size.get(c) || 0) + 1);
  const cyclic = new Set(keys.filter(k => size.get(comp.get(k)) > 1 || preds.get(k).includes(k)));
  // связи внутри цикла не задают порядок: иначе глубина была бы бесконечной
  const dagPreds = new Map(keys.map(k => [k, preds.get(k).filter(p => comp.get(p) !== comp.get(k))]));
  const compDepth = new Map();
  const members = new Map();
  for (const k of keys) members.set(comp.get(k), [...(members.get(comp.get(k)) || []), k]);
  const depthOf = c => {
    if (compDepth.has(c)) return compDepth.get(c);
    let d = 0;
    for (const k of members.get(c)) for (const p of dagPreds.get(k)) d = Math.max(d, depthOf(comp.get(p)) + 1);
    compDepth.set(c, d);
    return d;
  };
  const depth = new Map(keys.map(k => [k, depthOf(comp.get(k))]));
  return { map, keys, preds, dagPreds, cyclic, depth };
}

function longestPath(s, skip) {
  const included = k => !skip.has(statusOf(s.map.get(k)));
  const order = [...s.keys].sort((a, b) => s.depth.get(a) - s.depth.get(b) || KEYS.compare(a, b));
  const fin = new Map();
  const prev = new Map();
  for (const k of order) {
    if (!included(k)) continue;
    let best = 0;
    let from = null;
    for (const p of s.dagPreds.get(k)) {
      if (fin.has(p) && fin.get(p) > best) {
        best = fin.get(p);
        from = p;
      }
    }
    fin.set(k, best + weight(s.map.get(k)));
    prev.set(k, from);
  }
  let end = null;
  for (const k of s.keys) if (fin.has(k) && (end === null || fin.get(k) > fin.get(end))) end = k;
  const path = [];
  for (let k = end; k !== null; k = prev.get(k)) path.unshift(k);
  return { path, total: end === null ? 0 : fin.get(end), prev };
}

// Самая длинная по оценкам цепочка зависимостей среди задач, статус которых не входит в skip.
export function criticalPath(tasks, skip = ['cancelled']) {
  const { path, total } = longestPath(structure(tasks), new Set(skip));
  return { path, total };
}

function crossingCount(layers, pos, dagPreds) {
  let n = 0;
  for (let i = 1; i < layers.length; i++) {
    const edges = [];
    for (const k of layers[i]) for (const p of dagPreds.get(k)) if (pos.get(p).layer === i - 1) edges.push([pos.get(p).order, pos.get(k).order]);
    for (let a = 0; a < edges.length; a++) {
      for (let b = a + 1; b < edges.length; b++) {
        if ((edges[a][0] - edges[b][0]) * (edges[a][1] - edges[b][1]) < 0) n++;
      }
    }
  }
  return n;
}

// Узлы — по слоям глубины зависимостей слева направо; порядок в слое — по барицентру соседей,
// чтобы рёбра меньше пересекались. Узлы циклов помечаются cyclic, раскладка при этом не ломается.
export function layoutGraph(tasks, options = {}) {
  const s = structure(tasks || []);
  const succs = new Map(s.keys.map(k => [k, []]));
  for (const k of s.keys) for (const p of s.dagPreds.get(k)) succs.get(p).push(k);
  const layers = [];
  for (const k of s.keys) (layers[s.depth.get(k)] ||= []).push(k);
  const pos = new Map();
  const place = () => layers.forEach((layer, i) => layer.forEach((k, j) => pos.set(k, { layer: i, order: j })));
  place();

  const reorder = (i, neighbours) => {
    const bary = new Map(layers[i].map((k, j) => {
      const ns = neighbours.get(k);
      return [k, ns.length ? ns.reduce((sum, n) => sum + pos.get(n).order, 0) / ns.length : j];
    }));
    layers[i] = layers[i].map((k, j) => [k, j]).sort((a, b) => bary.get(a[0]) - bary.get(b[0]) || a[1] - b[1]).map(([k]) => k);
    layers[i].forEach((k, j) => pos.set(k, { layer: i, order: j }));
  };
  let best = layers.map(l => [...l]);
  let bestCross = crossingCount(layers, pos, s.dagPreds);
  for (let sweep = 0; sweep < SWEEPS && bestCross > 0; sweep++) {
    for (let i = 1; i < layers.length; i++) reorder(i, s.dagPreds);
    for (let i = layers.length - 2; i >= 0; i--) reorder(i, succs);
    const cross = crossingCount(layers, pos, s.dagPreds);
    if (cross < bestCross) {
      bestCross = cross;
      best = layers.map(l => [...l]);
    }
  }
  best.forEach((l, i) => { layers[i] = l; });
  place();

  const crit = longestPath(s, new Set(options.skipCritical || ['cancelled']));
  const onPath = new Set(crit.path);
  const nodes = s.keys.map(k => {
    const p = pos.get(k);
    return {
      key: k, layer: p.layer, order: p.order, w: NODE_W, h: NODE_H,
      x: PAD + p.layer * (NODE_W + COL_GAP), y: PAD + p.order * (NODE_H + ROW_GAP),
      cyclic: s.cyclic.has(k), critical: onPath.has(k),
    };
  });
  const edges = [];
  for (const k of s.keys) {
    for (const p of s.preds.get(k)) {
      edges.push({ from: p, to: k, critical: onPath.has(k) && crit.prev.get(k) === p, cyclic: !s.dagPreds.get(k).includes(p) });
    }
  }
  const rows = Math.max(1, ...layers.map(l => l.length));
  const cols = Math.max(1, layers.length);
  return {
    nodes, edges,
    width: PAD * 2 + cols * NODE_W + (cols - 1) * COL_GAP,
    height: PAD * 2 + rows * NODE_H + (rows - 1) * ROW_GAP,
  };
}

// --- SVG-схема ---

const SVG_NS = 'http://www.w3.org/2000/svg';
// зависимость в этих статусах уже не держит задачу
const SATISFIED = new Set(['approved', 'sent', 'done']);
const DIM = new Set(['done', 'cancelled']);

function svg(tag, attrs, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attrs || {})) {
    if (value !== null && value !== undefined && value !== false) el.setAttribute(name, String(value));
  }
  for (const c of children) if (c) el.append(c);
  return el;
}

const color = id => 'var(' + statusInfo(id).color + ')';

function edgePath(a, b) {
  const x1 = a.x + a.w;
  const y1 = a.y + a.h / 2;
  const x2 = b.x;
  const y2 = b.y + b.h / 2;
  const mx = (x1 + x2) / 2;
  return { d: 'M' + x1 + ',' + y1 + ' C' + mx + ',' + y1 + ' ' + mx + ',' + y2 + ' ' + x2 + ',' + y2, x2, y2 };
}

// Схема в контейнере host; узлы и рёбра по ключам переиспользуются, клик и Enter по узлу — onOpen(key).
export function mountGraph(host, onOpen) {
  const edgesLayer = svg('g', { class: 'edges' });
  const nodesLayer = svg('g', { class: 'nodes' });
  const root = svg('svg', { role: 'img', 'aria-label': T.graphAria }, edgesLayer, nodesLayer);
  host.replaceChildren(root);
  const nodes = new Map();
  const edges = new Map();

  const highlight = (key, on) => {
    for (const e of edges.values()) {
      if (e.from === key || e.to === key) e.path.classList.toggle('hl', on);
    }
  };

  function nodeEntry(key) {
    const bg = svg('rect', { class: 'bg', width: NODE_W, height: NODE_H, rx: 9 });
    const bar = svg('rect', { class: 'sbar', x: 0, y: 0, width: 4, height: NODE_H, rx: 2 });
    const tip = svg('title');
    const gk = h('span', { class: 'gk' });
    const gs = h('span', { class: 'gs' });
    const gt = h('div', { class: 'gt' });
    const fo = svg('foreignObject', { x: 0, y: 0, width: NODE_W, height: NODE_H },
      h('div', { class: 'gn' }, h('div', { class: 'gr' }, gk, gs), gt));
    const g = svg('g', { class: 'node', 'data-key': key, tabindex: '0', role: 'button' }, tip, bg, bar, fo);
    g.addEventListener('click', () => onOpen(key));
    g.addEventListener('keydown', e => {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      e.preventDefault();
      onOpen(key);
    });
    g.addEventListener('mouseenter', () => highlight(key, true));
    g.addEventListener('mouseleave', () => highlight(key, false));
    g.addEventListener('focus', () => highlight(key, true));
    g.addEventListener('blur', () => highlight(key, false));
    nodesLayer.append(g);
    return { g, bar, tip, gk, gs, gt, sig: null };
  }

  return {
    // isOut(task) — задача не проходит фильтры и показывается приглушённой
    paint(tasks, options = {}) {
      const layout = layoutGraph(tasks, options);
      const byKey = new Map(tasks.map(t => [t.key, t]));
      const placed = new Map(layout.nodes.map(n => [n.key, n]));
      root.setAttribute('width', layout.width);
      root.setAttribute('height', layout.height);
      root.setAttribute('viewBox', '0 0 ' + layout.width + ' ' + layout.height);

      for (const n of layout.nodes) {
        const t = byKey.get(n.key);
        const status = statusOf(t);
        const cls = ['node'];
        if (n.critical) cls.push('crit');
        if (n.cyclic) cls.push('cyc');
        if (DIM.has(status)) cls.push('dim');
        if ((t.rule_violations || []).length) cls.push('viol');
        if (options.isOut && options.isOut(t)) cls.push('f-out');
        const q = t.open_questions || 0;
        const sig = JSON.stringify([cls, t.title, status, q, n.cyclic]);
        let entry = nodes.get(n.key);
        if (!entry) {
          entry = nodeEntry(n.key);
          nodes.set(n.key, entry);
        }
        entry.g.setAttribute('transform', 'translate(' + n.x + ',' + n.y + ')');
        if (entry.sig === sig) continue;
        entry.sig = sig;
        entry.g.setAttribute('class', cls.join(' '));
        const label = statusInfo(status).label;
        entry.g.setAttribute('aria-label', n.key + ' — ' + t.title + ' (' + label + ')');
        entry.tip.textContent = n.key + ' — ' + t.title + (n.cyclic ? ' · ' + T.graphCycle : '');
        entry.bar.style.setProperty('fill', color(status));
        entry.gk.textContent = n.key + (q ? ' · ? ' + q : '');
        entry.gs.textContent = label;
        entry.gs.style.setProperty('color', color(status));
        entry.gt.textContent = t.title;
      }
      for (const [key, entry] of nodes) {
        if (!placed.has(key)) {
          entry.g.remove();
          nodes.delete(key);
        }
      }

      const seen = new Set();
      for (const e of layout.edges) {
        // зависимость задачи от самой себя видна по пометке цикла на узле
        if (e.from === e.to) continue;
        const id = e.from + '>' + e.to;
        seen.add(id);
        let entry = edges.get(id);
        if (!entry) {
          const path = svg('path', { class: 'edge' });
          const dot = svg('circle', { class: 'edge-end', r: 2.5 });
          const g = svg('g', { 'data-key': id }, path, dot);
          edgesLayer.append(g);
          entry = { g, path, dot, from: e.from, to: e.to };
          edges.set(id, entry);
        }
        const geo = edgePath(placed.get(e.from), placed.get(e.to));
        const from = byKey.get(e.from);
        const cls = ['edge'];
        if (e.critical) cls.push('crit');
        else if (SATISFIED.has(statusOf(from))) cls.push('ok');
        if (e.cyclic) cls.push('cyc');
        if (entry.path.classList.contains('hl')) cls.push('hl');
        entry.path.setAttribute('class', cls.join(' '));
        entry.path.setAttribute('d', geo.d);
        entry.dot.setAttribute('cx', geo.x2);
        entry.dot.setAttribute('cy', geo.y2);
      }
      for (const [id, entry] of edges) {
        if (!seen.has(id)) {
          entry.g.remove();
          edges.delete(id);
        }
      }
    },
  };
}
