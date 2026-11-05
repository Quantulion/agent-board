import { test } from 'node:test';
import assert from 'node:assert/strict';
import { NODE_H, NODE_W, criticalPath, layoutGraph } from '../../agent_board/static/js/graph.js';

const t = (key, deps = [], extra = {}) => ({ key, title: key, deps, status: 'not_started', estimate: null, ...extra });
const byKey = layout => new Map(layout.nodes.map(n => [n.key, n]));
const layerOf = (layout, key) => byKey(layout).get(key).layer;

// Число пересечений рёбер между соседними слоями при текущем порядке узлов.
function crossings(layout) {
  const pos = byKey(layout);
  const edges = layout.edges.map(e => [pos.get(e.from), pos.get(e.to)]).filter(([a, b]) => b.layer === a.layer + 1);
  let n = 0;
  for (let i = 0; i < edges.length; i++) {
    for (let j = i + 1; j < edges.length; j++) {
      const [a1, b1] = edges[i];
      const [a2, b2] = edges[j];
      if (a1.layer !== a2.layer) continue;
      if ((a1.order - a2.order) * (b1.order - b2.order) < 0) n++;
    }
  }
  return n;
}

test('layoutGraph: слой — длина самой длинной цепочки зависимостей', () => {
  // arrange
  const tasks = [t('A'), t('B', ['A']), t('C', ['B']), t('D', ['A', 'C']), t('E')];
  // act
  const layout = layoutGraph(tasks);
  // assert
  assert.deepEqual(['A', 'B', 'C', 'D', 'E'].map(k => layerOf(layout, k)), [0, 1, 2, 3, 0]);
  const a = byKey(layout).get('A');
  const d = byKey(layout).get('D');
  assert.equal(a.w, NODE_W);
  assert.equal(a.h, NODE_H);
  assert.ok(d.x > a.x, 'зависимая задача правее зависимости');
  assert.ok(layout.width >= d.x + d.w && layout.height >= a.y + a.h, 'холст вмещает все узлы');
});

test('layoutGraph: узлы одного слоя не перекрываются', () => {
  // arrange
  const tasks = [t('A'), t('B'), t('C'), t('D', ['A']), t('E', ['B']), t('F', ['C'])];
  // act
  const layout = layoutGraph(tasks);
  // assert
  for (const layer of [0, 1]) {
    const ys = layout.nodes.filter(n => n.layer === layer).map(n => n.y).sort((x, y) => x - y);
    ys.slice(1).forEach((y, i) => assert.ok(y - ys[i] >= NODE_H, 'между узлами слоя не меньше высоты узла'));
  }
});

test('layoutGraph: ромб раскладывается без пересечений', () => {
  // arrange
  const tasks = [t('A'), t('B', ['A']), t('C', ['A']), t('D', ['B', 'C'])];
  // act
  const layout = layoutGraph(tasks);
  // assert
  assert.equal(crossings(layout), 0);
  assert.equal(layout.edges.length, 4);
});

test('layoutGraph: порядок внутри слоя по барицентру убирает пересечения порядка ключей', () => {
  // arrange: по ключам X стоит выше Y, но X зависит от нижней B, а Y — от верхней A
  const tasks = [t('A'), t('B'), t('X', ['B']), t('Y', ['A']), t('Z', ['B'])];
  // act
  const layout = layoutGraph(tasks);
  // assert
  assert.equal(crossings(layout), 0);
  const pos = byKey(layout);
  assert.ok(pos.get('Y').order < pos.get('X').order);
});

test('layoutGraph: критический путь — самая длинная цепочка по оценкам, без оценки считается 1', () => {
  // arrange: A(1) → B(5) → D(1) длиннее, чем A → C(2) → D
  const tasks = [t('A'), t('B', ['A'], { estimate: 5 }), t('C', ['A'], { estimate: 2 }), t('D', ['B', 'C']), t('E', [], { estimate: 3 })];
  // act
  const layout = layoutGraph(tasks);
  // assert
  const crit = layout.edges.filter(e => e.critical).map(e => e.from + '>' + e.to).sort();
  assert.deepEqual(crit, ['A>B', 'B>D']);
  assert.deepEqual(layout.nodes.filter(n => n.critical).map(n => n.key).sort(), ['A', 'B', 'D']);
  assert.deepEqual(criticalPath(tasks), { path: ['A', 'B', 'D'], total: 7 });
});

test('layoutGraph: отменённые задачи не входят в критический путь', () => {
  // arrange
  const tasks = [t('A'), t('B', ['A'], { estimate: 9, status: 'cancelled' }), t('C', ['A'], { estimate: 2 })];
  // act
  const layout = layoutGraph(tasks);
  // assert
  assert.deepEqual(layout.nodes.filter(n => n.critical).map(n => n.key).sort(), ['A', 'C']);
  assert.ok(byKey(layout).has('B'), 'отменённая задача остаётся на схеме');
});

test('layoutGraph: статусы вне критического пути задаются параметром, display_status важнее status', () => {
  // arrange
  const tasks = [t('A', [], { status: 'done', estimate: 4 }), t('B', ['A']), t('C', [], { display_status: 'approved', status: 'in_review', estimate: 3 })];
  // act
  const layout = layoutGraph(tasks, { skipCritical: ['cancelled', 'done', 'approved'] });
  // assert
  assert.deepEqual(layout.nodes.filter(n => n.critical).map(n => n.key), ['B']);
});

test('layoutGraph: цикл не роняет раскладку, узлы цикла помечены', () => {
  // arrange
  const tasks = [t('A', ['C']), t('B', ['A']), t('C', ['B']), t('D', ['C']), t('E', ['E'])];
  // act
  const layout = layoutGraph(tasks);
  // assert
  const pos = byKey(layout);
  assert.deepEqual(['A', 'B', 'C', 'E'].map(k => pos.get(k).cyclic), [true, true, true, true]);
  assert.equal(pos.get('D').cyclic, false);
  assert.ok(pos.get('D').layer > pos.get('C').layer, 'задача после цикла правее него');
  assert.ok(layout.nodes.every(n => Number.isFinite(n.x) && Number.isFinite(n.y)));
});

test('layoutGraph: зависимости от задач вне списка не рисуются, пустой граф', () => {
  // arrange
  const tasks = [t('A', ['GONE'])];
  // act
  const layout = layoutGraph(tasks);
  const empty = layoutGraph([]);
  // assert
  assert.equal(layout.edges.length, 0);
  assert.equal(layerOf(layout, 'A'), 0);
  assert.deepEqual(empty.nodes, []);
  assert.ok(empty.width > 0 && empty.height > 0);
});

test('layoutGraph: раскладка детерминирована и не зависит от порядка входа', () => {
  // arrange
  const tasks = [t('PAY-10', ['PAY-2']), t('PAY-2'), t('PAY-3', ['PAY-2']), t('PAY-11', ['PAY-3', 'PAY-10'])];
  // act
  const a = layoutGraph(tasks);
  const b = layoutGraph([...tasks].reverse());
  // assert
  const coords = l => l.nodes.map(n => [n.key, n.x, n.y]).sort();
  assert.deepEqual(coords(a), coords(b));
});
