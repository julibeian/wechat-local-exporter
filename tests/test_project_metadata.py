from __future__ import annotations

from wechat_exporter import PROJECT_URL, __version__
from wechat_exporter.gui import STAR_PROMPT_DELAY_SECONDS, VOICE_TEXT_GUIDE_STEPS


def test_public_project_metadata() -> None:
    assert __version__ == "1.2.0"
    assert PROJECT_URL == "https://github.com/julibeian/wechat-txt-pdf-exporter"
    assert STAR_PROMPT_DELAY_SECONDS == 60.0
    assert len(VOICE_TEXT_GUIDE_STEPS) == 4
    assert "TXT 或 PDF" in VOICE_TEXT_GUIDE_STEPS[-1]
