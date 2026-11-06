"""Установка доски из зафиксированного коммита репозитория.

Код пакета копируется из коммита в <home>/runtime/<commit>, а обёртка <home>/bin/agent-board переключается на него
одной заменой файла. Работающая доска так не зависит ни от незакоммиченных правок, ни от маркеров конфликта в рабочем
дереве репозитория.
"""
from __future__ import annotations

import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Optional

from .store import board_home

PACKAGE_PATH = "board/agent_board"
LAUNCHER = "agent-board"
INSTALL_FILE = "install.json"

# Запускает пакет из каталога рядом с собой: текущий каталог в путь импорта не попадает, а в списке процессов
# сервер виден как «agent-board serve».
LAUNCHER_TEXT = (
    "import os\n"
    "import runpy\n"
    "import sys\n"
    "\n"
    "sys.path[0] = os.path.dirname(os.path.abspath(__file__))\n"
    "runpy.run_module(\"agent_board\", run_name=\"__main__\", alter_sys=True)\n"
)


class InstallError(Exception):
    pass


def runtime_root(home: Path) -> Path:
    return home / "runtime"


def wrapper_path(home: Path) -> Path:
    return home / "bin" / "agent-board"


def _git(repo: Path, *args: str, binary: bool = False):
    r = subprocess.run(["git", "-C", str(repo)] + list(args), capture_output=True, timeout=120)
    if r.returncode != 0:
        raise InstallError("git %s: %s" % (" ".join(args), r.stderr.decode("utf-8", "replace").strip()))
    return r.stdout if binary else r.stdout.decode("utf-8").strip()


def default_ref(repo: Path) -> str:
    """Основная ветка репозитория: main или master, иначе текущий HEAD."""
    for name in ("main", "master"):
        r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", "refs/heads/" + name],
                           capture_output=True, timeout=30)
        if r.returncode == 0:
            return name
    return "HEAD"


def resolve_commit(repo: Path, ref: Optional[str]) -> str:
    return _git(repo, "rev-parse", "--verify", (ref or default_ref(repo)) + "^{commit}")


def _extract(repo: Path, commit: str, target: Path) -> None:
    """Раскладывает пакет из коммита в каталог target (атомарно: сначала во временный каталог рядом)."""
    data = _git(repo, "archive", "--format=tar", commit, PACKAGE_PATH, binary=True)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=str(target.parent)))
    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            members = [m for m in tar.getmembers()
                       if m.name.startswith(PACKAGE_PATH + "/") and ".." not in Path(m.name).parts
                       and (m.isfile() or m.isdir())]
            safe = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            tar.extractall(str(staging), members=members, **safe)
        package = staging / PACKAGE_PATH
        if not (package / "__main__.py").is_file():
            raise InstallError("в коммите %s нет пакета %s" % (commit, PACKAGE_PATH))
        package.rename(staging / "agent_board")
        shutil.rmtree(str(staging / "board"))
        (staging / LAUNCHER).write_text(LAUNCHER_TEXT, encoding="utf-8")
        try:
            staging.rename(target)
        except OSError:
            # тот же коммит уже разложен параллельной установкой
            if not (target / LAUNCHER).is_file():
                raise
    finally:
        if staging.exists():
            shutil.rmtree(str(staging), ignore_errors=True)


def _write_atomic(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, str(path))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def wrapper_text(runtime: Path, python: str, home: Path) -> str:
    return (
        "#!/bin/sh\n"
        "# agent-board из зафиксированного коммита; переключается командой agent-board self-update\n"
        "if [ -z \"${AGENT_BOARD_HOME:-}\" ]; then AGENT_BOARD_HOME=%s; fi\n"
        "export AGENT_BOARD_HOME\n"
        "exec %s %s \"$@\"\n" % (shlex.quote(str(home)), shlex.quote(python), shlex.quote(str(runtime / LAUNCHER)))
    )


def read_install(home: Path) -> Optional[dict]:
    try:
        data = json.loads((runtime_root(home) / INSTALL_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def install(repo: Path, ref: Optional[str] = None, home: Optional[Path] = None,
            python: Optional[str] = None) -> dict:
    """Ставит пакет из коммита ref (по умолчанию — основная ветка) и переключает на него обёртку."""
    repo = Path(repo).resolve()
    home = Path(home) if home else board_home()
    python = python or sys.executable
    commit = resolve_commit(repo, ref)
    runtime = runtime_root(home) / commit
    runtime.parent.mkdir(parents=True, exist_ok=True)
    if not (runtime / LAUNCHER).is_file():
        _extract(repo, commit, runtime)
    previous = (read_install(home) or {}).get("commit")
    _write_atomic(wrapper_path(home), wrapper_text(runtime, python, home), 0o755)
    _write_atomic(runtime_root(home) / INSTALL_FILE, json.dumps(
        {"repo": str(repo), "ref": ref, "commit": commit, "python": python}, ensure_ascii=False, indent=2) + "\n")
    return {"commit": commit, "previous": previous, "runtime": str(runtime), "wrapper": str(wrapper_path(home))}


def run(repo: Optional[str], ref: Optional[str], home: Optional[Path] = None) -> int:
    """Установка или обновление с сообщениями для человека; ref и repo по умолчанию — из прошлой установки."""
    from . import lifecycle
    from .messages import msg

    home = Path(home) if home else board_home()
    saved = read_install(home) or {}
    repo = repo or saved.get("repo")
    if not repo:
        print(msg("install.no_repo"), file=sys.stderr)
        return 1
    if ref is None:
        ref = saved.get("ref")
    try:
        result = install(Path(repo), ref, home)
    except (InstallError, OSError, subprocess.SubprocessError, tarfile.TarError) as e:
        print(msg("install.failed", error=e), file=sys.stderr)
        return 1
    changed = result["commit"] != result["previous"]
    print(msg("install.done" if changed else "install.unchanged", commit=result["commit"][:12],
              wrapper=result["wrapper"]))
    if changed and lifecycle.live_server() is not None:
        print(msg("install.restart"))
    bin_dir = str(wrapper_path(home).parent)
    if bin_dir not in os.environ.get("PATH", "").split(os.pathsep):
        print(msg("install.path_hint", bin=bin_dir))
    return 0
