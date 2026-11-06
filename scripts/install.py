#!/usr/bin/env python3
"""Ставит agent-board из коммита репозитория: install.py [--ref REF] [--repo PATH] [--home DIR].

Сам установщик тоже берётся из выбранного коммита, а не из рабочего дерева: незакоммиченные правки и маркеры
конфликта в checkout на установку не влияют.
"""
from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

PACKAGE_PATH = "board/agent_board"


def main() -> int:
    parser = argparse.ArgumentParser(prog="install.py", description="Install agent-board from a repository commit")
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent),
                        help="repository (default: the one this script lives in)")
    parser.add_argument("--ref", help="commit, branch or tag (default: main, else master, else HEAD)")
    parser.add_argument("--home", help="board data directory (default: $AGENT_BOARD_HOME or ~/.agent-board)")
    args = parser.parse_args()
    if args.home:
        os.environ["AGENT_BOARD_HOME"] = str(Path(args.home).expanduser().resolve())
    repo = Path(args.repo).resolve()
    ref = args.ref
    if ref is None:
        ref = next((name for name in ("main", "master") if subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", "refs/heads/" + name],
            capture_output=True).returncode == 0), "HEAD")
    archive = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", ref + "^{commit}", PACKAGE_PATH],
                             capture_output=True)
    if archive.returncode != 0:
        print("install.py: %s" % archive.stderr.decode("utf-8", "replace").strip(), file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory(prefix="agent-board-install-") as tmp:
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
            safe = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            tar.extractall(tmp, members=[m for m in tar.getmembers() if ".." not in Path(m.name).parts], **safe)
        sys.path.insert(0, str(Path(tmp) / "board"))
        try:
            from agent_board import install
        except ImportError:
            print("install.py: %s in %s has no installer, pick a newer commit" % (PACKAGE_PATH, ref), file=sys.stderr)
            return 1
        return install.run(str(repo), args.ref)


if __name__ == "__main__":
    raise SystemExit(main())
