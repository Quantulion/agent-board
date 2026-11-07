import { test } from 'node:test';
import assert from 'node:assert/strict';
import { installFakeDocument } from './fake-dom.mjs';
import { setLanguage } from '../../agent_board/static/js/i18n.js';
import { retryControl, retryPath } from '../../agent_board/static/js/retry.js';

installFakeDocument();
setLanguage('ru');

// Клиент, ответ которого тест отпускает сам: так видно состояние кнопки, пока запрос идёт.
function pendingClient() {
  const calls = [];
  let settle;
  return {
    calls,
    post(path, body) {
      calls.push([path, body]);
      return new Promise((resolve, reject) => { settle = { resolve, reject }; });
    },
    resolve: v => settle.resolve(v),
    reject: e => settle.reject(e),
  };
}

const parts = control => {
  const [btn, error] = control.el.children;
  return { btn, error };
};

test('retryPath кодирует ключ задачи и агента проекта', () => {
  assert.equal(retryPath('AB-1'), '/tasks/AB-1/retry');
  assert.equal(retryPath('_project'), '/tasks/_project/retry');
  assert.equal(retryPath('a/b'), '/tasks/a%2Fb/retry');
});

test('нажатие отправляет повтор от пользователя, пока запрос идёт кнопка неактивна', async () => {
  // arrange
  const client = pendingClient();
  let done = 0;
  const control = retryControl(client, 'AB-1', () => { done += 1; });
  const { btn } = parts(control);
  assert.equal(btn.textContent, 'Повторить');
  // act
  const running = control.run();
  // assert
  assert.deepEqual(client.calls, [['/tasks/AB-1/retry', { actor: 'user' }]]);
  assert.equal(btn.disabled, true);
  assert.equal(btn.textContent, 'Запуск…');
  await control.run();
  assert.equal(client.calls.length, 1, 'повторный щелчок во время запроса игнорируется');
  client.resolve({ events: [] });
  await running;
  assert.equal(done, 1);
  assert.equal(btn.disabled, true, 'после успеха кнопка ждёт, пока снимок уберёт ошибку');
});

test('ошибка запроса показывается рядом с кнопкой, кнопка снова активна', async () => {
  // arrange
  const client = pendingClient();
  let done = 0;
  const control = retryControl(client, '_project', () => { done += 1; });
  const { btn, error } = parts(control);
  // act
  const running = control.run();
  client.reject(new Error('Повторить можно только упавший ход'));
  await running;
  // assert
  assert.equal(done, 0);
  assert.equal(btn.disabled, false);
  assert.equal(btn.textContent, 'Повторить');
  assert.equal(error.hidden, false);
  assert.equal(error.textContent, 'Повторить можно только упавший ход');
});

test('reset возвращает кнопку в исходное состояние и прячет ошибку', async () => {
  // arrange
  const client = pendingClient();
  const control = retryControl(client, 'AB-2');
  const { btn, error } = parts(control);
  const running = control.run();
  client.resolve({});
  await running;
  // act
  control.reset();
  // assert
  assert.equal(btn.disabled, false);
  assert.equal(btn.textContent, 'Повторить');
  assert.equal(error.hidden, true);
});

test('подписи кнопки берутся из языка проекта', () => {
  setLanguage('en');
  try {
    const { btn } = parts(retryControl(pendingClient(), 'AB-3'));
    assert.equal(btn.textContent, 'Retry');
  } finally {
    setLanguage('ru');
  }
});
