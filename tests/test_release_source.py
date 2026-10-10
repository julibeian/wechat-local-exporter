from __future__ import annotations

import io
from pathlib import Path
import runpy
import zipfile

import pytest


audit = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/verify_release_source.py"))


@pytest.mark.parametrize("name", ["docs/review-20260101.md", "state/account.sqlite", "src/.env"])
def test_source_archive_rejects_internal_notes_and_local_data(name):
    with pytest.raises(ValueError):
        audit["audit_files"]({name: b"local content"})


def test_source_archive_rejects_embedded_account_credentials():
    value = "wxid_" + "0123456789abcdef"
    with pytest.raises(ValueError, match="Private identifier"):
        audit["audit_files"]({"src/module.py": value.encode()})


def test_source_archive_accepts_public_docs_and_synthetic_fixtures():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("source-v1/README.md", "Public instructions.")
        archive.writestr("source-v1/tests/fixture.py", "account = 'wxid_sample'\n")
    result = audit["audit_zip"](stream.getvalue())
    assert result["status"] == "ok" and result["files"] == 2
