// Кнопка «Повторить» у ошибки хода агента: перезапускает упавший ход агента задачи или агента проекта.
import { h } from './dom.js';
import { T } from './i18n.js';

export const retryPath = key => '/tasks/' + encodeURIComponent(key) + '/retry';

// Кнопка и место для ошибки запроса. Пока запрос идёт, кнопка неактивна; после успеха она остаётся
// неактивной с подписью «Запуск…», пока ошибка хода не исчезнет из снимка и кнопку не уберут.
export function retryControl(client, key, onDone) {
  const btn = h('button', { class: 'btn sm', type: 'button', 'data-retry': key, title: T.agentRetry.title }, T.agentRetry.label);
  const error = h('span', { class: 'ferr retry-err', role: 'alert', hidden: true });
  const el = h('span', { class: 'retry' }, btn, error);
  let busy = false;

  function setBusy(on, label) {
    busy = on;
    btn.disabled = on;
    btn.textContent = label;
  }

  async function run() {
    if (busy) return;
    setBusy(true, T.agentRetry.busy);
    error.hidden = true;
    try {
      await client.post(retryPath(key), { actor: 'user' });
      if (onDone) onDone();
    } catch (err) {
      setBusy(false, T.agentRetry.label);
      error.textContent = (err && err.message) || String(err);
      error.hidden = false;
    }
  }

  btn.addEventListener('click', e => {
    // кнопка стоит внутри кликабельных пунктов: щелчок не должен их открывать
    if (e && e.stopPropagation) e.stopPropagation();
    run();
  });

  return {
    el,
    run,
    // вернуть кнопку в исходное состояние, когда агент снова упадёт после успешного повтора
    reset() {
      setBusy(false, T.agentRetry.label);
      error.hidden = true;
    },
  };
}
