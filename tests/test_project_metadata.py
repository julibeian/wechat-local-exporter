from __future__ import annotations

import tomllib
from pathlib import Path

from wechat_exporter import PROJECT_URL, __version__
from wechat_exporter import gui
from wechat_exporter.gui import VOICE_TEXT_GUIDE_STEPS


def test_public_project_metadata() -> None:
    assert __version__ == "1.5.0"
    assert PROJECT_URL == "https://github.com/julibeian/wechat-local-exporter"
    assert not hasattr(gui, "STAR_PROMPT_DELAY_SECONDS")
    assert not hasattr(gui, "StarPrompt")
    assert len(VOICE_TEXT_GUIDE_STEPS) == 4
    assert "高级聊天资料包" in VOICE_TEXT_GUIDE_STEPS[-1]


def test_packaging_versions_match_runtime_version() -> None:
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    installer = (root / "packaging" / "installer.iss").read_text(encoding="utf-8")

    assert project["project"]["version"] == __version__
    assert f'#define AppVersion "{__version__}"' in installer
