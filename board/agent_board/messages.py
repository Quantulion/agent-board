"""Сообщения консольной утилиты и ошибки доски на английском и русском.

Сообщение создаётся по ключу каталога и сразу читается как строка на языке процесса; сервер переводит его на язык
запроса, поэтому ошибка доски приходит в CLI и интерфейс на языке того, кто спросил.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

LANGUAGES = ("en", "ru")
DEFAULT_LANGUAGE = "en"
LANG_ENV = "AGENT_BOARD_LANG"
LANG_HEADER = "X-Board-Lang"

# ключ → (английский, русский); параметры — в виде {name}
CATALOG: Dict[str, tuple] = {
    # --- CLI ---
    "cli.not_running": ("the board is not running: python3 -m agent_board serve",
                        "доска не запущена: python3 -m agent_board serve"),
    "cli.no_project": ("Project is not set: --project or $AGENT_BOARD_PROJECT",
                       "Не указан проект: --project или $AGENT_BOARD_PROJECT"),
    "cli.bad_json": ("Invalid JSON in {name}: {error}", "Некорректный JSON в {name}: {error}"),
    "cli.timeout": ("The board did not answer in {seconds:g} s: the request may still be running on the server — "
                    "check the board",
                    "Доска не ответила за {seconds:g} с: запрос всё ещё выполняется на сервере — проверьте доску"),
    "cli.unknown_task": ("Unknown task: {key}", "Неизвестная задача: {key}"),
    "cli.status_usage": ("Usage: agent-board status (board state) or status <KEY> <STATUS>",
                         "Использование: agent-board status (состояние доски) или status <KEY> <STATUS>"),
    "cli.tracker_unavailable": ("no access to the tracker", "нет доступа к трекеру"),
    "cli.no_command": ("No command given: agent-board lock-run <resource> -- <command...>",
                       "Не указана команда: agent-board lock-run <ресурс> -- <команда...>"),
    "cli.no_task_env": ("$AGENT_BOARD_TASK is not set", "Не задана переменная $AGENT_BOARD_TASK"),
    "cli.lock_waiting": ("waiting for resource {resource}: position {position}",
                         "ждёт ресурс {resource}: позиция {position}"),
    "cli.run_failed": ("Failed to start {command}: {error}", "Не удалось запустить {command}: {error}"),
    "cli.release_failed": ("Failed to release resource {resource}: {error}",
                           "Не удалось освободить ресурс {resource}: {error}"),
    "cli.board_unavailable": ("the board is unavailable, waiting…", "доска недоступна, жду…"),
    "cli.project_exists": ("Project already exists: {slug}", "Проект уже существует: {slug}"),
    # справка команд
    "help.project": ("project slug (default $AGENT_BOARD_PROJECT)", "slug проекта (по умолчанию $AGENT_BOARD_PROJECT)"),
    "help.serve": ("start the board", "запустить доску"),
    "help.port": ("default: a fixed port in 8737–8746", "по умолчанию постоянный 8737–8746"),
    "help.no_runner": ("do not start agents (board debugging)", "не запускать агентов (отладка доски)"),
    "help.no_tracker_sync": ("do not start background tracker sync", "не запускать фоновую синхронизацию трекера"),
    "help.detach": ("run in the background (log in <home>/server.log), print the link",
                    "запустить в фоне (лог в <home>/server.log), напечатать ссылку"),
    "help.stop": ("stop the running board", "остановить запущенную доску"),
    "help.version": ("agent-board version", "версия agent-board"),
    "help.init": ("create a project", "создать проект"),
    "help.state": ("full project snapshot", "весь snapshot проекта"),
    "help.show": ("show a task", "показать задачу"),
    "help.status": ("without arguments: board and project state; status <KEY> <STATUS>: move a task",
                    "без аргументов: состояние доски и проектов; status <KEY> <STATUS>: перевести задачу"),
    "help.status_json": ("board state as a JSON object", "состояние доски JSON-объектом"),
    "help.update": ("update task card fields", "обновить поля карточки задачи"),
    "help.ask": ("ask the user a question", "задать вопрос пользователю"),
    "help.say": ("post a message to a thread", "написать сообщение в тред"),
    "help.thread": ("show a thread", "показать тред"),
    "help.add_task": ("add a task (user only)", "добавить задачу (только пользователь)"),
    "help.deps": ("replace task dependencies with a list of keys (user only)",
                  "заменить зависимости задачи списком ключей (только пользователь)"),
    "help.deps_json": ("dependency keys as a JSON list", "ключи зависимостей JSON-списком"),
    "help.stage": ("move the project to a stage: tracker, work, done (user only)",
                   "перевести проект на стадию: tracker, work, done (только пользователь)"),
    "help.ship": ("send approved tasks to review with the project command (user only)",
                  "отправить одобренные задачи в ревью командой проекта (только пользователь)"),
    "help.retry": ("retry a failed turn of a task agent or the project agent (_project); user only",
                   "повторить упавший ход агента задачи или агента проекта (_project); только пользователь"),
    "help.rebase": ("move the task branch onto the current base together with uncommitted work; "
                    "user or project agent",
                    "перенести ветку задачи на текущую базу вместе с незакоммиченной работой; "
                    "пользователь или агент проекта"),
    "help.tracker": ("create tasks in the tracker (create), check access (check) or sync statuses now (sync); "
                     "user only",
                     "завести задачи в трекере (create), проверить доступ (check) или синхронизировать статусы "
                     "сейчас (sync); только пользователь"),
    "help.config": ("project settings: show, change or validate (user only)",
                    "настройки проекта: показать, изменить или проверить (только пользователь)"),
    "help.config_get": ("show settings", "показать настройки"),
    "help.as_json": ("print as a JSON object", "вывести JSON-объектом"),
    "help.config_set": ("change settings fields (all save checks)", "изменить поля настроек (все проверки сохранения)"),
    "help.config_fields": ("settings fields as a JSON object", "поля настроек JSON-объектом"),
    "help.dry_run": ("only validate, do not save", "только проверить, не сохраняя"),
    "help.config_validate": ("validate saved settings", "проверить сохранённые настройки"),
    "help.config_schema": ("allowed settings fields: types, defaults, enums; the board is not needed",
                           "допустимые поля настроек: типы, значения по умолчанию, перечисления; доска не нужна"),
    "help.propose": ("propose a graph change to the user (project agent)",
                     "предложить пользователю изменение графа (агент проекта)"),
    "help.lock_run": ("run a command holding an exclusive resource", "выполнить команду под эксклюзивным ресурсом"),
    "help.events": ("project events after a sequence number", "события проекта после номера"),
    "help.self_update": ("install the board from a repository commit and switch agent-board to it",
                         "установить доску из коммита репозитория и переключить на неё agent-board"),
    "help.ref": ("commit, branch or tag (default: the one from the previous install, else the main branch)",
                 "коммит, ветка или тег (по умолчанию — из прошлой установки, иначе основная ветка)"),
    "help.repo": ("repository to install from (default: the one from the previous install)",
                  "репозиторий, из которого ставить (по умолчанию — из прошлой установки)"),
    # --- установка ---
    "install.no_repo": ("no install found: run python3 <repo>/scripts/install.py or pass --repo",
                        "установка не найдена: запустите python3 <repo>/scripts/install.py или укажите --repo"),
    "install.failed": ("install failed: {error}", "установка не удалась: {error}"),
    "install.done": ("installed commit {commit}: {wrapper}", "установлен коммит {commit}: {wrapper}"),
    "install.unchanged": ("commit {commit} is already installed: {wrapper}",
                          "коммит {commit} уже установлен: {wrapper}"),
    "install.restart": ("the running board still uses the old code; restart it: "
                        "agent-board stop && agent-board serve --detach",
                        "запущенная доска работает на старом коде; перезапустите её: "
                        "agent-board stop && agent-board serve --detach"),
    "install.path_hint": ("add {bin} to PATH to use agent-board", "добавьте {bin} в PATH, чтобы вызывать agent-board"),
    # --- запуск и остановка сервера ---
    "life.restart_hint": ("restart it: agent-board stop && agent-board serve --detach",
                          "перезапустите: agent-board stop && agent-board serve --detach"),
    "life.not_running_status": ("the board is not running (agent-board serve --detach)",
                                "доска не запущена (agent-board serve --detach)"),
    "life.running": ("the board is running: {url} (version {version}, pid {pid})",
                     "доска запущена: {url} (версия {version}, pid {pid})"),
    "life.project_unreadable": ("  {slug}: failed to read the project", "  {slug}: не удалось прочитать проект"),
    "life.project_line": ("  {slug} “{title}”: stage {stage}, {tasks} tasks",
                          "  {slug} «{title}»: стадия {stage}, задач {tasks}"),
    "life.no_projects": ("  no projects", "  проектов нет"),
    "life.version_unknown": ("unknown", "неизвестна"),
    "life.other_version": ("the board is already running another version ({running}, need {needed}): {hint}",
                           "доска уже запущена другой версии ({running}, нужна {needed}): {hint}"),
    "life.start_failed": ("the server did not start (code {code}), {log}:\n{tail}",
                          "сервер не запустился (код {code}), {log}:\n{tail}"),
    "life.start_timeout": ("the server did not come up in {seconds:g} s, {log}:\n{tail}",
                           "сервер не поднялся за {seconds:g} с, {log}:\n{tail}"),
    "life.not_running": ("the board is not running", "доска не запущена"),
    "life.not_board": ("pid {pid} from {path} does not look like the board server, the process was left alone",
                       "pid {pid} из {path} не похож на сервер доски, процесс не тронут"),
    "life.stopped": ("the board is stopped", "доска остановлена"),
    "life.stop_failed": ("failed to stop pid {pid}: {error}", "не удалось остановить pid {pid}: {error}"),
    "life.stop_timeout": ("the board (pid {pid}) did not stop in {seconds:g} s",
                          "доска (pid {pid}) не остановилась за {seconds:g} с"),
    "life.already_running": ("The board is already running (pid {pid}, port {port})",
                             "Доска уже запущена (pid {pid}, порт {port})"),
    "life.already_running_home": ("The board is already running with this directory: {path}",
                                  "Доска уже запущена с этим каталогом: {path}"),
    # --- HTTP API ---
    "api.unknown_project": ("Unknown project: {slug}", "Неизвестный проект: {slug}"),
    "api.missing_field": ("Missing field: {name}", "Не указано поле: {name}"),
    "api.bad_length": ("Invalid Content-Length", "Некорректный Content-Length"),
    "api.body_too_large": ("Request body is too large", "Слишком большое тело запроса"),
    "api.bad_json": ("Invalid JSON", "Некорректный JSON"),
    "api.body_not_object": ("Request body must be a JSON object", "Тело запроса должно быть JSON-объектом"),
    "api.unknown_route": ("Unknown route", "Неизвестный маршрут"),
    "api.config_read_user_only": ("Only the user can read project settings",
                                  "Настройки проекта читает только пользователь"),
    "api.diff_failed": ("Failed to get changes: {error}", "Не удалось получить изменения: {error}"),
    "api.no_path": ("path is not set", "Не указан path"),
    "api.path_outside": ("Path is outside the project directories", "Путь вне каталогов проекта"),
    "api.path_unavailable": ("Path is not accessible", "Путь недоступен"),
    "api.file_not_found": ("File not found", "Файл не найден"),
    "api.file_too_large": ("File is larger than {mb} MB", "Файл больше {mb} МБ"),
    "api.bad_since": ("Invalid since: {value!r}", "Некорректный since: {value!r}"),
    "api.bad_lock_body": ("task must be a string, pid a positive number",
                          "task должен быть строкой, pid — положительным числом"),
    "api.bad_dry_run": ("dry_run must be true or false", "dry_run должен быть true или false"),
    "api.bad_author": ("Invalid author: {author!r}", "Недопустимый автор: {author!r}"),
    "api.bad_from_task": ("from_task must be a string", "from_task должен быть строкой"),
    "api.bad_deps": ("deps must be a list", "deps должен быть списком"),
    "api.bad_choice": ("choice must be a string or null", "choice должен быть строкой или null"),
    "api.bad_comment": ("comment must be a string", "comment должен быть строкой"),
    "api.bad_payload": ("payload must be an object", "payload должен быть объектом"),
    "api.bad_question_body": ("question must be an object", "question должен быть объектом"),
    "api.bad_question": ("Invalid question: {error}", "Некорректный вопрос: {error}"),
    # --- доска ---
    "board.task_not_object": ("A task must be an object", "Задача должна быть объектом"),
    "board.task_extra_fields": ("Invalid task fields: {names}", "Недопустимые поля задачи: {names}"),
    "board.task_field_str": ("Task field {name} must be a string", "Поле задачи {name} должно быть строкой"),
    "board.task_kind": ("Task kind must be one of: {kinds}", "Вид задачи должен быть одним из: {kinds}"),
    "board.task_estimate": ("Task estimate must be a number", "Оценка задачи должна быть числом"),
    "board.task_deps": ("Task deps must be a list of strings", "deps задачи должен быть списком строк"),
    "board.tracker_key": ("tracker_key: a non-empty string without spaces, up to {max} characters",
                          "tracker_key: непустая строка без пробелов, до {max} символов"),
    "board.tracker_url": ("tracker_url: an http(s) link", "tracker_url: ссылка http(s)"),
    "board.rule_violations": ("rule_violations must be a list of at most {max} items",
                              "rule_violations должен быть списком не длиннее {max}"),
    "board.rule_violation_item": ("A rule_violations item must be an object with strings rule and text",
                                  "Элемент rule_violations должен быть объектом со строками rule и text"),
    "board.unknown_event": ("Unknown event type: {type!r}", "Неизвестный тип события: {type!r}"),
    "board.config_user_only": ("Only the user can change project settings",
                               "Настройки проекта меняет только пользователь"),
    "board.fields_not_object": ("fields must be an object", "fields должен быть объектом"),
    "board.config_conflict": ("Settings ({names}) are being changed from elsewhere at the same time — try again later",
                              "Настройки ({names}) одновременно меняются из другого места — повторите позже"),
    "board.stage_user_only": ("Only the user can change the project stage",
                              "Стадию проекта меняет только пользователь"),
    "board.bad_stage": ("Invalid stage transition: {prev} -> {stage!r}",
                        "Недопустимый переход стадии: {prev} -> {stage!r}"),
    "board.add_task_user_only": ("Only the user adds tasks: if a new task is needed, write to the project agent "
                                 "(say _project)",
                                 "Задачи добавляет только пользователь: нужна новая задача — напишите агенту "
                                 "проекта (say _project)"),
    "board.unknown_actor": ("Unknown actor: {actor!r}", "Неизвестный актор: {actor!r}"),
    "board.blocked_computed": ("Status blocked is computed from dependencies; set an external block with the "
                               "external_blocker field",
                               "Статус blocked вычисляется по зависимостям; внешнюю блокировку задают полем "
                               "external_blocker"),
    "board.agent_status": ("An agent cannot set status {status}", "Агент не может ставить статус {status}"),
    "board.review_user_only": ("Only the user starts a review", "Ревью начинает только пользователь"),
    "board.review_not_in_review": ("A review starts only for a task in review (now {status})",
                                   "Ревью начинается только для задачи на ревью (сейчас {status})"),
    "board.no_review_tool": ("The project has no review tool: see the changes in the Changes tab",
                             "В проекте нет инструмента ревью: смотрите изменения во вкладке «Изменения»"),
    "board.review_open": ("A review round is already open", "Раунд ревью уже открыт"),
    "board.ship_user_only": ("Only the user sends to review", "Отправляет в ревью только пользователь"),
    "board.ship_keys": ("keys must be a non-empty list of task keys",
                        "keys должен быть непустым списком ключей задач"),
    "board.ship_duplicate": ("Task keys in the request repeat", "Ключи задач в запросе повторяются"),
    "board.ship_in_progress": ("Task {key} is already being sent", "Задача {key} уже отправляется"),
    "board.approve_user_only": ("Only the user approves", "Одобряет только пользователь"),
    "board.approve_not_in_review": ("Only a task in review can be approved (now {status})",
                                    "Одобрить можно только задачу на ревью (сейчас {status})"),
    "board.return_user_only": ("Only the user can return a task to work with a comment",
                               "Вернуть в работу с комментарием может только пользователь"),
    "board.comment_required": ("A comment is required", "Нужен комментарий"),
    "board.return_status": ("Only a task in review or after it can be returned to work (now {status})",
                            "Вернуть в работу можно задачу на ревью или после него (сейчас {status})"),
    "board.unknown_fields": ("Invalid fields: {names}", "Недопустимые поля: {names}"),
    "board.runner_fields": ("Only the runner changes these fields: {names}",
                            "Эти поля меняет только раннер: {names}"),
    "board.tracker_user_only": ("Only the user changes the tracker link",
                                "Привязку к трекеру меняет только пользователь"),
    "board.wording_user_only": ("Only the user changes the task wording",
                                "Формулировку задачи меняет только пользователь"),
    "board.violations_actor": ("Rule violations are recorded by the user and the project agent",
                               "Нарушения правил фиксируют пользователь и агент проекта"),
    "board.deps_user_only": ("Only the user changes dependencies: if the graph needs a change, write to the project "
                             "agent (say _project)",
                             "Зависимости меняет только пользователь: нужна правка графа — напишите агенту "
                             "проекта (say _project)"),
    "board.unknown_question": ("Unknown question: {id}", "Неизвестный вопрос: {id}"),
    "board.question_answered": ("The question is already answered: {id}", "На вопрос уже ответили: {id}"),
    "board.unknown_proposal_kind": ("Unknown proposal kind: {kind!r}", "Неизвестный вид предложения: {kind!r}"),
    "board.proposal_payload": ("Proposal payload must be an object", "payload предложения должен быть объектом"),
    "board.proposal_task": ("An add_task proposal needs a task object", "В предложении add_task нужен объект task"),
    "board.proposal_key": ("The proposal needs a string key", "В предложении нужен строковый key"),
    "board.proposal_deps": ("In a set_deps proposal deps must be a list of strings",
                            "В предложении set_deps deps должен быть списком строк"),
    "board.unknown_proposal": ("Unknown proposal: {id}", "Неизвестное предложение: {id}"),
    "board.proposal_done": ("The proposal is already processed: {id} ({status})",
                            "Предложение уже обработано: {id} ({status})"),
    "board.key_not_str": ("A task key must be a string", "Ключ задачи должен быть строкой"),
    "board.key_underscore": ("A task key cannot start with “_”: {key}",
                             "Ключ задачи не может начинаться с «_»: {key}"),
    "board.task_exists": ("The task already exists: {key}", "Задача уже существует: {key}"),
    "board.unknown_repo": ("Repository {repo} is not in the project settings: {names}",
                           "Репозиторий {repo} не из настроек проекта: {names}"),
    "board.graph_via_proposals": ("The project agent changes the graph only through proposals",
                                  "Агент проекта меняет граф только через предложения"),
    "board.unknown_task": ("Unknown task: {key}", "Неизвестная задача: {key}"),
    "board.bad_ident": ("Invalid identifier ({what}): {value!r}", "Недопустимый идентификатор ({what}): {value!r}"),
    "board.ident_project": ("project", "проект"),
    "board.ident_task": ("task", "задача"),
    "board.ident_thread": ("thread", "тред"),
    "board.project_exists": ("Project already exists: {slug}", "Проект уже существует: {slug}"),
    "board.self_dep": ("A task cannot depend on itself: {key}", "Задача не может зависеть от себя: {key}"),
    "board.unknown_dep": ("Dependency on an unknown task: {key}", "Зависимость от неизвестной задачи: {key}"),
    "board.cycle": ("Dependencies form a cycle: {cycle}", "Зависимости образуют цикл: {cycle}"),
    "board.undeclared_resource": ("Resource {resource!r} is not declared in the project. Declared: {names}",
                                  "Ресурс {resource!r} не объявлен в проекте. Объявленные: {names}"),
    "board.none": ("none", "нет"),
    # --- отправка в ревью ---
    "ship.no_command": ("The project has no review command (ship.command in settings)",
                        "В проекте не задана команда отправки в ревью (ship.command в настройках)"),
    "ship.auto_in_progress": ("the task is already being sent", "задача уже отправляется"),
    "ship.auto_dep_not_approved": ("dependency {key} is not approved yet ({status})",
                                   "зависимость {key} ещё не одобрена ({status})"),
    "ship.auto_dep_not_sent": ("dependency {key} has not been sent yet — send it, then this task",
                               "зависимость {key} ещё не отправлена — отправьте её, затем эту задачу"),
    "ship.on_approve_bool": ("ship.on_approve must be true or false", "ship.on_approve должен быть true или false"),
    "ship.on_approve_command": ("ship.on_approve needs a ship command: set ship.command",
                                "ship.on_approve требует команду отправки: задайте ship.command"),
    "ship.timeout": ("ship.timeout must be a positive number of seconds",
                     "ship.timeout должен быть положительным числом секунд"),
    "ship.quotes": ("Placeholder inside quotes: do not quote placeholders — values are already escaped",
                    "Плейсхолдер внутри кавычек: не берите плейсхолдеры в кавычки — значения уже экранированы"),
    "ship.ansi_c": ("$'…' quotes are not supported in the review command",
                    "Кавычки $'…' в команде отправки не поддерживаются"),
    "ship.heredoc": ("Inline text << (heredoc) is not supported in the review command: move it to a script",
                     "Встроенный текст << (heredoc) в команде отправки не поддерживается: вынесите его в скрипт"),
    "ship.bad_template": ("Invalid review command template: {error}",
                          "Некорректный шаблон команды отправки: {error}"),
    "ship.unknown_placeholder": ("Unknown placeholder in the review command: {{{name}}}. Allowed: {allowed}",
                                 "Неизвестный плейсхолдер в команде отправки: {{{name}}}. Допустимы: {allowed}"),
    "ship.not_approved": ("Only an approved task can be sent: {key} (now {status})",
                          "Отправить можно только одобренную задачу: {key} (сейчас {status})"),
    "ship.send_dep_first": ("{key}: send {dep} first", "{key}: сначала отправьте {dep}"),
    "ship.dep_not_approved": ("{key}: dependency {dep} is not approved yet ({status})",
                              "{key}: зависимость {dep} ещё не одобрена ({status})"),
    # --- настройки проекта ---
    "config.not_settable": ("Field {name} cannot be changed in settings: {reason}",
                            "Поле {name} нельзя менять в настройках: {reason}"),
    "config.slug_fixed": ("the project name does not change", "имя проекта не меняется"),
    "config.stage_command": ("the stage is changed by the stage command", "стадию меняет команда stage"),
    "config.session_runner": ("the project agent session is managed by the runner",
                              "сессию агента проекта ведёт раннер"),
    "config.unknown_field": ("Unknown config field: {name}", "Неизвестное поле конфига: {name}"),
    "config.positive_int": ("{name} must be an integer not less than 1", "{name} должен быть целым числом не меньше 1"),
    "config.string": ("{name} must be a string", "{name} должен быть строкой"),
    "config.nonempty_string": ("{name} must be a non-empty string", "{name} должен быть непустой строкой"),
    "config.optional_string": ("{name} must be a non-empty string or null",
                               "{name} должен быть непустой строкой или null"),
    "config.rules_too_long": ("decomposition_rules is longer than {max} characters",
                              "decomposition_rules длиннее {max} символов"),
    "config.repos_list": ("repos must be a list of repositories", "repos должен быть списком репозиториев"),
    "config.repo_object": ("A repository in repos must be an object {{name, path, base}}",
                           "Репозиторий в repos должен быть объектом {{name, path, base}}"),
    "config.repo_twice": ("Repository {name} is listed twice", "Репозиторий {name} указан дважды"),
    "config.repo_relative": ("Path of repository {name} must be absolute: {path}",
                             "Путь репозитория {name} должен быть абсолютным: {path}"),
    "config.repo_missing": ("Directory of repository {name} not found: {path}",
                            "Каталог репозитория {name} не найден: {path}"),
    "config.repo_not_git": ("Directory of repository {name} is not a git repository: {path}",
                            "Каталог репозитория {name} — не git-репозиторий: {path}"),
    "config.bad_branch_template": ("Invalid branch_template: {error}", "Некорректный branch_template: {error}"),
    "config.branch_only_key": ("Only the {{key}} placeholder is allowed in branch_template",
                               "В branch_template допустим только плейсхолдер {{key}}"),
    "config.branch_needs_key": ("branch_template must contain {{key}}: each task has its own branch",
                                "branch_template должен содержать {{key}}: у каждой задачи своя ветка"),
    "config.resources_list": ("resources must be a list of resources", "resources должен быть списком ресурсов"),
    "config.resource_object": ("A resource in resources must be an object {{name, description}} with a name of Latin "
                               "letters, digits, “-”, “_” and “.”",
                               "Ресурс в resources должен быть объектом {{name, description}} с именем из латиницы, "
                               "цифр, «-», «_» и «.»"),
    "config.resource_twice": ("Resource {name} is listed twice", "Ресурс {name} указан дважды"),
    "config.links_list": ("links must be a list of links", "links должен быть списком ссылок"),
    "config.bad_link": ("Invalid link in links: {item} — title and href (http(s) or a file path) are required",
                        "Недопустимая ссылка в links: {item} — нужны title и href (http(s) или путь к файлу)"),
    "config.review_tool": ("Unknown review tool: review.tool must be one of: {tools}",
                           "Неизвестный инструмент ревью: review.tool должен быть одним из: {tools}"),
    "config.ship_object": ("ship must be an object {{command, timeout, on_approve}}",
                           "ship должен быть объектом {{command, timeout, on_approve}}"),
    "config.harness_type": ("Unknown harness: harness.type must be one of: {types}",
                            "Неизвестный харнесс: harness.type должен быть одним из: {types}"),
    "config.allowed_tools": ("harness.allowed_tools must be a list of strings",
                             "harness.allowed_tools должен быть списком строк"),
    "config.chains": ("chains must be true or false", "chains должен быть true или false"),
    "config.tracker_object": ("tracker must be an object with a type field",
                              "tracker должен быть объектом с полем type"),
    # --- трекер ---
    "tracker.str_list": ("tracker.{name} must be a list of strings", "tracker.{name} должен быть списком строк"),
    "tracker.missing": ("tracker.{name} is not set", "Не задано tracker.{name}"),
    "tracker.http_url": ("tracker.{name} must be an http(s) address", "tracker.{name} должен быть http(s)-адресом"),
    "tracker.url_credentials": ("tracker.{name} must not contain a login and password: secrets are set via auth",
                                "tracker.{name} не должен содержать логин и пароль: секреты задаются через auth"),
    "tracker.auth_type": ("tracker.auth.type must be one of: {kinds}",
                          "tracker.auth.type должен быть одним из: {kinds}"),
    "tracker.auth_extra": ("Invalid tracker.auth keys: {names} — secrets are set only by an environment variable name "
                           "or a file path",
                           "Недопустимые ключи tracker.auth: {names} — секреты задаются только именем переменной "
                           "окружения или путём к файлу"),
    "tracker.auth_env": ("tracker.auth.env is not set — the name of the environment variable with the token",
                         "Не задано tracker.auth.env — имя переменной окружения с токеном"),
    "tracker.auth_user_env": ("tracker.auth.user_env must be an environment variable name",
                              "tracker.auth.user_env должен быть именем переменной окружения"),
    "tracker.auth_path": ("tracker.auth.path is not set — the path to the cookie file",
                          "Не задано tracker.auth.path — путь к файлу с cookie"),
    "tracker.title_template_str": ("tracker.title_template must be a string",
                                   "tracker.title_template должен быть строкой"),
    "tracker.bad_title_template": ("Invalid tracker.title_template: {error}",
                                   "Некорректный tracker.title_template: {error}"),
    "tracker.title_placeholder": ("Unknown placeholder in tracker.title_template: {{{name}}}. Allowed: {{key}}, "
                                  "{{title}}",
                                  "Неизвестный плейсхолдер в tracker.title_template: {{{name}}}. Допустимы: {{key}}, "
                                  "{{title}}"),
    "tracker.statuses_object": ("tracker.statuses must be an object “tracker status → board status”",
                                "tracker.statuses должен быть объектом «статус трекера → статус доски»"),
    "tracker.bad_status": ("Invalid board status in tracker.statuses[{name!r}]: {value!r}. Allowed: {allowed} "
                           "(only the user approves)",
                           "Недопустимый статус доски в tracker.statuses[{name!r}]: {value!r}. Допустимы: {allowed} "
                           "(одобрение ставит только пользователь)"),
    "tracker.type": ("tracker.type must be one of: {types}", "tracker.type должен быть одним из: {types}"),
    "tracker.extra_keys": ("Invalid tracker keys: {names}", "Недопустимые ключи tracker: {names}"),
    "tracker.field_str": ("tracker.{name} must be a string", "tracker.{name} должен быть строкой"),
    "tracker.search_path": ("tracker.jira_search_path must be a path from the Jira address, e.g. /rest/api/2/search",
                            "tracker.jira_search_path должен быть путём от адреса Jira, например /rest/api/2/search"),
    "tracker.extra_fields": ("tracker.extra_fields must be an object", "tracker.extra_fields должен быть объектом"),
    "tracker.repo": ("tracker.repo must look like owner/name", "tracker.repo должен быть в виде owner/name"),
    "tracker.poll_seconds": ("tracker.poll_seconds must be a positive number of seconds",
                             "tracker.poll_seconds должен быть положительным числом секунд"),
    "tracker.insecure": ("Tracker {host} is reachable over plain http: the token or cookie travels in clear text — "
                         "use https",
                         "Трекер {host} доступен по http без шифрования: токен или cookie уходят открытым текстом — "
                         "используйте https"),
    "tracker.env_missing": ("Environment variable {name} is not set (tracker {what})",
                            "Не задана переменная окружения {name} ({what} трекера)"),
    "tracker.what_token": ("token", "токен"),
    "tracker.what_user": ("user", "пользователь"),
    "tracker.where_env": ("environment variable {name}", "переменной окружения {name}"),
    "tracker.where_cookie": ("cookie file {path}", "файле с cookie {path}"),
    "tracker.bad_chars": ("Invalid characters in {where} (line breaks, control or non-Latin characters)",
                          "Недопустимые символы в {where} (переводы строк, управляющие или не латинские символы)"),
    "tracker.cookie_unreadable": ("Failed to read the tracker cookie file: {path}",
                                  "Не удалось прочитать файл с cookie трекера: {path}"),
    "tracker.cookie_empty": ("The tracker cookie file is empty: {path}", "Файл с cookie трекера пуст: {path}"),
    "tracker.hint_cookie": ("check the cookie in file {path}", "проверьте cookie в файле {path}"),
    "tracker.hint_token": ("check the token in environment variable {name}",
                           "проверьте токен в переменной окружения {name}"),
    "tracker.no_connection": ("No connection to tracker {base}: {reason}", "Нет связи с трекером {base}: {reason}"),
    "tracker.bad_request": ("Invalid request to the tracker: check the address and {hint}",
                            "Некорректный запрос к трекеру: проверьте адрес и {hint}"),
    "tracker.not_json": ("The tracker answered not in JSON ({method} {path})",
                         "Трекер вернул ответ не в JSON ({method} {path})"),
    "tracker.timeout": ("The tracker did not answer in {seconds:g} s", "Трекер не ответил за {seconds:g} с"),
    "tracker.unexpected": ("The tracker returned an unexpected answer ({method} {path})",
                           "Трекер вернул неожиданный ответ ({method} {path})"),
    "tracker.auth_rejected": ("The tracker rejected authorization (HTTP {code}): {hint}{detail}",
                              "Трекер отклонил авторизацию (HTTP {code}): {hint}{detail}"),
    "tracker.redirect": ("The tracker redirected the request (HTTP {code}) — check the tracker address and "
                         "authorization: {hint}",
                         "Трекер перенаправил запрос (HTTP {code}) — проверьте адрес трекера и авторизацию: {hint}"),
    "tracker.not_found": ("Not found in the tracker (HTTP 404){detail}", "Не найдено в трекере (HTTP 404){detail}"),
    "tracker.rejected": ("The tracker rejected the request (HTTP {code}){detail}",
                         "Трекер отклонил запрос (HTTP {code}){detail}"),
    "tracker.unavailable": ("The tracker is temporarily unavailable (HTTP {code}), try again later",
                            "Трекер временно недоступен (HTTP {code}), повторите позже"),
    "tracker.http_error": ("Tracker error (HTTP {code}){detail}", "Ошибка трекера (HTTP {code}){detail}"),
    "tracker.not_configured": ("The project has no tracker (tracker.type = none)",
                               "В проекте не настроен трекер (tracker.type = none)"),
    "tracker.jira_no_key": ("Jira did not return the key of the created issue",
                            "Jira не вернула ключ созданной задачи"),
    "tracker.jira_anonymous": ("Jira answers as to an anonymous user — the session expired or authorization was not "
                               "accepted: {hint}",
                               "Jira отвечает как анонимному пользователю — сессия истекла или авторизация не "
                               "принята: {hint}"),
    "tracker.jira_search": ("Jira returned an unexpected search answer", "Jira вернула неожиданный ответ поиска"),
    "tracker.jira_issue_missing": ("Issue {key} not found in Jira (deleted, moved or no access)",
                                   "Задача {key} не найдена в Jira (удалена, перенесена или нет доступа)"),
    "tracker.jira_project_missing": ("Project {project} not found in Jira or no access to it",
                                     "Проект {project} не найден в Jira или нет доступа к нему"),
    "tracker.github_bad_key": ("Invalid GitHub issue key: {key!r}", "Некорректный ключ задачи GitHub: {key!r}"),
    "tracker.github_no_number": ("GitHub did not return the number of the created issue",
                                 "GitHub не вернул номер созданной задачи"),
    "tracker.github_no_state": ("GitHub did not return the state of issue {key}",
                                "GitHub не вернул состояние задачи {key}"),
    "tracker.github_repo_missing": ("Repository {repo} not found on GitHub or no access to it",
                                    "Репозиторий {repo} не найден на GitHub или нет доступа к нему"),
    "tracker.check_user_only": ("Only the user checks tracker access",
                                "Проверяет доступ к трекеру только пользователь"),
    "tracker.create_user_only": ("Only the user creates tasks in the tracker",
                                 "Заводит задачи в трекере только пользователь"),
    "tracker.sync_user_only": ("Only the user syncs tracker statuses",
                               "Синхронизирует статусы трекера только пользователь"),
    "tracker.wrong_stage": ("Tasks are created in the tracker at the tracker stage (now {stage})",
                            "Задачи заводятся в трекере на стадии tracker (сейчас {stage})"),
    "tracker.creating": ("Tasks are already being created in the tracker", "Задачи уже заводятся в трекере"),
    "tracker.dep_not_created": ("{key} is not created: {missing} must be created first",
                                "{key} не заведена: сначала нужно завести {missing}"),
    "tracker.bad_issue_url": ("The tracker returned a non-http(s) link to issue {key}: the link was not saved",
                              "Трекер вернул ссылку на задачу {key} не по http(s): ссылка не сохранена"),
    "tracker.sync_stage": ("Statuses are synced at the work and done stages (now {stage})",
                           "Статусы синхронизируются на стадиях work и done (сейчас {stage})"),
    "tracker.sync_internal": ("Internal sync error", "Внутренняя ошибка синхронизации"),
    "tracker.link_failed": ("Link {dep} → {key}: {error}", "Связь {dep} → {key}: {error}"),
    # --- системные сообщения в тредах и журнале (язык — из настроек проекта) ---
    "board.applied_proposal": ("Applied proposal {id}: {change}", "Применено предложение {id}: {change}"),
    "board.applied_add_task": ("added task {key}", "добавлена задача {key}"),
    "board.applied_set_deps": ("dependencies of task {key}: {deps}", "зависимости задачи {key}: {deps}"),
    "board.applied_no_deps": ("none", "нет"),
    "board.applied_cancel_task": ("cancelled task {key}", "отменена задача {key}"),
    "board.retry_user_only": ("Only the user retries an agent turn", "Повторить ход агента может только пользователь"),
    "board.retry_status": ("The task agent does not work at status {status}, nothing to retry",
                           "Агент задачи на статусе {status} не работает, повторять нечего"),
    "board.retry_stage": ("Task agents work only at the work stage", "Агенты задач работают только на стадии work"),
    "board.retry_not_failed": ("Only a failed turn can be retried: agent {key} is now in state {state}",
                               "Повторить можно только упавший ход: агент {key} сейчас в состоянии {state}"),
    "board.rebase_actor": ("Only the user or the project agent can move a task onto its base",
                           "Перенести задачу на базу может только пользователь или агент проекта"),
    "board.rebase_status": ("A task at status {status} is not moved onto its base: its work is already accepted "
                            "or cancelled",
                            "Задача на статусе {status} не переносится на базу: её работа уже принята или отменена"),
    "board.rebase_no_worktree": ("The task has no working copy yet: the agent will start right from the fresh base",
                                 "У задачи ещё нет рабочей копии: агент начнёт работу сразу от свежей базы"),
    "run.leftover_missing": ("The old turn process was not found (it exited or its pid belongs to another process), "
                             "leaving it alone.",
                             "Старый процесс хода не найден (завершился или pid занят другим процессом), его не трогаем."),
    "run.interrupted": ("The turn was interrupted by a server restart, the agent will continue with a note about it.",
                        "Ход прерван перезапуском сервера, агент продолжит с сообщением о прерывании."),
    "run.failures": ("The agent failed {count} times in a row, automatic restart stopped: {error}. "
                     "Retry the turn with agent-board retry {key} — the agent continues where it stopped; "
                     "or write a message or change the status.",
                     "Агент упал {count} раза подряд, автоматический перезапуск остановлен: {error}. "
                     "Повторите ход командой agent-board retry {key} — агент продолжит с места остановки; "
                     "или напишите сообщение либо смените статус."),
    "run.carried": ("The task branch was moved onto {base}, but the uncommitted work could not be restored to the files "
                    "without a conflict. It is saved in git at {ref}: restore it manually (for example, "
                    "git stash apply {ref}), then delete the ref with git update-ref -d {ref}.",
                    "Ветка задачи перенесена на {base}, но незакоммиченную работу не удалось вернуть в файлы без "
                    "конфликта. Она сохранена в git по ссылке {ref}: верните её вручную (например, git stash apply "
                    "{ref}), затем удалите ссылку командой git update-ref -d {ref}."),
    "run.agents_loop": ("Agents are talking to each other without the user — stopped; reply in the thread to continue",
                        "Агенты переписываются без участия пользователя — остановлено; ответьте в треде, чтобы "
                        "продолжить"),
    "run.lost_session": ("The harness did not find agent session {session}, the agent will start the task over in a "
                         "new session.",
                         "Сессия агента {session} не найдена у харнесса, агент начнёт задачу заново в новой сессии."),
    "run.runner_error": ("Runner error: {error}", "Ошибка раннера: {error}"),
    "run.internal_error": ("runner error: {error}", "ошибка раннера: {error}"),
    "run.commit_failed": ("Failed to commit the approved work, the task is back in review: {error}",
                          "Не удалось закоммитить одобренную работу, задача возвращена на ревью: {error}"),
    "run.not_started": ("The agent was not started: {error}", "Агент не запущен: {error}"),
    "run.worktree_check_failed": ("failed to check worktree {path}: {error}",
                                  "не удалось проверить worktree {path}: {error}"),
    "run.worktree_mismatch": ("worktree {path} does not match the expected {expected}",
                              "worktree {path} не совпадает с ожидаемым {expected}"),
    "run.no_worktree": ("the task has a session but no worktree", "у задачи есть сессия, но нет worktree"),
    "run.repo_missing": ("repository {repo} is not in the project settings",
                         "репозиторий {repo} не найден в настройках проекта"),
    "run.repo_ambiguous": ("the task has no repository and the project has {count}",
                           "у задачи не указан репозиторий, а в проекте их {count}"),
    "run.worktree_failed": ("failed to prepare the worktree: {error}", "не удалось подготовить worktree: {error}"),
    "run.rebase_failed": ("Failed to move the work onto {base}: {error}", "Не удалось перенести работу на {base}: {error}"),
    "run.turn_failed": ("the turn ended with an error", "ход завершился с ошибкой"),
    "run.review_approved": ("Approved in review (round {round})", "Одобрено в ревью (раунд {round})"),
    "run.review_failed": ("Review was not started: {error}", "Ревью не запущено: {error}"),
    "run.review_aborted": ("Review round {round} was interrupted: {error}. Open the review again.",
                           "Раунд ревью {round} прерван: {error}. Откройте ревью снова."),
    "run.review_during_turn": ("The review finished during an agent turn — approve manually",
                               "Ревью завершено во время хода агента — одобрите вручную"),
    "run.review_not_in_review": ("Review round {round} finished without comments, but the task is no longer in review "
                                 "({status}): approve it manually when it is ready",
                                 "Раунд ревью {round} завершён без замечаний, но задача уже не на ревью ({status}): "
                                 "одобрите её вручную, когда она будет готова"),
    "run.review_tool_off": ("The review tool is turned off in the project: open round {round} closed",
                            "Инструмент ревью в проекте отключён: открытый раунд {round} закрыт"),
    "run.ship_interrupted": ("Sending to review was interrupted by a server restart. The command may not have "
                             "finished: check the result and send the task again if needed.",
                             "Отправка в ревью прервана перезапуском сервера. Команда могла выполниться не до конца: "
                             "проверьте результат и при необходимости отправьте задачу снова."),
    "run.ship_not_started": ("Sending to review was not started because of a server restart — send the task again.",
                             "Отправка в ревью не была запущена из-за перезапуска сервера — отправьте задачу снова."),
    "run.ship_failed": ("Sending to review failed (exit code {code}).{output}",
                        "Отправка в ревью не удалась (код выхода {code}).{output}"),
    "run.ship_timeout": ("Sending to review timed out ({seconds} s), the command processes were stopped.{output}",
                         "Отправка в ревью прервана по таймауту ({seconds} с), процессы команды остановлены.{output}"),
    "run.ship_error": ("Sending to review failed: {error}", "Отправка в ревью не выполнена: {error}"),
    "run.ship_committed": ("Remaining changes were committed before sending ({sha}).",
                           "Перед отправкой закоммичены оставшиеся изменения ({sha})."),
    "run.ship_cancelled": ("The task was cancelled, sending to review stopped.",
                           "Задача отменена, отправка в ревью остановлена."),
    "run.ship_auto_error": ("Automatic sending to review after approval was not started: {error}",
                            "Автоматическая отправка в ревью после одобрения не запущена: {error}"),
    "run.ship_multi_deps": ("several unfinished dependencies — send them first",
                            "несколько незавершённых зависимостей — отправьте их сначала"),
    "run.ship_dep_no_branch": ("dependency {key} has no branch", "у зависимости {key} нет ветки"),
    "run.ship_dep_failed": ("dependency {keys} was not sent", "зависимость {keys} не отправлена"),
    "run.ship_not_approved": ("the task is no longer approved ({status})", "задача уже не одобрена ({status})"),
    "run.ship_no_branch": ("the task has no branch", "у задачи нет ветки"),
    "run.ship_start_failed": ("failed to start the command: {error}", "не удалось запустить команду: {error}"),
    "run.ship_status_changed": ("the send command succeeded, but the task is no longer approved — status unchanged",
                                "команда отправки выполнена, но задача уже не одобрена — статус не изменён"),
    "ship.output_failed": ("failed to start output recording: {error}", "не удалось запустить запись вывода: {error}"),
    "harness.not_found": ("{command} not found in PATH", "{command} не найден в PATH"),
    "harness.start_failed": ("failed to start {command}: {error}", "не удалось запустить {command}: {error}"),
    "harness.exit_code": ("the process exited with code {code}", "процесс завершился с кодом {code}"),
    "harness.prompt_failed": ("failed to write the system prompt: {error}",
                              "не удалось записать системный промпт: {error}"),
    "harness.session_mismatch": ("session id in the stream ({got}) differs from the one passed ({expected})",
                                 "id сессии в потоке ({got}) отличается от переданного ({expected})"),
    "harness.agent_error": ("agent error", "ошибка агента"),
    "harness.unknown": ("Unknown harness: {kind}", "Неизвестный харнесс: {kind}"),
    "review.not_found": ("{binary} not found", "{binary} не найден"),
    "review.start_failed": ("failed to start {binary}: {error}", "не удалось запустить {binary}: {error}"),
    "review.exit_code": ("crit exited with code {code}", "crit завершился с кодом {code}"),
    "review.comments_unreadable": ("crit did not approve the round, but its comments could not be read",
                                   "crit не одобрил раунд, но прочитать его комментарии не удалось"),
    "review.comments_bad_json": ("crit comments: invalid JSON: {error}", "crit comments: некорректный JSON: {error}"),
    "review.comments_not_list": ("crit comments: a list was expected", "crit comments: ожидался список"),
    "review.unknown_tool": ("Unknown review tool: {tool}", "Неизвестный инструмент ревью: {tool}"),
}


class Msg(str):
    """Сообщение каталога: как строка — на языке процесса, render(lang) — на любом другом."""

    def __new__(cls, key: str, params: Dict[str, Any]) -> "Msg":
        text = _render(key, current_language(), params)
        obj = super().__new__(cls, text)
        obj.key = key
        obj.params = params
        return obj

    def render(self, lang: str) -> str:
        return _render(self.key, lang, self.params)

    def __reduce__(self):
        return (_restore, (self.key, self.params))


def _restore(key: str, params: Dict[str, Any]) -> Msg:
    return Msg(key, params)


def msg(key: str, /, **params: Any) -> Msg:
    return Msg(key, params)


def normalize(value: Optional[str]) -> str:
    """Язык из настройки или заголовка: ru, ru-RU, «русский» — русский, всё прочее — английский."""
    value = (value or "").strip().lower()
    return "ru" if value.startswith(("ru", "рус")) else DEFAULT_LANGUAGE


def current_language() -> str:
    return normalize(os.environ.get(LANG_ENV))


def _render(key: str, lang: str, params: Dict[str, Any]) -> str:
    template = CATALOG[key][LANGUAGES.index(lang) if lang in LANGUAGES else 0]
    return template.format(**{k: localize(v, lang) for k, v in params.items()})


def localize(value: Any, lang: str) -> Any:
    """Переводит сообщения каталога внутри значения (в том числе вложенные в словари и списки) на язык lang."""
    if isinstance(value, Msg):
        return value.render(lang)
    if isinstance(value, dict):
        return {k: localize(v, lang) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [localize(v, lang) for v in value]
    return value


def clip(text: Any, limit: int) -> Any:
    """Обрезает произвольный текст; сообщения каталога и так короткие и остаются переводимыми."""
    return text if isinstance(text, Msg) else text[:limit]


def reason(error: BaseException) -> Any:
    """Текст исключения; сообщение каталога сохраняется, чтобы его можно было перевести."""
    if len(error.args) == 1 and isinstance(error.args[0], Msg):
        return error.args[0]
    if isinstance(error, KeyError) and len(error.args) == 1:
        return str(error.args[0])
    return str(error)
