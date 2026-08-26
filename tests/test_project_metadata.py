from __future__ import annotations

from wechat_exporter import PROJECT_URL, __version__
from wechat_exporter.gui import STAR_PROMPT_DELAY_SECONDS


def test_public_project_metadata() -> None:
    assert __version__ == "1.0.0"
    assert PROJECT_URL == "https://github.com/julibeian/wechat-txt-pdf-exporter"
    assert STAR_PROMPT_DELAY_SECONDS == 30.0
