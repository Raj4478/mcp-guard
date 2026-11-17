"""The macOS full-screen Spaces fix (mcp_guard/ui/macos.py)."""

from __future__ import annotations

import sys

import pytest

from mcp_guard.ui import macos


@pytest.mark.skipif(sys.platform == "darwin", reason="checks the non-macOS path")
def test_does_nothing_off_macos():
    assert macos.make_accessory_app() is None
    assert macos.float_over_all_spaces("anything") == 0


@pytest.mark.skipif(sys.platform != "darwin", reason="needs AppKit")
def test_popup_window_joins_all_spaces():
    """Real Tk window: the Objective-C calls must find it and set both flags."""
    tk = pytest.importorskip("tkinter")
    from mcp_guard.ui.tk_popup import POPUP_TITLE

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display: {exc}")
    try:
        macos.make_accessory_app()
        win = tk.Toplevel(root)
        win.title(POPUP_TITLE)
        win.update_idletasks()
        root.update()
        assert macos.float_over_all_spaces(POPUP_TITLE) == 1
        assert macos.float_over_all_spaces("no window has this title") == 0

        # Read the flags back from the NSWindow.
        import ctypes
        windows = macos._send(ctypes.c_void_p, macos._shared_app(), b"windows")
        count = macos._send(ctypes.c_ulong, windows, b"count")
        behaviours = []
        for i in range(count):
            w = macos._send(ctypes.c_void_p, windows, b"objectAtIndex:", i, argtypes=(ctypes.c_ulong,))
            title = macos._send(ctypes.c_void_p, w, b"title")
            raw = macos._send(ctypes.c_char_p, title, b"UTF8String") if title else None
            if raw and raw.decode() == POPUP_TITLE:
                behaviours.append(macos._send(ctypes.c_ulong, w, b"collectionBehavior"))
        assert behaviours and all(b & macos._CAN_JOIN_ALL_SPACES and b & macos._FULL_SCREEN_AUXILIARY
                                  for b in behaviours)
    finally:
        root.destroy()
