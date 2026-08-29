"""Custom title bar plumbing: window controls bridge, embedded fallback, markup."""

from __future__ import annotations

from pathlib import Path

from saturday import webui


class _FakeWin:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def minimize(self) -> None:
        self.calls.append("minimize")

    def maximize(self) -> None:
        self.calls.append("maximize")

    def restore(self) -> None:
        self.calls.append("restore")

    def destroy(self) -> None:
        self.calls.append("destroy")


def test_window_controls_minimize_via_close():
    win = _FakeWin()
    ctl = webui._WindowControls(win)
    assert ctl.win_min() is True
    assert ctl.win_close() is True
    assert win.calls == ["minimize", "destroy"]


def test_window_controls_maximizes_then_restores():
    win = _FakeWin()
    ctl = webui._WindowControls(win)
    assert ctl.win_max() is True  # now maximized
    assert win.calls == ["maximize"]
    assert ctl.win_max() is False  # now restored
    assert win.calls == ["maximize", "restore"]


def test_embedded_window_falls_back_without_pywebview(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "webview":
            raise ImportError("pywebview not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert webui.launch_embedded_window("http://127.0.0.1:1/", 800, 600) is False


def test_titlebar_markup_present():
    assets = Path(webui.__file__).resolve().parent / "webui_assets"
    html = (assets / "index.html").read_text(encoding="utf-8")
    js = (assets / "app.js").read_text(encoding="utf-8")
    assert 'id="titlebar"' in html
    assert "pywebview-drag-region" in html
    assert "tbMax" in html and "tbClose" in html
    assert 'enableTitleBar' in js and "pywebviewready" in js
