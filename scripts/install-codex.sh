#!/bin/sh
# Links the agent-board skill into Codex skills: install-codex.sh [--force] [--uninstall]
set -e

force=0
uninstall=0
for arg in "$@"; do
    case "$arg" in
        --force) force=1 ;;
        --uninstall) uninstall=1 ;;
        -h|--help) echo "usage: install-codex.sh [--force] [--uninstall]"; exit 0 ;;
        *) echo "install-codex.sh: unknown argument $arg" >&2; exit 2 ;;
    esac
done

root=$(cd -P "$(dirname "$0")/.." && pwd)
source_dir="$root/skills/agent-board"
skills_dir="${CODEX_HOME:-$HOME/.codex}/skills"
target="$skills_dir/agent-board"

if [ "$uninstall" = 1 ]; then
    if [ -h "$target" ]; then
        rm "$target"
        echo "removed $target"
    elif [ -e "$target" ]; then
        echo "install-codex.sh: $target is not a symlink, leaving it" >&2
        exit 1
    else
        echo "not installed: $target"
    fi
    exit 0
fi

if [ ! -f "$source_dir/SKILL.md" ]; then
    echo "install-codex.sh: $source_dir/SKILL.md not found" >&2
    exit 1
fi

if [ -h "$target" ] && [ "$(readlink "$target")" = "$source_dir" ]; then
    echo "already installed: $target -> $source_dir"
    exit 0
fi

if [ -e "$target" ] || [ -h "$target" ]; then
    if [ "$force" != 1 ]; then
        echo "install-codex.sh: $target already exists and is not this skill; use --force to replace it" >&2
        exit 1
    fi
    if [ -h "$target" ]; then
        rm "$target"
    else
        backup="$target.bak.$(date +%Y%m%d%H%M%S)"
        mv "$target" "$backup"
        echo "moved existing $target to $backup"
    fi
fi

mkdir -p "$skills_dir"
ln -s "$source_dir" "$target"
echo "installed: $target -> $source_dir"
