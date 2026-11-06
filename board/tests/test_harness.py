from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_board.cli import main
from agent_board.harness import (
    ClaudeHarness,
    CodexHarness,
    FakeHarness,
    TurnResult,
    make_harness,
)
from agent_board.server import BoardServer

FIXTURES = Path(__file__).parent / "fixtures"
CLAUDE_SID = "106d935a-53e5-4157-992b-1f9195627674"
CODEX_TID = "019a5c1e-1111-7222-8333-444455556666"


def cat_proc(name, hint=None, exit_code=0):
    """Процесс, который выдаёт фикстуру как поток харнесса."""
    script = "import sys; sys.stdout.write(open(sys.argv[1]).read()); sys.exit(%d)" % exit_code
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(FIXTURES / name)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    proc.session_hint = hint
    return proc


# --- сборка командных строк ---

BOARD_RULES = [
    "Bash(agent-board status:*)", "Bash(agent-board update:*)", "Bash(agent-board ask:*)",
    "Bash(agent-board say:*)", "Bash(agent-board show:*)", "Bash(agent-board thread:*)",
    "Bash(agent-board state:*)", "Bash(agent-board events:*)", "Bash(agent-board propose:*)",
]


def test_claude_start_args(tmp_path):
    # arrange
    h = ClaudeHarness("plan", model="opus", system_prompt="SYS")
    # act
    args = h.build_start_args("sid-1", tmp_path / "s.md")
    # assert
    assert args == [
        "claude", "-p", "--session-id", "sid-1", "--output-format", "stream-json", "--verbose",
        "--permission-mode", "plan", "--allowedTools", *BOARD_RULES,
        "--model", "opus", "--append-system-prompt-file", str(tmp_path / "s.md"),
    ]


def test_claude_allowed_tools_do_not_include_lock_run(tmp_path):
    # arrange / act
    args = ClaudeHarness("auto").build_start_args("sid", tmp_path / "s.md")
    # assert
    assert not any("lock-run" in a or a == "Bash(agent-board:*)" for a in args)


def test_claude_start_args_without_model(tmp_path):
    assert "--model" not in ClaudeHarness("auto").build_start_args("sid", tmp_path / "s.md")


def test_claude_resume_args(tmp_path):
    # arrange / act
    args = ClaudeHarness("auto").build_resume_args("sid-1", tmp_path / "s.md")
    # assert
    assert args == [
        "claude", "-p", "--resume", "sid-1", "--output-format", "stream-json", "--verbose",
        "--permission-mode", "auto", "--allowedTools", *BOARD_RULES,
        "--append-system-prompt-file", str(tmp_path / "s.md"),
    ]


def test_claude_resume_rewrites_system_prompt_file(tmp_path, monkeypatch):
    # arrange
    h = ClaudeHarness("auto", system_prompt="старый")
    spawned = []
    monkeypatch.setattr(ClaudeHarness, "_spawn", lambda self, args, cwd, env, hint, stdin_text="": spawned.append(args))
    log = tmp_path / "logs" / "T-1.jsonl"
    h.start_turn(tmp_path, "p", "sid-1", log, {})
    # act
    h.system_prompt = "новый"
    h.resume_turn(tmp_path, "sid-1", "m", log, {})
    # assert
    system_file = spawned[1][spawned[1].index("--append-system-prompt-file") + 1]
    assert Path(system_file).read_text(encoding="utf-8") == "новый"


def test_codex_start_args():
    args = CodexHarness(model="m1").build_start_args()
    assert args == ["codex", "exec", "--json", "--sandbox", "workspace-write",
                    "-c", "sandbox_workspace_write.network_access=true", "--model", "m1", "-"]


def test_codex_resume_args_keep_sandbox_before_subcommand():
    args = CodexHarness(sandbox="read-only").build_resume_args("tid")
    assert args == ["codex", "exec", "--json", "--sandbox", "read-only",
                    "-c", "sandbox_workspace_write.network_access=true", "resume", "tid", "-"]


def test_terminal_commands(tmp_path):
    assert ClaudeHarness("auto").terminal_command(tmp_path, "s1") == "cd %s && claude --resume s1" % tmp_path
    assert CodexHarness().terminal_command(tmp_path, "s1") == "cd %s && codex resume s1" % tmp_path


def test_terminal_command_quotes_cwd():
    cmd = ClaudeHarness("auto").terminal_command(Path("/a b/c"), "s1")
    assert cmd == "cd '/a b/c' && claude --resume s1"


def test_new_session_id():
    assert len(ClaudeHarness("auto").new_session_id()) == 36
    assert CodexHarness().new_session_id() is None


def test_make_harness():
    assert isinstance(make_harness({"type": "claude", "permission_mode": "plan"}), ClaudeHarness)
    assert isinstance(make_harness({"type": "codex"}), CodexHarness)
    assert isinstance(make_harness({"type": "fake"}), FakeHarness)
    assert make_harness({"type": "claude"}).permission_mode == "auto"
    with pytest.raises(ValueError):
        make_harness({"type": "gpt"})


# --- разбор потоков ---

def test_claude_finish_success_writes_log(tmp_path):
    # arrange
    log = tmp_path / "logs" / "T1.jsonl"
    # act
    res = ClaudeHarness("auto").finish(cat_proc("claude_stream.jsonl", hint=CLAUDE_SID), log)
    # assert
    assert res == TurnResult(session_id=CLAUDE_SID, ok=True)
    assert log.read_text() == (FIXTURES / "claude_stream.jsonl").read_text()


def test_claude_finish_uses_hint_without_init_event(tmp_path):
    res = ClaudeHarness("auto").finish(cat_proc("codex_stream.jsonl", hint="hint-sid"), tmp_path / "l")
    assert res.session_id == "hint-sid"


def test_claude_finish_notes_session_id_mismatch(tmp_path):
    res = ClaudeHarness("auto").finish(cat_proc("claude_stream.jsonl", hint="other"), tmp_path / "l")
    assert res.ok is True and res.session_id == "other" and "other" in res.note and CLAUDE_SID in res.note


def test_claude_finish_error_event(tmp_path):
    res = ClaudeHarness("auto").finish(cat_proc("claude_error.jsonl"), tmp_path / "l")
    assert res.ok is False and "rate limit" in res.error and res.session_id == CLAUDE_SID


def test_claude_finish_nonzero_exit_uses_output_tail(tmp_path):
    res = ClaudeHarness("auto").finish(cat_proc("codex_stream.jsonl", exit_code=3), tmp_path / "l")
    assert res.ok is False and res.exit_code == 3 and res.error


def test_log_is_appended_between_turns(tmp_path):
    log = tmp_path / "l"
    h = ClaudeHarness("auto")
    h.finish(cat_proc("claude_stream.jsonl"), log)
    h.finish(cat_proc("claude_stream.jsonl"), log)
    assert len(log.read_text().splitlines()) == 6


def test_codex_finish_success(tmp_path):
    res = CodexHarness().finish(cat_proc("codex_stream.jsonl"), tmp_path / "l")
    assert res == TurnResult(session_id=CODEX_TID, ok=True)


def test_codex_finish_turn_failed(tmp_path):
    res = CodexHarness().finish(cat_proc("codex_error.jsonl"), tmp_path / "l")
    assert res.ok is False and res.error == "stream disconnected" and res.session_id == CODEX_TID


def test_codex_turn_completed_clears_earlier_error(tmp_path):
    # arrange
    (tmp_path / "f.jsonl").write_text(
        '{"type":"thread.started","thread_id":"t"}\n{"type":"error","message":"reconnecting"}\n{"type":"turn.completed"}\n'
    )
    script = "import sys; sys.stdout.write(open(sys.argv[1]).read())"
    proc = subprocess.Popen([sys.executable, "-c", script, str(tmp_path / "f.jsonl")], stdout=subprocess.PIPE, text=True)
    # act
    res = CodexHarness().finish(proc, tmp_path / "l")
    # assert
    assert res.ok is True and res.error == ""


def test_finish_kills_process_group_on_exception(tmp_path):
    # arrange
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; print('x', flush=True); time.sleep(60)"],
        stdout=subprocess.PIPE, text=True, start_new_session=True,
    )
    bad_log = tmp_path / "dir"
    bad_log.mkdir()
    # act: лог — каталог, открыть его нельзя
    with pytest.raises(OSError):
        CodexHarness().finish(proc, bad_log)
    # assert
    assert proc.poll() is not None


def test_finish_handles_large_output(tmp_path):
    # arrange: вывод заметно больше буфера пайпа
    code = "import sys\nfor i in range(20000): print('x' * 100)"
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    # act
    res = CodexHarness().finish(proc, tmp_path / "l")
    # assert
    assert res.ok is True and len((tmp_path / "l").read_text().splitlines()) == 20000


# --- отсутствующий бинарь ---

def test_oserror_on_spawn_gives_failed_result(tmp_path):
    # arrange: cwd не существует
    h = CodexHarness()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "codex").write_text("#!/bin/sh\n")
    (bin_dir / "codex").chmod(0o755)
    # act
    proc = h.start_turn(tmp_path / "nope", "p", None, tmp_path / "l", {"PATH": str(bin_dir)})
    res = h.finish(proc, tmp_path / "l")
    # assert
    assert res.ok is False and "не удалось запустить" in res.error


@pytest.mark.parametrize("harness,name", [(ClaudeHarness("auto"), "claude"), (CodexHarness(), "codex")])
def test_missing_binary_gives_failed_result(harness, name, tmp_path):
    # arrange
    env = {"PATH": str(tmp_path)}
    log = tmp_path / "l"
    # act
    proc = harness.start_turn(tmp_path, "p", harness.new_session_id(), log, env)
    res = harness.finish(proc, log)
    proc2 = harness.resume_turn(tmp_path, "sid", "m", log, env)
    res2 = harness.finish(proc2, log)
    # assert
    assert res.ok is False and res.error == "%s не найден в PATH" % name
    assert harness.finish(harness.resume_turn(tmp_path, "sid", "m", log, env), log).session_id == "sid"
    assert res2.ok is False and res2.error == res.error


# --- fake ---

@pytest.fixture
def srv(tmp_home):
    assert main(["init", "pay", "--title", "Платежи"]) == 0
    server = BoardServer(port=0)
    server.start()
    yield server
    server.stop()


def fake_env(tmp_home, **extra):
    env = dict(AGENT_BOARD_HOME=str(tmp_home), AGENT_BOARD_PROJECT="pay", AGENT_BOARD_ACTOR="agent", AGENT_BOARD_TASK="T1")
    env.update(extra)
    return env


def script(*steps):
    return "Сделай задачу. FAKE-SCRIPT: " + json.dumps(list(steps))


def test_fake_script_changes_status_and_writes_file(srv, tmp_home, tmp_path, monkeypatch):
    # arrange
    monkeypatch.setenv("AGENT_BOARD_PROJECT", "pay")
    main(["add-task", "--json", '{"key": "T1", "title": "x"}'])
    h = FakeHarness()
    log = tmp_path / "logs" / "T1.jsonl"
    prompt = script({"cli": ["status", "T1", "in_progress"]}, {"write": {"path": "out/a.txt", "text": "hi"}}, {"sleep": 0.01})
    # act
    proc = h.start_turn(tmp_path, prompt, None, log, fake_env(tmp_home))
    res = h.finish(proc, log)
    # assert
    assert res.ok is True and res.session_id
    assert (tmp_path / "out" / "a.txt").read_text() == "hi"
    assert srv.hub.board("pay").snapshot()["tasks"][0]["status"] == "in_progress"


def test_fake_lock_run_step_runs_command_under_resource(srv, tmp_home, tmp_path):
    # arrange
    from agent_board.store import ProjectStore

    store = ProjectStore("pay")
    config = store.load_config()
    config.resources = [{"name": "tests", "description": ""}]
    store.save_config(config)
    main(["--project", "pay", "add-task", "--json", '{"key": "T1", "title": "x"}'])
    h = FakeHarness()
    log = tmp_path / "logs" / "T1.jsonl"
    command = [sys.executable, "-c", "open('ran.txt', 'w').write('ok')"]
    prompt = script({"lock-run": ["tests", "--"] + command})
    # act
    res = h.finish(h.start_turn(tmp_path, prompt, None, log, fake_env(tmp_home)), log)
    # assert
    assert res.ok is True
    assert (tmp_path / "ran.txt").read_text() == "ok"
    types = [e.type for e in store.read_events()]
    assert types.count("lock_acquired") == 1 and types.count("lock_released") == 1


def test_fake_resume_keeps_session_id(tmp_home, tmp_path):
    h = FakeHarness()
    log = tmp_path / "l"
    res = h.finish(h.resume_turn(tmp_path, "sess-9", script({"sleep": 0}), log, fake_env(tmp_home)), log)
    assert res.ok is True and res.session_id == "sess-9"


def test_fake_fail_step(tmp_home, tmp_path):
    h = FakeHarness()
    log = tmp_path / "l"
    proc = h.start_turn(tmp_path, script({"fail": "бум"}), "sid", log, fake_env(tmp_home))
    res = h.finish(proc, log)
    assert res.ok is False and res.error == "бум" and res.exit_code == 1


def test_fake_failing_cli_step_fails_turn(tmp_home, tmp_path):
    h = FakeHarness()
    log = tmp_path / "l"
    # сервер не запущен: CLI вернёт ненулевой код
    proc = h.start_turn(tmp_path, script({"cli": ["status", "T1", "in_progress"]}), "sid", log, fake_env(tmp_home))
    res = h.finish(proc, log)
    assert res.ok is False and res.error


def test_start_in_own_process_group(tmp_home, tmp_path):
    h = FakeHarness()
    log = tmp_path / "l"
    proc = h.start_turn(tmp_path, script({"sleep": 0.05}), "sid", log, fake_env(tmp_home))
    try:
        assert os.getpgid(proc.pid) == proc.pid != os.getpgid(0)
    finally:
        h.finish(proc, log)


def test_fake_prompt_goes_via_stdin_not_argv(tmp_home, tmp_path):
    # arrange
    h = FakeHarness()
    secret = "СЕКРЕТНЫЙ-ТЕКСТ"
    prompt = secret + " " + script({"sleep": 0.3})
    # act
    proc = h.start_turn(tmp_path, prompt, "sid", tmp_path / "l", fake_env(tmp_home))
    try:
        cmdline = subprocess.run(["ps", "-o", "command=", "-p", str(proc.pid)], capture_output=True, text=True).stdout
    finally:
        res = h.finish(proc, tmp_path / "l")
    # assert
    assert secret not in cmdline and res.ok is True


def test_fake_handles_2mb_prompt(tmp_home, tmp_path):
    h = FakeHarness()
    prompt = "x" * (2 * 1024 * 1024) + " " + script({"write": {"path": "a.txt", "text": "ok"}})
    res = h.finish(h.start_turn(tmp_path, prompt, "sid", tmp_path / "l", fake_env(tmp_home)), tmp_path / "l")
    assert res.ok is True and (tmp_path / "a.txt").read_text() == "ok"


def test_fake_message_starting_with_dash_arrives_verbatim(tmp_home, tmp_path):
    h = FakeHarness()
    msg = "--help me " + script({"write": {"path": "a.txt", "text": "ok"}})
    res = h.finish(h.resume_turn(tmp_path, "sid", msg, tmp_path / "l", fake_env(tmp_home)), tmp_path / "l")
    assert res.ok is True and (tmp_path / "a.txt").exists()


def test_fake_ignores_text_after_script(tmp_home, tmp_path):
    h = FakeHarness()
    msg = script({"write": {"path": "a.txt", "text": "ok"}}) + "\n\nвторое сообщение пользователя"
    res = h.finish(h.resume_turn(tmp_path, "sid", msg, tmp_path / "l", fake_env(tmp_home)), tmp_path / "l")
    assert res.ok is True and (tmp_path / "a.txt").exists()


def test_fake_cli_output_not_in_event_stream(srv, tmp_home, tmp_path, monkeypatch):
    # arrange
    monkeypatch.setenv("AGENT_BOARD_PROJECT", "pay")
    main(["add-task", "--json", '{"key": "T1", "title": "x"}'])
    h = FakeHarness()
    log = tmp_path / "l"
    # act
    res = h.finish(h.start_turn(tmp_path, script({"cli": ["show", "T1"]}), "sid", log, fake_env(tmp_home)), log)
    # assert
    assert res.ok is True
    assert all(json.loads(l).get("type") in ("system", "result") for l in log.read_text().splitlines())


def test_claude_start_writes_system_file_and_pipes_prompt(tmp_path):
    # arrange: поддельный claude, который печатает свой stdin и аргументы как result
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(
        "#!%s\nimport sys, json\nprint(json.dumps({'type': 'result', 'is_error': False, 'result': sys.stdin.read()}))\n"
        % sys.executable
    )
    fake.chmod(0o755)
    h = ClaudeHarness("auto", system_prompt="SYS")
    log = tmp_path / "logs" / "T1.jsonl"
    # act
    proc = h.start_turn(tmp_path, "--привет", "sid", log, {"PATH": str(bin_dir)})
    res = h.finish(proc, log)
    # assert
    assert res.ok is True and json.loads(log.read_text())["result"] == "--привет"
    assert (tmp_path / "logs" / "T1.jsonl.system.md").read_text() == "SYS"


def test_claude_extra_allowed_tools_from_config_go_after_board_rules(tmp_path):
    # arrange
    from agent_board.harness import make_harness

    h = make_harness({"type": "claude", "permission_mode": "default", "allowed_tools": ["Bash(crit comment:*)"]})
    # act
    start = h.build_start_args("sid", tmp_path / "s.md")
    resume = h.build_resume_args("sid", tmp_path / "s.md")
    # assert
    for args in (start, resume):
        i = args.index("--allowedTools")
        assert args[i + 1:i + 1 + len(BOARD_RULES) + 1] == [*BOARD_RULES, "Bash(crit comment:*)"]


def test_runner_harness_allows_crit_comment_only_for_crit_review(tmp_home):
    # arrange
    from agent_board.board import Board
    from agent_board.config import ProjectConfig
    from agent_board.runner import Runner
    from agent_board.store import ProjectStore

    store = ProjectStore("pay")
    store.create(ProjectConfig(slug="pay", title="p", harness={"type": "claude"}))
    board = Board(store)

    def tools(review):
        board.update_config({"review": review})
        return Runner._harness(board).build_start_args("sid", tmp_home / "s.md")

    # act / assert
    assert "Bash(crit comment:*)" in tools({"tool": "crit"})
    assert "Bash(/opt/crit comment:*)" in tools({"tool": "crit", "binary": "/opt/crit"})
    assert not any("crit" in a for a in tools({"tool": "none"}))


# --- сигналы группе процессов после завершения лидера ---

@pytest.fixture
def killpg_calls(monkeypatch):
    """Записывает сигналы группам процессов; проверка существования (сигнал 0) проходит как есть."""
    real = os.killpg
    calls = []

    def record(pgid, sig):
        calls.append((pgid, sig))
        return real(pgid, sig)

    monkeypatch.setattr(os, "killpg", record)
    return calls


def _sent(calls, pgid):
    return [sig for p, sig in calls if p == pgid and sig != 0]


def test_signal_group__reaped_leader_and_empty_group__sends_nothing(killpg_calls):
    # arrange: номер группы собранного лидера без участников мог занять чужой процесс
    from agent_board.harness import signal_group
    import signal

    proc = subprocess.Popen(["true"], start_new_session=True)
    proc.wait()
    # act
    signal_group(proc, signal.SIGKILL)
    # assert
    assert _sent(killpg_calls, proc.pid) == []


def test_signal_group__reaped_leader_with_live_member__kills_group(killpg_calls):
    # arrange
    from agent_board.harness import signal_group
    import signal
    import time

    proc = subprocess.Popen(["bash", "-c", "sleep 30 & echo $!"], start_new_session=True, stdout=subprocess.PIPE,
                            text=True)
    child = int(proc.stdout.readline())
    proc.wait()
    proc.stdout.close()
    # act
    signal_group(proc, signal.SIGKILL)
    # assert
    assert _sent(killpg_calls, proc.pid) == [signal.SIGKILL]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("участник группы не завершён")


def test_terminate__already_reaped_leader__sends_nothing(killpg_calls):
    # arrange
    from agent_board.harness import terminate

    proc = subprocess.Popen(["true"], start_new_session=True)
    proc.wait()
    # act
    terminate(proc, 1.0)
    # assert
    assert _sent(killpg_calls, proc.pid) == []


def test_kill_round__reaped_review_client__sends_nothing(killpg_calls):
    # arrange
    from agent_board.review import kill_round

    proc = subprocess.Popen(["true"], start_new_session=True)
    proc.wait()
    # act
    kill_round(proc)
    # assert
    assert _sent(killpg_calls, proc.pid) == []
