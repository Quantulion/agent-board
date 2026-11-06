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
    return fake_crit.install(tmp_path / "bin")


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
