// Реестр действий шапки карточки: каждое действие описывает видимость, доступность и выполнение.
const registry = new Map();

// Регистрирует действие; повторная регистрация с тем же id заменяет его на прежнем месте.
// def: { id, label, icon, title?, visible(task, snapshot), enabled(task, snapshot), run(ctx, task) }
export function registerCardAction(def) {
  if (!def || !def.id || typeof def.run !== 'function') throw new Error('card action needs id and run');
  registry.set(def.id, def);
  return def;
}

export function unregisterCardAction(id) {
  registry.delete(id);
}

const call = (fn, fallback, task, snapshot) => (typeof fn === 'function' ? fn(task, snapshot) : fallback);

// Видимые действия задачи в порядке регистрации; title — подсказка (строка или функция от задачи).
export function cardActions(task, snapshot) {
  return [...registry.values()]
    .filter(def => call(def.visible, true, task, snapshot))
    .map(def => ({
      id: def.id,
      label: def.label,
      icon: def.icon,
      enabled: Boolean(call(def.enabled, true, task, snapshot)),
      title: call(def.title, def.title, task, snapshot) || null,
      run: def.run,
    }));
}

// Подпись видимого набора действий: по ней шапка решает, нужна ли перерисовка.
export const actionsSignature = actions => JSON.stringify(actions.map(a => [a.id, a.label, a.icon, a.enabled, a.title]));

// Запуск действий кнопками: пока действие выполняется, повторный клик игнорируется,
// а ошибка (синхронная или отклонённый промис) уходит в onError.
export function createActionRunner(onError) {
  const running = new Set();
  return {
    busy: id => running.has(id),
    async click(id, btn, run) {
      if (running.has(id)) return;
      running.add(id);
      btn.disabled = true;
      try {
        await Promise.resolve().then(run);
      } catch (err) {
        onError((err && err.message) || String(err));
      } finally {
        running.delete(id);
        btn.disabled = false;
      }
    },
  };
}
