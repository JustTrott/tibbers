"""The Windows shell's window geometry: never remember or reuse a position
that is off every monitor (the minimised-window sentinel in particular)."""

from types import SimpleNamespace

from tibbers import _shell_windows as shell


SCREEN = [SimpleNamespace(x=0, y=0, width=1536, height=864)]


def test_parked_position_is_off_screen():
    # -32000 scaled by 125%, exactly what pywebview reports for a minimise.
    assert not shell.on_screen(-25600, -25600, SCREEN)
    assert not shell.on_screen(-32000, -32000, SCREEN)


def test_ordinary_positions_are_on_screen():
    assert shell.on_screen(480, 200, SCREEN)
    assert shell.on_screen(0, 0, SCREEN)
    assert shell.on_screen(-20, -10, SCREEN)          # slightly past an edge


def test_beyond_every_monitor_is_off_screen():
    assert not shell.on_screen(3000, 200, SCREEN)
    assert not shell.on_screen(200, 2000, SCREEN)
    assert not shell.on_screen(None, None, SCREEN)


def test_no_screen_list_only_rejects_the_sentinel():
    assert shell.on_screen(5000, 5000, [])
    assert not shell.on_screen(-25600, 100, [])


class _Prefs:
    def __init__(self, box=None):
        self.box = dict(box or {})
        self.saved = []

    def geometry(self, name):
        return dict(self.box)

    def remember_geometry(self, name, **fields):
        self.saved.append(fields)
        self.box.update(fields)


def test_moved_to_parked_position_is_not_remembered(monkeypatch):
    monkeypatch.setattr(shell, "on_screen", lambda x, y, screens=None: x > -10000)
    prefs = _Prefs()
    w = shell.Windows("http://127.0.0.1:7777", prefs)
    w._moved("settings", 480, 200)
    w._moved("settings", -25600, -25600)
    assert prefs.saved == [{"x": 480, "y": 200}]


def test_stored_parked_position_falls_back_to_centred(monkeypatch):
    monkeypatch.setattr(shell, "on_screen", lambda x, y, screens=None: x is not None and x > -10000)
    prefs = _Prefs({"x": -25600, "y": -25600, "width": 560, "height": 460})
    w = shell.Windows("http://127.0.0.1:7777", prefs)
    box = w._stored("settings", shell.SETTINGS_SIZE)
    assert (box["x"], box["y"]) == (None, None)
    assert (box["width"], box["height"]) == (560, 460)


def test_window_ops_never_touch_the_ui_synchronously_off_thread():
    """The freeze root cause: a WinForms property set (TopMost especially)
    from the League-watcher thread marshals synchronously and deadlocks the
    GUI thread. Every window op must go through _gui_run, and no synchronous
    `window.on_top = x` may survive anywhere in the module."""
    import inspect
    src = inspect.getsource(shell)
    assert ".on_top =" not in src.replace("on_top=(name", "")
    # _gui_run posts to the GUI thread and does not wait (BeginInvoke).
    assert "BeginInvoke" in inspect.getsource(shell._gui_run)
    for method in ("_show", "_hide", "set_on_top", "stand_down"):
        assert "_gui_run" in inspect.getsource(getattr(shell.Windows, method)), method


def test_gui_run_runs_inline_when_no_window_exists_yet():
    ran = []
    shell._gui_run(lambda: ran.append(1))  # nothing created: runs in place
    assert ran == [1]

