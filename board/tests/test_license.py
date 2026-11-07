from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Первая строка файла LICENSE для каждой поддерживаемой лицензии
LICENSE_HEADINGS = {
    "MIT": "MIT License",
    "Apache-2.0": "Apache License",
}


def _pyproject_license():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    section = re.search(r"^\[project\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S).group(1)
    return re.search(r'^license\s*=\s*\{\s*text\s*=\s*"([^"]+)"\s*\}', section, re.M).group(1)


def test_license_is_consistent_across_manifests():
    # arrange
    spdx = _pyproject_license()

    # act
    heading = (ROOT / "LICENSE").read_text(encoding="utf-8").strip().splitlines()[0].strip()
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    # assert
    assert spdx in LICENSE_HEADINGS
    assert heading == LICENSE_HEADINGS[spdx]
    assert plugin["license"] == spdx
    assert re.search(r"^## License\s*$\s*^.*\b" + re.escape(spdx) + r"\b", readme, re.M)
