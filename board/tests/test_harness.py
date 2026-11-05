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

def test_claude_start_args():
    # arrange
    h = ClaudeHarness("plan", model="opus", system_prompt="SYS")
    # act
    args = h.build_start_args("do it", "sid-1")
    # assert
    assert args == [
        "claude", "-p", "--session-id", "sid-1", "--output-format", "stream-json", "--verbose",
        "--permission-mode", "plan", "--model", "opus", "--append-system-prompt", "SYS", "do it",
    ]


def test_claude_start_args_without_model():
    args = ClaudeHarness("auto", system_prompt="S").build_start_args("p", "sid")
    assert "--model" not in args and args[-1] == "p"


def test_claude_resume_args():
    args = ClaudeHarness("auto").build_resume_args("sid-1", "continue")
    assert args == [
        "claude", "-p", "--resume", "sid-1", "--output-format", "stream-json", "--verbose",
        "--permission-mode", "auto", "continue",
    ]


def test_codex_start_args(tmp_path):
    args = CodexHarness(model="m1").build_start_args(tmp_path, "do it")
    assert args == ["codex", "exec", "--json", "-C", str(tmp_path), "--sandbox", "workspace-write", "--model", "m1", "do it"]


def test_codex_resume_args(tmp_path):
    args = CodexHarness().build_resume_args(tmp_path, "tid", "more")
    assert args == ["codex", "exec", "--json", "-C", str(tmp_path), "resume", "tid", "more"]


def test_terminal_commands(tmp_path):
    assert ClaudeHarness("auto").terminal_command(tmp_path, "s1") == "cd %s && claude --resume s1" % tmp_path
    assert CodexHarness().terminal_command(tmp_path, "s1") == "cd %s && codex resume s1" % tmp_path


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
    res = ClaudeHarness("auto").finish(cat_proc("claude_stream.jsonl", hint="hint-sid"), log)
    # assert
    assert res == TurnResult(session_id=CLAUDE_SID, ok=True)
    assert log.read_text() == (FIXTURES / "claude_stream.jsonl").read_text()


def test_claude_finish_uses_hint_without_init_event(tmp_path):
    res = ClaudeHarness("auto").finish(cat_proc("codex_stream.jsonl", hint="hint-sid"), tmp_path / "l")
    assert res.session_id == "hint-sid"


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


def test_finish_handles_large_output(tmp_path):
    # arrange: вывод заметно больше буфера пайпа
    code = "import sys\nfor i in range(20000): print('x' * 100)"
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    # act
    res = CodexHarness().finish(proc, tmp_path / "l")
    # assert
    assert res.ok is True and len((tmp_path / "l").read_text().splitlines()) == 20000


# --- отсутствующий бинарь ---

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
