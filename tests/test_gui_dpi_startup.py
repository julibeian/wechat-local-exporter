"""Exercise the packaged entry point's DPI setup in a fresh process."""

import subprocess
import sys
import textwrap


def test_dpi_aware_cold_start_keeps_contact_rows_and_export_action_visible():
    script = textwrap.dedent('''
        import time
        import tkinter as tk
        from pathlib import Path
        from types import SimpleNamespace
        from wechat_exporter import gui, update_ui
        from wechat_exporter.models import Conversation, AccountLocation
        from wechat_exporter.service import RestartRequired

        gui._enable_windows_high_dpi()
        root = tk.Tk()
        root.attributes('-alpha', 0)
        gui._configure_native_fonts(root)
        gui.ExporterApp._detect = lambda self: ('test', Path('Weixin.exe'))
        update_ui.UpdateManager.check = lambda self, **kw: self.result
        def skip_connect(*args, **kwargs):
            raise RestartRequired()
        gui.ConnectionManager.connect = skip_connect
        gui.messagebox.askyesno = lambda *args, **kwargs: False
        app = gui.ExporterApp(root)
        app.service = SimpleNamespace(
            account=AccountLocation(Path('tmp'), 'qa', 'QA'),
            archive=SimpleNamespace(self_conversation=lambda: Conversation('self', '我自己', is_self=True)),
            close=lambda: None,
        )
        app.conversations = [Conversation('room@chatroom', '课程资料群', is_group=True)]
        app._filter_conversations()
        app.tree.selection_set(tuple(app._conversation_by_iid)[-1])
        app._selection_changed()
        # No geometry or sash reset: this is the real DPI-aware startup path.
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)
        row_height = int(gui.ttk.Style(root).lookup('Treeview', 'rowheight'))
        assert app.tree.winfo_height() >= row_height * 5, (
            root.geometry(), app.tree.winfo_height(), row_height
        )
        for widget in (app.export_button, app.history_button):
            assert widget.winfo_viewable()
            assert widget.winfo_y() >= 0
            assert widget.winfo_rooty() + widget.winfo_height() < root.winfo_rooty() + root.winfo_height()
        app.updates.close()
        root.destroy()
    ''')
    result = subprocess.run(
        [sys.executable, '-c', script], capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
