#!/usr/bin/env python3
"""Выгружает текущее дерево репозитория в новый git-репозиторий одним коммитом.

Берутся отслеживаемые и новые неигнорируемые файлы в их текущем виде; игнорируемые
файлы и служебный каталог .superpowers не попадают. Ничего не отправляет на сервер.

    python3 scripts/export-repo.py <каталог> --author-name "Имя" --author-email you@example.com
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_DIRS = (".superpowers",)


def _git(args, cwd, env=None):
    return subprocess.run(["git"] + args, cwd=str(cwd), env=env, capture_output=True, check=True).stdout


def list_files(source: Path):
    """Пути файлов, которые попадут в выгрузку, относительно корня репозитория."""
    out = _git(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], source)
    result = []
    for rel in sorted(set(p for p in out.decode("utf-8").split("\0") if p)):
        if rel.split("/")[0] in EXCLUDED_DIRS:
            continue
        # удалённые, но ещё не убранные из индекса файлы пропускаем
        if not os.path.lexists(str(source / rel)):
            continue
        result.append(rel)
    return result


def export(source: Path, dest: Path, name: str, email: str, message: str) -> None:
    if dest.exists() and any(dest.iterdir()):
        raise SystemExit("export-repo: %s не пуст" % dest)
    files = list_files(source)
    for rel in files:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(source / rel), str(target), follow_symlinks=False)

    # автор задаётся только переменными окружения, чтобы не зависеть от настроек машины
    env = dict(os.environ, GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email,
               GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email, GIT_CONFIG_NOSYSTEM="1")
    _git(["init", "-q"], dest)
    _git(["symbolic-ref", "HEAD", "refs/heads/main"], dest)
    _git(["add", "-A"], dest)
    _git(["-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", message], dest, env)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Выгрузить дерево в новый репозиторий одним коммитом")
    p.add_argument("dest", help="каталог нового репозитория (не существует или пуст)")
    p.add_argument("--author-name", required=True)
    p.add_argument("--author-email", required=True)
    p.add_argument("--message", default="Initial commit")
    p.add_argument("--source", default=str(ROOT), help="исходный репозиторий (по умолчанию этот)")
    a = p.parse_args(argv)
    dest = Path(a.dest).expanduser().resolve()
    export(Path(a.source).resolve(), dest, a.author_name, a.author_email, a.message)
    print("exported to %s" % dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
