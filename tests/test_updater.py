import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from wechat_exporter import updater
from wechat_exporter.errors import UserFacingError
from wechat_exporter.update import DownloadedUpdate
from wechat_exporter.updater import UpdatePlan, STABLE_EXE, apply_update, installation_kind


def make_plan(tmp_path, kind="portable"):
    target = tmp_path / "app" / STABLE_EXE
    target.parent.mkdir()
    target.write_bytes(b"old executable")
    payload = tmp_path / "update-123" / "payload.exe"
    payload.parent.mkdir()
    payload.write_bytes(b"new executable")
    if kind == "installer":
        (target.parent / "unins000.exe").write_bytes(b"uninstaller")
    installed_bytes = b"new installed" if kind == "installer" else payload.read_bytes()
    return UpdatePlan(
        str(payload),
        hashlib.sha256(payload.read_bytes()).hexdigest(),
        hashlib.sha256(installed_bytes).hexdigest(),
        str(target),
        kind,
        "1.4.0",
        1234,
    )


def test_portable_replacement_and_restart(tmp_path):
    plan = make_plan(tmp_path)
    started = []
    apply_update(plan, launch=lambda path, *args: started.append(path))
    assert Path(plan.target).read_bytes() == b"new executable"
    assert started == [Path(plan.target)]
    assert (Path(plan.payload).parent / "previous.exe").read_bytes() == b"old executable"


def test_hash_failure_never_changes_old_executable(tmp_path):
    plan = make_plan(tmp_path)
    Path(plan.payload).write_bytes(b"tampered")
    with pytest.raises(UserFacingError, match="校验"):
        apply_update(plan, launch=lambda *args: pytest.fail("must not run"))
    assert Path(plan.target).read_bytes() == b"old executable"


def test_invalid_target_hash_never_runs_installer(tmp_path):
    plan = make_plan(tmp_path, "installer")
    plan = UpdatePlan(
        plan.payload,
        plan.sha256,
        "z" * 64,
        plan.target,
        plan.kind,
        plan.version,
        plan.parent_pid,
    )
    with pytest.raises(UserFacingError, match="校验"):
        apply_update(plan, launch=lambda *args: pytest.fail("must not run"))
    assert Path(plan.target).read_bytes() == b"old executable"


def test_failed_portable_start_restores_old_binary(tmp_path):
    plan = make_plan(tmp_path)
    calls = []
    def launch(path, *args):
        calls.append(path.read_bytes())
        if len(calls) == 1:
            raise OSError("start failed")
    with pytest.raises(UserFacingError):
        apply_update(plan, launch=launch)
    assert calls == [b"new executable", b"old executable"]
    assert Path(plan.target).read_bytes() == b"old executable"


@pytest.mark.parametrize("exit_code", [0, 5, 3010])
def test_installer_exit_codes_and_rollback(tmp_path, exit_code):
    plan = make_plan(tmp_path, "installer")
    calls = []
    def launch(path, *args):
        calls.append((path, args))
        if path == Path(plan.payload):
            Path(plan.target).write_bytes(b"new installed")
        return SimpleNamespace(wait=lambda: exit_code)
    if exit_code:
        with pytest.raises(UserFacingError):
            apply_update(plan, launch=launch)
        assert Path(plan.target).read_bytes() == b"old executable"
    else:
        apply_update(plan, launch=launch)
        assert Path(plan.target).read_bytes() == b"new installed"
    assert "/NORESTART" in calls[0][1]
    assert "/NOCLOSEAPPLICATIONS" in calls[0][1]
    assert calls[-1][0] == Path(plan.target)


def test_installer_success_code_with_wrong_installed_binary_rolls_back(tmp_path):
    plan = make_plan(tmp_path, "installer")
    started = []

    def launch(path, *args):
        if path == Path(plan.payload):
            Path(plan.target).write_bytes(b"wrong installed binary")
            return SimpleNamespace(wait=lambda: 0)
        started.append(path)

    with pytest.raises(UserFacingError):
        apply_update(plan, launch=launch)

    assert Path(plan.target).read_bytes() == b"old executable"
    assert started == [Path(plan.target)]


def test_locked_portable_file_preserves_original(tmp_path, monkeypatch):
    plan = make_plan(tmp_path)
    monkeypatch.setattr(updater.os, "replace", lambda *a: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(UserFacingError):
        apply_update(plan, launch=lambda *a: None)
    assert Path(plan.target).read_bytes() == b"old executable"
    assert not Path(plan.target + ".update-new").exists()


def test_install_detection_and_staging_never_replaces_running_file(tmp_path, monkeypatch):
    plan = make_plan(tmp_path, "installer")
    target = Path(plan.target)
    assert installation_kind(target) == "installer"
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater.sys, "executable", str(target))
    launched = []
    monkeypatch.setattr(updater, "_launch", lambda *args: launched.append(args))
    download = DownloadedUpdate(
        Path(plan.payload),
        plan.sha256,
        plan.target_sha256,
        plan.version,
        plan.kind,
    )
    path = updater.stage_update(download)
    assert path.is_file()
    assert launched[0][0].name == "update-runner.exe"
    assert launched[0][1] == "--apply-update"
    assert target.read_bytes() == b"old executable"
    assert not (path.parent / "ready").exists()
    (path.parent / "abort").touch()
    second = updater.stage_update(download)
    assert second.parent != path.parent
    assert (path.parent / "abort").is_file()  # Retry cannot revive a slow old helper.


def test_source_mode_cannot_replace_python(tmp_path, monkeypatch):
    monkeypatch.setattr(updater.sys, "frozen", False, raising=False)
    plan = make_plan(tmp_path)
    with pytest.raises(UserFacingError, match="源码"):
        updater.stage_update(
            DownloadedUpdate(
                Path(plan.payload),
                plan.sha256,
                plan.target_sha256,
                plan.version,
                plan.kind,
            )
        )


def test_cleanup_only_removes_successful_transaction(tmp_path):
    success, failed = tmp_path / "update-success", tmp_path / "update-failed"
    success.mkdir()
    failed.mkdir()
    (success / "success").touch()
    (failed / "previous.exe").write_bytes(b"backup")
    updater.cleanup_finished_updates(tmp_path)
    assert not success.exists()
    assert failed.is_dir()
