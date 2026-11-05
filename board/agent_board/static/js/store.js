// Живое состояние проекта: снимок с сервера плюс поток событий SSE.
// События лишь сигналят «что-то изменилось» — правдой остаётся перечитанный снимок.

// Типы событий журнала (сервер шлёт именованные события, EventSource требует подписки на каждый).
export const EVENT_TYPES = [
  'task_added', 'status_changed', 'task_updated', 'deps_changed', 'task_unblocked', 'agent_stop',
  'agent_wake', 'question_asked', 'question_answered', 'message_posted', 'thread_read',
  'proposal_created', 'proposal_applied', 'proposal_rejected', 'turn_started', 'turn_finished',
  'turn_failed', 'task_committed', 'lock_waiting', 'lock_acquired', 'lock_released', 'stage_changed',
];

export const COALESCE_MS = 150;
// столько нужно провести в сети без событий, чтобы следующая ошибка снова начала с короткой задержки
export const STABLE_MS = 5000;
// снимок с такой ошибкой не появится сам: поток закрывается до ручной перезагрузки
const FATAL_STATUSES = new Set([401, 404]);

export function nextBackoff(n) {
  return Math.min(8000, 500 * 2 ** Math.max(0, n));
}

// Задача считается изменённой, если отличается любое поле снимка (статус, замки, тред и т. п.).
export function diffTasks(prev, next) {
  const before = new Map((prev || []).map(t => [t.key, JSON.stringify(t)]));
  const after = new Map((next || []).map(t => [t.key, JSON.stringify(t)]));
  const added = [];
  const changed = [];
  for (const [key, value] of after) {
    if (!before.has(key)) added.push(key);
    else if (before.get(key) !== value) changed.push(key);
  }
  const removed = [...before.keys()].filter(key => !after.has(key));
  return { added, removed, changed };
}

export function createStore(slug, client, options = {}) {
  const ES = options.EventSource || globalThis.EventSource;
  const timers = options.timers || globalThis;
  const coalesceMs = options.coalesceMs ?? COALESCE_MS;

  let state = { snapshot: null, connection: 'connecting', error: null };
  const subscribers = new Set();
  const eventListeners = new Set();
  let es = null;
  let attempt = 0;
  let lastSeq = 0;
  let running = false;
  let reconnectTimer = null;
  let stableTimer = null;
  let refreshTimer = null;
  let inFlight = false;
  let again = false;

  function notify(changes) {
    for (const fn of [...subscribers]) fn(state, changes);
  }

  // Отдельные события нужны тем, кто реагирует на сам факт (например, конец хода), а не на снимок.
  function emitEvent(type, seq, raw) {
    if (!eventListeners.size) return;
    let data = {};
    try {
      const parsed = JSON.parse(raw);
      if (parsed && parsed.data && typeof parsed.data === 'object') data = parsed.data;
    } catch (e) {
      data = {};
    }
    for (const fn of [...eventListeners]) fn({ seq, type, data });
  }

  function setConnection(connection) {
    if (state.connection === connection) return;
    state = { ...state, connection };
    notify({ tasks: new Set(), snapshot: false, connection: true });
  }

  function apply(snap) {
    if (state.snapshot && snap.last_seq < state.snapshot.last_seq) return;
    const d = diffTasks(state.snapshot && state.snapshot.tasks, snap.tasks);
    lastSeq = Math.max(lastSeq, snap.last_seq || 0);
    state = { ...state, snapshot: snap, error: null };
    notify({ tasks: new Set([...d.added, ...d.removed, ...d.changed]), snapshot: true, connection: false });
  }

  // Чтения снимка не перекрываются: запрос во время чтения выполнится сразу после него.
  async function refresh() {
    if (inFlight) {
      again = true;
      return;
    }
    inFlight = true;
    try {
      const snap = await client.get('/state');
      if (running) apply(snap);
    } catch (e) {
      if (running) {
        state = { ...state, error: e };
        if (e && FATAL_STATUSES.has(e.status)) halt();
        notify({ tasks: new Set(), snapshot: false, connection: true });
      }
    } finally {
      inFlight = false;
      if (again && running) {
        again = false;
        scheduleRefresh();
      }
    }
  }

  function scheduleRefresh() {
    if (refreshTimer !== null || !running) return;
    refreshTimer = timers.setTimeout(() => {
      refreshTimer = null;
      refresh();
    }, coalesceMs);
  }

  function clearTimer(which) {
    const t = which === 'stable' ? stableTimer : which === 'reconnect' ? reconnectTimer : refreshTimer;
    if (t !== null) timers.clearTimeout(t);
    if (which === 'stable') stableTimer = null;
    else if (which === 'reconnect') reconnectTimer = null;
    else refreshTimer = null;
  }

  function closeAll() {
    if (es) es.close();
    es = null;
    for (const which of ['stable', 'reconnect', 'refresh']) clearTimer(which);
  }

  // Неустранимая ошибка: поток и переподключения прекращаются, состояние остаётся видимым.
  function halt() {
    running = false;
    closeAll();
    state = { ...state, connection: 'offline' };
  }

  function connect() {
    reconnectTimer = null;
    if (!running) return;
    const source = new ES('/api/p/' + encodeURIComponent(slug) + '/stream?since=' + lastSeq);
    es = source;
    source.onopen = () => {
      clearTimer('stable');
      stableTimer = timers.setTimeout(() => { stableTimer = null; attempt = 0; }, STABLE_MS);
      setConnection('live');
      scheduleRefresh();
    };
    source.onerror = () => {
      source.close();
      if (es !== source || !running) return;
      es = null;
      clearTimer('stable');
      setConnection('reconnecting');
      reconnectTimer = timers.setTimeout(connect, nextBackoff(attempt++));
    };
    for (const type of EVENT_TYPES) {
      source.addEventListener(type, e => {
        attempt = 0;
        const seq = Number(e.lastEventId);
        if (seq > lastSeq) lastSeq = seq;
        emitEvent(type, seq, e.data);
        scheduleRefresh();
      });
    }
  }

  return {
    get: () => state,
    subscribe(fn) {
      subscribers.add(fn);
      return () => subscribers.delete(fn);
    },
    onEvent(fn) {
      eventListeners.add(fn);
      return () => eventListeners.delete(fn);
    },
    start() {
      if (running) return;
      running = true;
      refresh().then(connect);
    },
    stop() {
      running = false;
      closeAll();
    },
  };
}
