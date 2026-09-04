from __future__ import annotations

import os
import subprocess
import threading
import uuid
from queue import Queue
from types import SimpleNamespace

import pytest

from wechat_exporter import gui
from wechat_exporter.instance_control import (
    WindowsInstanceCoordinator,
    _object_names,
    claim_primary_instance,
    signal_named_event,
)


@pytest.mark.skipif(os.name != "nt", reason="Windows named instance objects")
def test_second_instance_activates_primary_and_update_signal_is_distinct():
    namespace = f"WeChatExporterTest.{uuid.uuid4().hex}"
    primary = claim_primary_instance(namespace)
    assert isinstance(primary, WindowsInstanceCoordinator)
    shown = threading.Event()
    update_exit = threading.Event()
    primary.start(on_show=shown.set, on_update_exit=update_exit.set)
    try:
        assert claim_primary_instance(namespace) is None
        assert shown.wait(1)

        update_name = _object_names(namespace)[2]
        assert signal_named_event(update_name) is True
        assert update_exit.wait(1)
    finally:
        primary.close()

    replacement = claim_primary_instance(namespace)
    assert isinstance(replacement, WindowsInstanceCoordinator)
    replacement.close()


def test_secondary_gui_launch_returns_before_creating_tk(monkeypatch):
    monkeypatch.setattr(gui, "require_signature_integrity", lambda: None)
    monkeypatch.setattr(gui, "claim_primary_instance", lambda: None)
    monkeypatch.setattr(
        gui.tk,
        "Tk",
        lambda: pytest.fail("secondary launch must not create another GUI"),
    )

    gui.main()


def test_primary_gui_always_releases_instance_coordinator(monkeypatch):
    calls = []

    class Coordinator:
        def start(self, **callbacks):
            calls.append(("start", tuple(sorted(callbacks))))

        def close(self):
            calls.append(("close",))

    class Root:
        def mainloop(self):
            raise RuntimeError("test mainloop failure")

    coordinator = Coordinator()
    monkeypatch.setattr(gui, "require_signature_integrity", lambda: None)
    monkeypatch.setattr(gui, "claim_primary_instance", lambda: coordinator)
    monkeypatch.setattr(gui, "_enable_windows_high_dpi", lambda: None)
    monkeypatch.setattr(gui, "_configure_native_fonts", lambda _root: None)
    monkeypatch.setattr(gui.tk, "Tk", Root)
    monkeypatch.setattr(
        gui,
        "ExporterApp",
        lambda _root: SimpleNamespace(events=Queue()),
    )

    with pytest.raises(RuntimeError, match="test mainloop failure"):
        gui.main()

    assert calls == [
        ("start", ("on_show", "on_update_exit")),
        ("close",),
    ]


@pytest.mark.skipif(os.name != "nt", reason="Windows named instance objects")
def test_powershell_build_signal_reaches_primary_instance():
    namespace = f"WeChatExporterTest.{uuid.uuid4().hex}"
    primary = claim_primary_instance(namespace)
    assert isinstance(primary, WindowsInstanceCoordinator)
    update_exit = threading.Event()
    primary.start(on_show=lambda: None, on_update_exit=update_exit.set)
    event_name = _object_names(namespace)[2]
    environment = os.environ.copy()
    environment["WECHAT_EXPORTER_UPDATE_EVENT_TEST"] = event_name
    command = (
        "$event=[System.Threading.EventWaitHandle]::OpenExisting("
        "$env:WECHAT_EXPORTER_UPDATE_EVENT_TEST); "
        "try { if (-not $event.Set()) { exit 2 } } finally { $event.Dispose() }"
    )
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ],
            env=environment,
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr.decode(errors="replace")
        assert update_exit.wait(1)
    finally:
        primary.close()
