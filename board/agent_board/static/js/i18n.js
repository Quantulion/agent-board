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
  },
  unauthorized: 'Нет доступа',
  unauthorizedHint: 'Откройте ссылку с токеном, которую печатает agent-board serve.',
  notFound: 'Страница не найдена',
  notFoundHint: 'Такого адреса нет.',
  projectNotFound: 'Проект не найден',
  loadError: 'Не удалось загрузить данные',
  toProjects: 'К списку проектов',
  boardSoon: 'Доска появится в следующих задачах — пока список задач.',
  decompositionSoon: 'Экран разбивки появится в следующих задачах.',
  noTasks: 'Задач пока нет',
};

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
