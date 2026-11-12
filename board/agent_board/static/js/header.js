// Шапка проекта: индикатор соединения, шкала стадий, кнопка следующего шага и вид шапки по типу проекта.
import { h } from './dom.js';
import { STAGES, T } from './i18n.js';
import { openModal, toast } from './ui.js';

// Индикатор соединения: пока поток отключён, он важнее ошибки перечитывания снимка.
export function connectionIndicator(state) {
  const reconnecting = state.connection === 'reconnecting';
  const failed = Boolean(state.error) && !reconnecting;
  const denied = failed && state.error.status === 401;
  return {
    failed,
    busy: !failed && state.connection !== 'live',
    text: denied ? T.unauthorized : failed ? T.syncError : T.connection[state.connection] || state.connection,
  };
}

// Части доски по типу проекта: у проекта «Без плана» нет стадий, плана и графа зависимостей.
export function boardLayout(kind) {
  const plan = kind !== 'free';
  return { free: !plan, stepper: plan, progress: plan, planning: plan, graph: plan, waves: plan };
}

// Подзаголовок проекта под названием.
export function projectSubtitle(config) {
  const repos = (config.repos || []).length;
  return config.kind === 'free'
    ? T.projectSubFree(repos, config.max_agents, config.auto_approve === true)
    : T.projectSub(repos, config.max_agents, config.chains);
}

// Стадии проекта для шкалы: пройдена, текущая, впереди или пропущена (трекер без подключённого трекера).
export function stepperModel(stage, trackerType) {
  const cur = STAGES.findIndex(s => s.id === stage);
  const noTracker = !trackerType || trackerType === 'none';
  return STAGES.map((s, i) => {
    let state = i < cur || (stage === 'done' && i === cur) ? 'done' : i === cur ? 'cur' : 'todo';
    if (s.id === 'tracker' && noTracker && state !== 'cur') state = 'skipped';
    return { id: s.id, label: s.label, state };
  });
}

// Следующий шаг проекта, который пользователь делает из шкалы этапов: из трекера — к работе,
// а когда все задачи готовы или отменены — завершение проекта.
export function stageAction(snapshot) {
  if (!snapshot) return null;
  const stage = snapshot.config.stage;
  const tasks = snapshot.tasks || [];
  if (stage === 'tracker') return { to: 'work', ...T.stageGo.work };
  if (stage === 'work' && tasks.length && tasks.every(t => t.status === 'done' || t.status === 'cancelled')) {
    return { to: 'done', ...T.stageGo.done };
  }
  return null;
}

function confirmStage(ctx, action) {
  const error = h('div', { class: 'ferr', role: 'alert', hidden: true });
  let sending = false;
  const go = () => {
    if (sending) return;
    sending = true;
    okBtn.disabled = true;
    error.hidden = true;
    ctx.client.post('/stage', { stage: action.to, actor: 'user' })
      .then(() => {
        close();
        toast(action.toast);
      })
      .catch(err => {
        okBtn.disabled = false;
        error.textContent = (err && err.message) || String(err);
        error.hidden = false;
      })
      .finally(() => { sending = false; });
  };
  const okBtn = h('button', { class: 'btn primary', type: 'button', onclick: go }, action.ok);
  const { close } = openModal(action.title, [h('p', { class: 'mtext' }, action.text), error],
    [h('span', { class: 'grow' }), h('button', { class: 'btn', type: 'button', onclick: () => close() }, T.cancel), okBtn]);
  okBtn.focus();
}

// Кнопка следующего шага рядом со шкалой; узел переиспользуется, пока шаг тот же, — фокус не теряется.
export function paintStageAction(slot, ctx, snapshot) {
  const action = stageAction(snapshot);
  slot.hidden = !action;
  if (!action) return slot.replaceChildren();
  if (slot.dataset.to === action.to && slot.firstChild) return;
  slot.dataset.to = action.to;
  slot.replaceChildren(h('button', { class: 'btn sm primary', type: 'button', onclick: () => confirmStage(ctx, action) }, action.label));
}

export function paintStepper(stepper, config) {
  const items = [];
  stepperModel(config.stage, config.tracker && config.tracker.type).forEach((s, i) => {
    if (i) items.push(h('span', { class: 'sep' }));
    const skipped = s.state === 'skipped';
    items.push(h('span', { class: 'st ' + s.state, title: skipped ? T.stageSkipped : null },
      h('i', null, s.state === 'done' ? '✓' : skipped ? '–' : String(i + 1)), s.label,
      skipped ? h('span', { class: 'skp' }, T.stageSkipped) : null));
  });
  stepper.replaceChildren(...items);
}
