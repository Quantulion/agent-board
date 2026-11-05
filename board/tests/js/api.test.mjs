import { test } from 'node:test';
import assert from 'node:assert/strict';
import { api, ApiError } from '../../agent_board/static/js/api.js';

function fakeFetch(status, body) {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({ url, init });
    return { ok: status < 400, status, json: async () => body };
  };
  fn.calls = calls;
  return fn;
}

test('get обращается к API проекта', async () => {
  // arrange
  const fetch = fakeFetch(200, { tasks: [] });
  // act
  const body = await api('a b', fetch).get('/state');
  // assert
  assert.deepEqual(body, { tasks: [] });
  assert.equal(fetch.calls[0].url, '/api/p/a%20b/state');
});

test('post отправляет JSON', async () => {
  // arrange
  const fetch = fakeFetch(200, { events: [] });
  // act
  await api('pay', fetch).post('/stage', { stage: 'work' });
  // assert
  const { init } = fetch.calls[0];
  assert.equal(init.method, 'POST');
  assert.equal(init.headers['Content-Type'], 'application/json');
  assert.equal(init.body, '{"stage":"work"}');
});

test('без проекта — корень API', async () => {
  // arrange
  const fetch = fakeFetch(200, { projects: [] });
  // act
  await api(null, fetch).get('/projects');
  // assert
  assert.equal(fetch.calls[0].url, '/api/projects');
});

test('ошибка сервера становится ApiError со статусом и текстом', async () => {
  // arrange
  const fetch = fakeFetch(403, { error: 'Недопустимый переход' });
  // act
  const err = await api('pay', fetch).post('/stage', {}).catch(e => e);
  // assert
  assert.ok(err instanceof ApiError);
  assert.equal(err.status, 403);
  assert.equal(err.message, 'Недопустимый переход');
});

test('сетевой сбой — ApiError со статусом 0', async () => {
  // arrange
  const fetch = async () => { throw new TypeError('Failed to fetch'); };
  // act
  const err = await api('pay', fetch).get('/state').catch(e => e);
  // assert
  assert.ok(err instanceof ApiError);
  assert.equal(err.status, 0);
});
