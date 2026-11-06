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


def _tracked_files():
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("нет git-репозитория")
    out = subprocess.run(["git", "ls-files", "-z"], cwd=str(ROOT), capture_output=True, check=True).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def test_tracked_files__no_internal_names():
    # arrange
    hits = []
    # act
    for rel in _tracked_files():
        path = ROOT / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for n, line in enumerate(text.splitlines(), 1):
            if INTERNAL.search(line):
                hits.append("%s:%d: %s" % (rel, n, line.strip()[:120]))
    # assert
    assert hits == []
