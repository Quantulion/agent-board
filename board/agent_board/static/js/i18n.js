// Все строки интерфейса. Язык проекта (config.language) пока не используется: интерфейс только на русском.

export const STATUSES = [
  { id: 'not_started', label: 'Не начата', color: '--s-todo' },
  { id: 'blocked', label: 'Заблокирована', color: '--s-blocked' },
  { id: 'in_progress', label: 'В работе', color: '--s-progress' },
  { id: 'waiting_answer', label: 'Ждёт ответа', color: '--s-answer' },
  { id: 'queued', label: 'Ждёт очереди', color: '--s-queue' },
  { id: 'in_review', label: 'На ревью', color: '--s-review' },
  { id: 'approved', label: 'Одобрена', color: '--s-approved' },
  { id: 'sent', label: 'Отправлена', color: '--s-sent' },
  { id: 'done', label: 'Готово', color: '--s-done' },
  { id: 'cancelled', label: 'Отменена', color: '--s-cancelled' },
];

const STATUS_BY_ID = new Map(STATUSES.map(s => [s.id, s]));

export function statusInfo(id) {
  return STATUS_BY_ID.get(id) || { id, label: id, color: '--s-todo' };
}

export const STAGES = [
  { id: 'decomposition', label: 'Разбивка' },
  { id: 'tracker', label: 'Трекер' },
  { id: 'work', label: 'Работа' },
  { id: 'done', label: 'Завершение' },
];

export const T = {
  appName: 'agent-board',
  projects: 'Проекты',
  projectsLead: 'Локальная доска задач для AI-агентов.',
  noProjects: 'Проектов пока нет. Создайте первый: agent-board init <slug> --title "Название"',
  loading: 'Загрузка…',
  theme: 'Тема',
  themeAuto: 'Авто',
  themeLight: 'Светлая',
  themeDark: 'Тёмная',
  stageAria: 'Этап проекта',
  connection: {
    connecting: 'Подключение…',
    live: 'В сети',
    reconnecting: 'Переподключение…',
    offline: 'Нет связи',
  },
  syncError: 'Ошибка загрузки',
  unauthorized: 'Нет доступа',
  unauthorizedHint: 'Откройте ссылку с токеном, которую печатает agent-board serve.',
  notFound: 'Страница не найдена',
  notFoundHint: 'Такого адреса нет.',
  projectNotFound: 'Проект не найден',
  loadError: 'Не удалось загрузить данные',
  toProjects: 'К списку проектов',
  decompositionSoon: 'Экран разбивки появится в следующих задачах.',
  noTasks: 'Задач пока нет',
  // доска
  board: 'Доска',
  graph: 'Граф',
  viewAria: 'Вид',
  newTask: 'Новая задача',
  projectPanel: 'Проект',
  planning: 'Планирование',
  decompositionLink: 'Разбивка',
  allRepos: 'Все репозитории',
  repoAria: 'Репозиторий',
  typeAria: 'Тип',
  typeAll: 'Все',
  typeCode: 'Код',
  typeResearch: 'Исследования',
  kindCode: 'Код',
  kindResearch: 'Исследование',
  onlyMine: 'Только требующие меня',
  searchPlaceholder: 'Ключ или название',
  waitingForYou: 'Ждут вас',
  dropHere: 'Перетащите сюда',
  collapse: 'Свернуть',
  current: 'текущий',
  colHint: { blocked: 'вычисляется', queued: 'общие ресурсы', approved: 'можно отправить', sent: 'в трекере' },
  attn: {
    question: n => n > 1 ? 'вопросы агента · ' + n : 'вопрос агента',
    proposal: { add_task: 'Агент проекта предлагает новую задачу', set_deps: 'агент проекта предлагает изменить зависимости', cancel_task: 'агент проекта предлагает отменить' },
    review: 'ревью ждёт вас',
    error: 'ошибка агента',
    projectError: 'Ошибка агента проекта',
    loop: 'агенты переписываются — остановлено',
    violation: n => 'нарушает правила разбивки' + (n > 1 ? ' · ' + n : ''),
  },
  waits: 'ждёт',
  queuePos: (resource, pos) => resource + ' · ' + pos + '-й в очереди',
  holds: resource => 'держит ресурс «' + resource + '»',
  agentRunning: 'идёт ход агента',
  agentWaitingTurn: 'ждёт свободного слота',
  agentError: 'ошибка агента',
  chainFrom: branch => 'от ' + branch,
  proposalChip: 'предложение',
  proposalsCount: n => n + ' ' + plural(n, ['предложение', 'предложения', 'предложений']),
  questionsTitle: 'Открытые вопросы',
  unreadTitle: 'Непрочитанные сообщения',
  chainTitle: 'Ветка от ветки зависимости',
  proposalTitle: 'Агент проекта предлагает изменение',
  estimate: n => String(n).replace('.', ',') + ' д',
  doneOf: (done, total) => done + ' из ' + total + ' готово',
  progressRest: (shipped, working) => '· ещё ' + shipped + ' ' + plural(shipped, ['одобрена или отправлена', 'одобрены или отправлены', 'одобрены или отправлены'])
    + ' · ' + working + ' в работе у агентов',
  projectSub: (repos, limit, chains) => repos + ' ' + plural(repos, ['репозиторий', 'репозитория', 'репозиториев'])
    + ' · лимит ' + limit + ' ' + plural(limit, ['агент', 'агента', 'агентов'])
    + ' · цепочки веток ' + (chains ? 'включены' : 'выключены'),
  soon: 'Будет в следующей задаче',
  statusUnchanged: 'Статус не изменился.',
  noReaction: 'Других изменений нет.',
  moveFailed: key => 'Не удалось перенести ' + key + ': ',
  statusMenuAria: 'Сменить статус',
  reaction: {
    unblocked: keys => (keys.length > 1 ? 'Разблокированы ' : 'Разблокирована ') + keys.join(', ') + '.',
    woken: keys => (keys.length > 1 ? 'Разбужены агенты ' : 'Разбужен агент ') + keys.join(', ') + '.',
    stopped: keys => (keys.length > 1 ? 'Остановлены агенты ' : 'Остановлен агент ') + keys.join(', ') + '.',
    depsRemoved: keys => 'Убрана из зависимостей ' + keys.join(', ') + '.',
  },
};

// Системное сообщение раннера, которым он останавливает переписку агентов без участия пользователя.
export const AGENTS_LOOP_PREFIX = 'Агенты переписываются без участия пользователя';

// Склонение по числу: plural(5, ['задача', 'задачи', 'задач']) -> 'задач'.
export function plural(n, forms) {
  const a = Math.abs(n) % 100;
  const b = a % 10;
  if (a > 10 && a < 20) return forms[2];
  if (b > 1 && b < 5) return forms[1];
  if (b === 1) return forms[0];
  return forms[2];
}

export function tasksCount(n) {
  return n + ' ' + plural(n, ['задача', 'задачи', 'задач']);
}
