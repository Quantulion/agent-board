import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  CATALOGS, STAGES, T, currentLanguage, plural, setLanguage, statusInfo, tasksCount, uiLanguage,
} from '../../agent_board/static/js/i18n.js';

const CYRILLIC = /[А-Яа-яЁё]/;
// аргумент-заглушка подходит и как число, и как список ключей, и как строка
const ARG = ['K-1', 'K-2'];

// Все листья каталога: путь и значение; функции вызываются с заглушками.
function leaves(node, path = []) {
  if (typeof node === 'function') return [[path.join('.'), node(ARG, ARG, ARG)]];
  if (node && typeof node === 'object') return Object.entries(node).flatMap(([k, v]) => leaves(v, [...path, k]));
  return [[path.join('.'), node]];
}

function shape(node) {
  if (typeof node === 'function') return 'function';
  if (Array.isArray(node)) return 'array:' + node.length;
  if (node && typeof node === 'object') {
    return Object.fromEntries(Object.keys(node).sort().map(k => [k, shape(node[k])]));
  }
  return typeof node;
}

test('plural склоняет по числу', () => {
  setLanguage('ru');
  const forms = ['задача', 'задачи', 'задач'];
  assert.deepEqual([0, 1, 2, 4, 5, 11, 12, 21, 22, 25, 101, 111].map(n => plural(n, forms)),
    ['задач', 'задача', 'задачи', 'задачи', 'задач', 'задач', 'задач', 'задача', 'задачи', 'задач', 'задача', 'задач']);
  assert.equal(tasksCount(3), '3 задачи');
});

test('statusInfo знает статусы и не падает на неизвестном', () => {
  setLanguage('ru');
  assert.deepEqual(statusInfo('in_review'), { id: 'in_review', label: 'На ревью', color: '--s-review' });
  assert.equal(statusInfo('zzz').label, 'zzz');
});

test('каталог en повторяет структуру ru: те же ключи, типы и длины списков', () => {
  assert.deepEqual(shape(CATALOGS.en), shape(CATALOGS.ru));
});

test('каталог en не содержит кириллицы ни в строках, ни в результатах функций', () => {
  const bad = leaves(CATALOGS.en).filter(([, v]) => typeof v !== 'string' || CYRILLIC.test(v));
  assert.deepEqual(bad, []);
});

test('setLanguage меняет строки, статусы и стадии, а смену языка сообщает', () => {
  setLanguage('ru');
  assert.equal(setLanguage('en'), true);
  assert.equal(setLanguage('English'), false);
  assert.equal(currentLanguage(), 'en');
  assert.equal(T.board, 'Board');
  assert.equal(statusInfo('in_review').label, 'In review');
  assert.equal(STAGES.find(s => s.id === 'work').label, 'Work');
  assert.equal(tasksCount(1), '1 task');
  assert.equal(tasksCount(3), '3 tasks');
  assert.equal(setLanguage('ru'), true);
  assert.equal(T.board, 'Доска');
});

test('uiLanguage: русский только для русского, остальное — английский', () => {
  assert.deepEqual(['ru', 'RU', 'russian', 'русский', '', null].map(uiLanguage), ['ru', 'ru', 'ru', 'ru', 'ru', 'ru']);
  assert.deepEqual(['en', 'English', 'de', 'эсперанто'].map(uiLanguage), ['en', 'en', 'en', 'en']);
});
