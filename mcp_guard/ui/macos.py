"""macOS: show approval popups over full-screen apps.

On macOS every full-screen app gets its own Space (virtual desktop). A new
window from another app opens on that app's own Space - normally the plain
desktop - and never over someone else's full-screen Space. ``-topmost`` does
not help: it only orders windows *within* a Space. So when Claude runs full
screen, the popup is created, correctly "on top", on a Space the user is not
looking at, and the request times out.

The fix needs two AppKit settings that Tk does not expose:

1. The daemon becomes an *accessory* app (no Dock icon, like menu-bar
   utilities). macOS only lets accessory apps put windows over a
   full-screen Space.
2. Each popup's NSWindow gets ``canJoinAllSpaces | fullScreenAuxiliary`` and
   is re-ordered with ``orderFrontRegardless``, which shows it *without*
   activating the app, so the user stays in Claude's Space.

MCP Guard uses only the standard library, so instead of PyObjC this module
calls the Objective-C runtime through ctypes. ``[obj sel:arg]`` compiles to
``objc_msgSend(obj, sel, arg)``. On Apple Silicon that function must be called
through a prototype that matches the method exactly (a variadic call puts the
arguments in the wrong registers), so every call builds its own CFUNCTYPE.

Everything here is cosmetic and best-effort: on other platforms, or on any
error, the functions do nothing. The worst case is the old behaviour (popup on
another Space -> timeout -> block), which is still fail-closed.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import sys
from typing import Any

log = logging.getLogger(__name__)

_ACCESSORY_POLICY = 1               # NSApplicationActivationPolicyAccessory
_CAN_JOIN_ALL_SPACES = 1 << 0       # NSWindowCollectionBehaviorCanJoinAllSpaces
_FULL_SCREEN_AUXILIARY = 1 << 8     # NSWindowCollectionBehaviorFullScreenAuxiliary

_lib: Any = None


def _runtime() -> Any:
    """libobjc, loaded once."""
    global _lib
    if _lib is None:
        lib = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc") or "/usr/lib/libobjc.A.dylib")
        lib.objc_getClass.restype = ctypes.c_void_p
        lib.objc_getClass.argtypes = [ctypes.c_char_p]
        lib.sel_registerName.restype = ctypes.c_void_p
        lib.sel_registerName.argtypes = [ctypes.c_char_p]
        _lib = lib
    return _lib


def _send(restype: Any, obj: Any, selector: bytes, *args: Any, argtypes: tuple = ()) -> Any:
    """``[obj selector:args...]`` with an exact prototype (required on arm64)."""
    lib = _runtime()
    prototype = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)
    fn = prototype(("objc_msgSend", lib))
    return fn(obj, lib.sel_registerName(selector), *args)


def _shared_app() -> Any:
    cls = _runtime().objc_getClass(b"NSApplication")
    if not cls:
        raise RuntimeError("AppKit is not loaded")
    return _send(ctypes.c_void_p, cls, b"sharedApplication")


def make_accessory_app() -> None:
    """Turn this process into an accessory app. Call once, after ``tk.Tk()``."""
    if sys.platform != "darwin":
        return
    try:
        _send(ctypes.c_bool, _shared_app(), b"setActivationPolicy:", _ACCESSORY_POLICY,
              argtypes=(ctypes.c_long,))
    except Exception:
        log.debug("could not switch to the accessory activation policy", exc_info=True)


def float_over_all_spaces(title: str) -> int:
    """Let every window titled ``title`` show over any Space, full-screen included.

    Tk does not expose the NSWindow behind a Toplevel, so windows are found by
    title. Returns how many windows were updated (0 when not on macOS).
    """
    if sys.platform != "darwin":
        return 0
    updated = 0
    try:
        windows = _send(ctypes.c_void_p, _shared_app(), b"windows")
        count = _send(ctypes.c_ulong, windows, b"count")
        for i in range(count):
            win = _send(ctypes.c_void_p, windows, b"objectAtIndex:", i, argtypes=(ctypes.c_ulong,))
            ns_title = _send(ctypes.c_void_p, win, b"title")
            raw = _send(ctypes.c_char_p, ns_title, b"UTF8String") if ns_title else None
            if not raw or raw.decode("utf-8", "replace") != title:
                continue
            behaviour = _send(ctypes.c_ulong, win, b"collectionBehavior")
            _send(None, win, b"setCollectionBehavior:",
                  behaviour | _CAN_JOIN_ALL_SPACES | _FULL_SCREEN_AUXILIARY, argtypes=(ctypes.c_ulong,))
            # The window was already placed on a Space when Tk mapped it; new
            # flags do not move it. Hiding and re-showing does. orderFrontRegardless
            # shows it without activating the app, so the user stays where they are.
            _send(None, win, b"orderOut:", None, argtypes=(ctypes.c_void_p,))
            _send(None, win, b"orderFrontRegardless")
            updated += 1
    except Exception:
        log.debug("could not make the popup join all Spaces", exc_info=True)
    return updated
