from __future__ import annotations

import threading
import time

import pytest

from fakes import fake_crit

from agent_board import review as review_mod
from agent_board.review import CritReview, NoReview, make_review, port_of

TIMEOUT = 10.0


@pytest.fixture
def crit(tmp_path):
    yield fake_crit.install(tmp_path / "bin")
    fake_crit.cleanup(tmp_path / "bin")


@pytest.fixture
def data_dir(tmp_path):
    path = tmp_path / "crit-data"
    path.mkdir()
    return path


def wait_for(cond, what="условие"):
    deadline = time.monotonic() + TIMEOUT
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("не дождались: %s" % what)
        time.sleep(0.02)


def run_round(review, cwd, data_dir, tmp_path, finish=None, session=None, port=None):
    """Запускает раунд, ждёт ссылку, выполняет finish(session) и возвращает (результат, ссылки)."""
    urls = []
    proc = review.start(cwd, "main", data_dir, session=session, port=port)
    result = {}
    t = threading.Thread(target=lambda: result.update(r=review.watch(proc, tmp_path / "review.log",
                                                                       lambda u, s: urls.append((u, s)))))
    t.start()
    if finish is not None:
        wait_for(lambda: urls, "ссылка ревью")
        finish(urls[0][1])
    t.join(TIMEOUT)
    assert not t.is_alive()
    return result["r"], urls


def test_make_review_by_tool():
    # arrange / act / assert
    assert isinstance(make_review({"tool": "none"}), NoReview)
    crit = make_review({"tool": "crit", "binary": "/x/crit"})
    assert isinstance(crit, CritReview) and crit.binary == "/x/crit"
    assert make_review({}).binary == "crit"
    with pytest.raises(ValueError):
        make_review({"tool": "gerrit"})


def test_port_of_url():
    # arrange / act / assert
    assert port_of("http://localhost:47311") == 47311
    assert port_of(None) is None and port_of("garbage") is None


def test_round_with_comments_reports_url_and_normalized_comments(crit, data_dir, git_repo, tmp_path):
    # arrange
    review = CritReview(str(crit))
    fake_crit.configure(data_dir, port=47311)
    comments = [{"path": "a.txt", "line": 1, "body": "переименуй"}, {"body": "в целом ок"}]
    # act
    result, urls = run_round(review, git_repo, data_dir, tmp_path,
                             finish=lambda s: fake_crit.finish(data_dir, s, comments))
    # assert
    session = fake_crit.session_for(git_repo)
    assert urls == [("http://localhost:47311", session)]
    assert result.outcome == "changes" and result.url == "http://localhost:47311" and result.session == session
    by_body = {c["body"]: c for c in result.comments}
    assert set(by_body) == {"переименуй", "в целом ок"}
    assert by_body["переименуй"]["path"] == "a.txt" and by_body["переименуй"]["line"] == 1
    assert by_body["в целом ок"]["path"] is None and by_body["в целом ок"]["line"] is None
    assert all(c["id"] for c in result.comments)
    assert set(result.comments[0]) == {"id", "path", "line", "body"}
    call = fake_crit.review_calls(data_dir)[0]["argv"]
    assert call[:5] == ["--no-open", "--base-branch", "main", "-o", str(data_dir)]
    assert (tmp_path / "review.log").read_text().count("approved: false") == 1


def test_round_without_comments_is_approved(crit, data_dir, git_repo, tmp_path):
    # arrange
    review = CritReview(str(crit))
    # act
    result, _ = run_round(review, git_repo, data_dir, tmp_path, finish=lambda s: fake_crit.finish(data_dir, s))
    # assert
    assert result.outcome == "approved" and result.comments == []


def test_daemon_shutdown_before_finish_is_aborted_not_changes(crit, data_dir, git_repo, tmp_path):
    # arrange
    review = CritReview(str(crit))
    fake_crit.finish(data_dir, fake_crit.session_for(git_repo), [{"body": "старый"}], approved=False)
    first, _ = run_round(review, git_repo, data_dir, tmp_path)
    assert first.outcome == "changes"
    # act: следующий раунд, но демон остановился раньше, чем пользователь завершил ревью
    result, _ = run_round(review, git_repo, data_dir, tmp_path, session=first.session,
                          finish=lambda s: fake_crit.kill_daemon(data_dir, s))
    # assert
    assert result.outcome == "aborted" and "shut down" in result.error


def test_reconnect_to_missing_session_reports_no_session(crit, data_dir, git_repo, tmp_path):
    # arrange
    review = CritReview(str(crit))
    # act
    result, urls = run_round(review, git_repo, data_dir, tmp_path, session="deadbeef0000")
    # assert
    assert result.outcome == "no_session" and urls == []
    assert fake_crit.review_calls(data_dir)[0]["argv"][-2:] == ["--session", "deadbeef0000"]


def test_port_and_session_are_passed(crit, data_dir, git_repo, tmp_path):
    # arrange
    review = CritReview(str(crit))
    # act
    result, urls = run_round(review, git_repo, data_dir, tmp_path, port=40123,
                             finish=lambda s: fake_crit.finish(data_dir, s))
    # assert
    assert urls[0][0] == "http://localhost:40123"
    assert "-p" in fake_crit.review_calls(data_dir)[0]["argv"]


def test_missing_binary_fails_without_exception(data_dir, git_repo, tmp_path):
    # arrange
    review = CritReview(str(tmp_path / "no-such-crit"))
    # act
    result, _ = run_round(review, git_repo, data_dir, tmp_path)
    # assert
    assert result.outcome == "failed" and "не найден" in result.error


def test_crash_without_verdict_is_failed(tmp_path, data_dir, git_repo):
    # arrange
    bad = tmp_path / "bad-crit"
    bad.write_text("#!/bin/sh\necho 'boom' >&2\nexit 3\n")
    bad.chmod(0o755)
    # act
    result, _ = run_round(CritReview(str(bad)), git_repo, data_dir, tmp_path)
    # assert
    assert result.outcome == "failed" and "boom" in result.error


def test_comments_fall_back_to_round_output_when_listing_fails(crit, data_dir, git_repo, tmp_path, monkeypatch):
    # arrange
    review = CritReview(str(crit))

    def broken(self, cwd, data):
        raise review_mod.ReviewError("comments недоступны")

    monkeypatch.setattr(CritReview, "fetch_comments", broken)
    # act
    result, _ = run_round(review, git_repo, data_dir, tmp_path,
                          finish=lambda s: fake_crit.finish(data_dir, s, [{"path": "a.txt", "line": 2, "body": "x"}]))
    # assert
    assert result.outcome == "changes" and [c["body"] for c in result.comments] == ["x"]


def test_not_approved_with_empty_listing_uses_round_output(crit, data_dir, git_repo, tmp_path):
    # arrange: crit сказал approved: false, а список комментариев ещё не записан на диск
    review = CritReview(str(crit))
    fake_crit.configure(data_dir, empty_listing=True)
    # act
    result, _ = run_round(review, git_repo, data_dir, tmp_path,
                          finish=lambda s: fake_crit.finish(data_dir, s, [{"path": "a.txt", "line": 1, "body": "y"}]))
    # assert
    assert result.outcome == "changes" and [c["body"] for c in result.comments] == ["y"]


def test_not_approved_without_readable_comments_fails_instead_of_approving(tmp_path, data_dir, git_repo, monkeypatch):
    # arrange
    monkeypatch.setattr(review_mod, "COMMENTS_RETRY_DELAY", 0.01)
    bad = tmp_path / "bad-crit"
    bad.write_text("#!/bin/sh\n"
                   "if [ \"$1\" = comments ]; then echo '[]'; exit 0; fi\n"
                   "echo 'Started crit daemon at http://localhost:4000 (session s1, PID 1)' >&2\n"
                   "echo 'The review finished with 2 unresolved comments.'\n"
                   "echo 'approved: false' >&2\n")
    bad.chmod(0o755)
    # act
    result, _ = run_round(CritReview(str(bad)), git_repo, data_dir, tmp_path)
    # assert
    assert result.outcome == "failed" and "комментари" in result.error


def test_not_approved_with_zero_unresolved_everywhere_is_approved(tmp_path, data_dir, git_repo):
    # arrange
    bad = tmp_path / "zero-crit"
    bad.write_text("#!/bin/sh\n"
                   "if [ \"$1\" = comments ]; then echo '[]'; exit 0; fi\n"
                   "echo 'The review finished with 0 unresolved comments.'\n"
                   "echo '[]'\n"
                   "echo 'approved: false' >&2\n")
    bad.chmod(0o755)
    # act
    result, _ = run_round(CritReview(str(bad)), git_repo, data_dir, tmp_path)
    # assert
    assert result.outcome == "approved"


def test_stop_daemon_terminates_review_page_and_ignores_foreign_pid(crit, data_dir, git_repo, tmp_path):
    # arrange
    review = CritReview(str(crit))
    session = fake_crit.session_for(git_repo)
    fake_crit.finish(data_dir, session, [{"body": "x"}])
    run_round(review, git_repo, data_dir, tmp_path)
    pid = fake_crit.daemon_pid(data_dir, session)
    assert pid
    # act / assert: чужая сессия не трогается
    assert review.stop_daemon(git_repo, data_dir, "other-session") is False
    assert fake_crit.daemon_pid(data_dir, session) == pid
    # act / assert
    assert review.stop_daemon(git_repo, data_dir, session) is True
    wait_for(lambda: fake_crit.daemon(data_dir, session) is None, "демон остановлен")


# --- уборка данных ревью и логов ---

def test_trim_log__large_log__keeps_last_limit_from_line_start(tmp_path):
    # arrange
    log = tmp_path / "A.review.log"
    lines = ["строка %05d\n" % i for i in range(4000)]
    log.write_text("".join(lines), encoding="utf-8")
    # act
    review_mod.trim_log(log, limit=10000)
    # assert
    text = log.read_text(encoding="utf-8")
    assert len(text.encode("utf-8")) <= 10000 and text.endswith(lines[-1]) and text.startswith("строка")


def test_trim_log__small_or_missing_log__unchanged(tmp_path):
    # arrange
    log = tmp_path / "A.review.log"
    log.write_text("коротко\n", encoding="utf-8")
    # act
    review_mod.trim_log(log, limit=10000)
    review_mod.trim_log(tmp_path / "missing.log", limit=10000)
    # assert
    assert log.read_text(encoding="utf-8") == "коротко\n" and not (tmp_path / "missing.log").exists()


def _review_task(board, key, session, status):
    from conftest import make_task

    from agent_board.model import Status

    board.add_task(make_task(key), "user")
    board.update_task(key, "system", {"review": {"state": "approved", "session": session, "round": 1}})
    if status is not None:
        board.transition(key, Status(status), "user")
    data = board.store.root / "crit" / "reviews" / session
    data.mkdir(parents=True)
    (data / "review.json").write_text("{}", encoding="utf-8")
    return data


@pytest.mark.parametrize("status, days, removed", [
    ("done", 8, True), ("cancelled", 8, True), ("done", 6, False), ("in_review", 30, False), (None, 30, False),
])
def test_prune_review_data__closed_week_ago__removed(board, status, days, removed):
    # arrange
    from datetime import datetime, timedelta

    from conftest import NOW

    data = _review_task(board, "A", "abc123def456", status)
    now = datetime.fromisoformat(NOW) + timedelta(days=days)
    # act
    result = review_mod.prune_review_data(board.store, now)
    # assert
    assert (not data.exists()) is removed and result == (["A"] if removed else [])


def test_prune_review_data__unsafe_session__ignored(board):
    # arrange
    from datetime import datetime, timedelta

    from conftest import NOW, make_task

    from agent_board.model import Status

    board.add_task(make_task("A"), "user")
    board.update_task("A", "system", {"review": {"state": "approved", "session": "..", "round": 1}})
    board.transition("A", Status.DONE, "user")
    reviews = board.store.root / "crit" / "reviews"
    reviews.mkdir(parents=True)
    # act
    result = review_mod.prune_review_data(board.store, datetime.fromisoformat(NOW) + timedelta(days=30))
    # assert
    assert result == [] and reviews.exists()


def test_prune_review_data__closed_task__session_files_removed(board):
    # arrange
    from datetime import datetime, timedelta

    from conftest import NOW

    data = _review_task(board, "A", "abc123def456", "done")
    sessions = board.store.root / "crit" / "sessions"
    sessions.mkdir()
    (sessions / "abc123def456.json").write_text("{}", encoding="utf-8")
    (sessions / "abc123def456.log").write_text("log", encoding="utf-8")
    (sessions / "other.json").write_text("{}", encoding="utf-8")
    # act
    result = review_mod.prune_review_data(board.store, datetime.fromisoformat(NOW) + timedelta(days=30))
    # assert
    assert result == ["A"] and not data.exists()
    assert [p.name for p in sessions.iterdir()] == ["other.json"]


def test_prune_review_data__only_session_files_left__removed(board):
    # arrange
    from datetime import datetime, timedelta

    from conftest import NOW

    data = _review_task(board, "A", "abc123def456", "done")
    import shutil

    shutil.rmtree(str(data))
    sessions = board.store.root / "crit" / "sessions"
    sessions.mkdir()
    (sessions / "abc123def456.json").write_text("{}", encoding="utf-8")
    # act
    result = review_mod.prune_review_data(board.store, datetime.fromisoformat(NOW) + timedelta(days=30))
    # assert
    assert result == ["A"] and list(sessions.iterdir()) == []
