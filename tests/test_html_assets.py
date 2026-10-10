from __future__ import annotations

from importlib.resources import files
from pathlib import Path
import tomllib


REQUIRED_ASSETS = ("index.template.html", "viewer.css", "viewer.js")


def test_html_assets_are_available_as_package_resources() -> None:
    asset_root = files("wechat_exporter.html_assets")

    for filename in REQUIRED_ASSETS:
        resource = asset_root.joinpath(filename)
        assert resource.is_file(), filename
        assert resource.read_text(encoding="utf-8").strip(), filename


def test_html_assets_are_declared_for_setuptools_and_pyinstaller() -> None:
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = project["tool"]["setuptools"]["package-data"]
    spec = (root / "packaging" / "WeChat-Local-Exporter.spec").read_text(
        encoding="utf-8"
    )

    assert package_data["wechat_exporter.html_assets"] == ["*.html", "*.css", "*.js", "icons/*.svg", "icons/*.txt", "faces/*.png", "faces/*.json", "faces/*.txt"]
    icons = root / "src" / "wechat_exporter" / "html_assets" / "icons"
    assert (icons / "LICENSE.txt").is_file()
    assert (icons / "check-circle.svg").is_file()
    assert 'root / "src" / "wechat_exporter" / "html_assets"' in spec
    assert '(str(html_assets), "wechat_exporter/html_assets")' in spec
