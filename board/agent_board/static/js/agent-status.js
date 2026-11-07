// Состояние агента внизу треда: работает ли он над ответом, ждёт слота, упал или остановлен.
// Пока агент простаивает, индикатора нет.
import { h, icon } from './dom.js';
import { T } from './i18n.js';
import { slotNote, slotWait } from './slot.js';

const TICK_MS = 1000;

// Когда начался ход: точное время, записанное доской, иначе время запуска процесса.
export function turnStartedAt(proc) {
  if (!proc || typeof proc !== 'object') return null;
  for (const v of [proc.at, proc.started]) {
    if (typeof v !== 'string' || !v) continue;
    const ms = Date.parse(v);
    if (!Number.isNaN(ms)) return ms;
  }
  return null;
}

// Что показать про агента треда; null — агент простаивает.
// agent — {state, turn_error, wait_reason, parked, turn_process}.
export function threadAgentModel(agent, config) {
  if (!agent) return null;
  if (agent.state === 'running') return { kind: 'running', since: turnStartedAt(agent.turn_process) };
  const wait = slotWait(agent.state, agent.wait_reason, config);
  if (wait) return { kind: 'waiting', wait };
  if (agent.state === 'error') return { kind: 'error', text: agent.turn_error || T.agentError };
  if (agent.parked) return { kind: 'parked' };
  return null;
}

// Сколько идёт ход: «45 с», «3 мин 05 с», «1 ч 02 мин».
export function formatElapsed(ms) {
  const s = Math.max(0, Math.floor((Number(ms) || 0) / 1000));
  const pad = n => String(n).padStart(2, '0');
  if (s < 60) return T.thread.elapsed.s(s);
  if (s < 3600) return T.thread.elapsed.m(Math.floor(s / 60), pad(s % 60));
  return T.thread.elapsed.h(Math.floor(s / 3600), pad(Math.floor(s / 60) % 60));
}

// Индикатор для треда. parkedText — пояснение про остановленную переписку, своё у задачи и у проекта;
// alive() — владелец треда ещё открыт: иначе часы хода останавливаются;
// retry — кнопка «Повторить» у ошибки хода (одна на всё время жизни треда, чтобы перерисовка не сбрасывала запрос).
export function createAgentIndicator({ parkedText, alive, retry = null, now = () => Date.now(), timers = globalThis }) {
  const el = h('div', { class: 'agent-status', hidden: true, 'aria-live': 'polite' });
  let timer = null;
  let since = null;
  let clock = null;
  let signature = null;

  const stop = () => {
    if (timer !== null) timers.clearInterval(timer);
    timer = null;
  };
  const tick = () => {
    if (!alive()) return stop();
    if (clock && since !== null) clock.textContent = formatElapsed(now() - since);
  };

  function render(m) {
    if (m.kind === 'running') {
      // часы не зачитываются экранным диктором каждую секунду
      clock = since === null ? null : h('span', { class: 'elapsed mono', 'aria-hidden': 'true' });
      return h('div', { class: 'agent-run' },
        h('span', { class: 'dots', 'aria-hidden': 'true' }, h('i'), h('i'), h('i')),
        h('span', null, T.thread.running), clock);
    }
    if (m.kind === 'waiting') return slotNote(m.wait);
    if (m.kind === 'error') {
      return h('div', { class: 'agent-err' },
        h('div', { class: 'ferr', role: 'alert' }, h('b', null, icon('warn'), T.card.turnError + ': '), m.text),
        retry ? retry.el : null);
    }
    return h('div', { class: 'pa-note' }, parkedText);
  }

  return {
    el,
    paint(m) {
      // узел пересоздаётся только при смене состояния: анимация и часы не сбрасываются на каждом снимке
      const sig = m ? JSON.stringify(m) : null;
      if (sig === signature) return;
      signature = sig;
      stop();
      clock = null;
      // ход снова упадёт после успешного повтора — кнопка должна быть готова
      if (retry && (!m || m.kind !== 'error')) retry.reset();
      since = m && m.kind === 'running' ? m.since : null;
      el.hidden = !m;
      el.replaceChildren(...(m ? [render(m)] : []));
      if (clock) {
        tick();
        timer = timers.setInterval(tick, TICK_MS);
      }
    },
  };
}
