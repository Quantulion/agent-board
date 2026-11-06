from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# внутренние имена, личные пути и раскладка ключей автора не должны попадать в публикуемые файлы
# шаблон собран из частей, чтобы этот файл сам не совпадал с проверкой
INTERNAL = re.compile("|".join([r"\bci" + r"an\b", "ci" + "anprojects", "mn" + "z-", "/Us" + "ers/", "keys/" + "jira"]),
                      re.I)


def _tracked_files(root: Path):
    if shutil.which("git") is None or not (root / ".git").exists():
        pytest.skip("нет git-репозитория")
    out = subprocess.run(["git", "ls-files", "-z"], cwd=str(root), capture_output=True, check=True).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def internal_name_hits(root: Path):
    """Строки отслеживаемых файлов репозитория, где встречаются внутренние имена."""
    hits = []
    for rel in _tracked_files(root):
        path = root / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for n, line in enumerate(text.splitlines(), 1):
            if INTERNAL.search(line):
                hits.append("%s:%d: %s" % (rel, n, line.strip()[:120]))
    return hits


def agent_report_files(root: Path):
    """Отслеживаемые файлы из каталога рабочих отчётов агентов, которые попадут в коммит."""
    # удалённый с диска файл уйдёт из репозитория при коммите одобрения
    return [rel for rel in _tracked_files(root) if rel.split("/")[0] == "reports" and (root / rel).exists()]


def test_tracked_files__no_agent_reports():
    # act
    reports = agent_report_files(ROOT)
    # assert
    assert reports == []


def test_agent_report_files__tracked_report__returned(tmp_path):
    # arrange
    if shutil.which("git") is None:
        pytest.skip("нет git")
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "T-1.md").write_text("отчёт", encoding="utf-8")
    (tmp_path / "notes.md").write_text("заметки", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
    # act
    reports = agent_report_files(tmp_path)
    # assert
    assert reports == ["reports/T-1.md"]


def test_gitignore__ignores_agent_reports():
    # arrange
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    # act / assert
    assert "reports/" in lines


def test_tracked_files__no_internal_names():
    # act
    hits = internal_name_hits(ROOT)
    # assert
    assert hits == []
