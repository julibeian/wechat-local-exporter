import pytest


@pytest.fixture(autouse=True)
def isolated_app_settings(tmp_path, monkeypatch):
    # Unit/package smoke tests must never modify the developer's real settings.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
