from __future__ import annotations

import pytest

from agent_board import ship
from agent_board.config import ProjectConfig
from agent_board.model import Status
from conftest import make_task


def cfg(**ship_cfg):
    return ProjectConfig(slug="pay", title="P", ship=ship_cfg)


def test_settings_without_command_is_error():
    # arrange, act, assert
    for value in (None, "", "  "):
        with pytest.raises(ValueError, match="команда отправки"):
            ship.settings(cfg(command=value))


def test_settings_defaults_timeout_to_600():
    # arrange, act
    command, timeout = ship.settings(cfg(command="echo {key}"))
    # assert
    assert (command, timeout) == ("echo {key}", 600.0)


@pytest.mark.parametrize("timeout", [0, -5, "10", True])
def test_settings_rejects_bad_timeout(timeout):
    # arrange, act, assert
    with pytest.raises(ValueError, match="timeout"):
        ship.settings(cfg(command="echo", timeout=timeout))


@pytest.mark.parametrize("command", ["echo {nope}", "echo {key.__class__}", "echo {key[0]}", "echo {key!r}",
                                     "echo {key:>5}", "echo {}", "echo {key"])
def test_validate_template_rejects_unknown_or_fancy_placeholders(command):
    # arrange, act, assert
    with pytest.raises(ValueError):
        ship.validate_template(command)


def test_validate_template_accepts_known_and_escaped_braces():
    # arrange, act, assert
    ship.validate_template("git push {repo} {branch}:{base} {{literal}} {key} {title} {worktree}")


def test_render_quotes_every_value():
    # arrange
    values = {"key": "T-1", "title": "it's \"x\" $(rm -rf /)", "branch": "T-1", "base": "main",
              "worktree": "/a b/c", "repo": "/r"}
    # act
    result = ship.render("run {title} {worktree} {{x}}", values)
    # assert
    import shlex
    assert shlex.split(result) == ["run", "it's \"x\" $(rm -rf /)", "/a b/c", "{x}"]


def test_order_puts_dependencies_first():
    # arrange
    tasks = {k: make_task(k, deps=d) for k, d in {"A": [], "B": ["A"], "C": ["B"], "D": []}.items()}
    # act
    result = ship.order(["C", "D", "B", "A"], tasks)
    # assert
    assert result == ["A", "B", "C", "D"]


def test_check_request_demands_unshipped_dependency_first():
    # arrange
    tasks = {"A": make_task("A", status=Status.APPROVED), "B": make_task("B", status=Status.APPROVED, deps=["A"])}
    # act, assert
    with pytest.raises(ValueError, match="сначала отправьте A"):
        ship.check_request(["B"], tasks)
    ship.check_request(["A", "B"], tasks)


def test_check_request_accepts_sent_done_and_cancelled_dependencies():
    # arrange
    tasks = {"A": make_task("A", status=Status.SENT), "X": make_task("X", status=Status.CANCELLED),
             "B": make_task("B", status=Status.APPROVED, deps=["A", "X"])}
    # act, assert
    ship.check_request(["B"], tasks)


def test_check_request_rejects_task_not_approved():
    # arrange
    tasks = {"A": make_task("A", status=Status.IN_REVIEW)}
    # act, assert
    with pytest.raises(ValueError, match="одобренную"):
        ship.check_request(["A"], tasks)


def test_tail_of_keeps_last_bytes(tmp_path):
    # arrange
    log = tmp_path / "x.log"
    log.write_bytes(b"a" * 10000 + "конец".encode())
    # act
    tail = ship.tail_of(log)
    # assert
    assert len(tail.encode()) <= ship.OUTPUT_TAIL_BYTES and tail.endswith("конец")
    assert ship.tail_of(tmp_path / "missing.log") == ""


@pytest.mark.parametrize("command", ['run "{title}"', "run '{title}'", 'run "a $(x) {key}"', 'run "a \\" {title}"',
                                     "run 'a' \"b {branch}\""])
def test_validate_template_rejects_placeholder_inside_quotes(command):
    # arrange, act, assert
    with pytest.raises(ValueError, match="не берите плейсхолдеры в кавычки"):
        ship.validate_template(command)


@pytest.mark.parametrize("command", ["run --title={title}", 'run "x" {title}', "run 'x' {title} \"y\"",
                                     'echo "no placeholders {{literal}}" {key}', 'echo \\"{title}'])
def test_validate_template_allows_placeholder_outside_quotes(command):
    # arrange, act, assert
    ship.validate_template(command)


@pytest.mark.parametrize("command", ["run $'x' {title}", "run $'{title}'", "run {key} $'\\n'"])
def test_validate_template_rejects_ansi_c_quotes(command):
    # arrange, act, assert
    with pytest.raises(ValueError, match="\\$'"):
        ship.validate_template(command)


@pytest.mark.parametrize("command", ["echo 'cost$' {key}", 'echo "$\'" {key}', "echo \\$'x' {key}"])
def test_validate_template_allows_dollar_quote_that_is_not_ansi_c(command):
    # arrange, act, assert
    ship.validate_template(command)


def test_tail_of_stays_within_limit_after_decoding_invalid_utf8(tmp_path):
    # arrange
    log = tmp_path / "x.log"
    log.write_bytes(b"\xff" * 10000)
    # act
    tail = ship.tail_of(log)
    # assert
    assert 0 < len(tail.encode("utf-8")) <= ship.OUTPUT_TAIL_BYTES
