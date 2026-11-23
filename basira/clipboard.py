"""Clipboard read and write using Win32 API.
Zero subprocess, zero codepage issues, sub-millisecond roundtrip.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import time

from . import _win32 as _w

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

_w.user32.OpenClipboard.argtypes = (w.HWND,)
_w.user32.OpenClipboard.restype = w.BOOL
_w.user32.CloseClipboard.restype = w.BOOL
_w.user32.EmptyClipboard.restype = w.BOOL
_w.user32.GetClipboardData.argtypes = (w.UINT,)
_w.user32.GetClipboardData.restype = ctypes.c_void_p
_w.user32.SetClipboardData.argtypes = (w.UINT, ctypes.c_void_p)
_w.user32.SetClipboardData.restype = ctypes.c_void_p

_w.kernel32.GlobalAlloc.argtypes = (w.UINT, ctypes.c_size_t)
_w.kernel32.GlobalAlloc.restype = ctypes.c_void_p
_w.kernel32.GlobalLock.argtypes = (ctypes.c_void_p,)
_w.kernel32.GlobalLock.restype = ctypes.c_void_p
_w.kernel32.GlobalUnlock.argtypes = (ctypes.c_void_p,)
_w.kernel32.GlobalUnlock.restype = w.BOOL


def get_text(*, timeout: float = 0.5) -> str:
    """Read UTF-16 text from the system clipboard."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if _w.user32.OpenClipboard(0):
            try:
                h = _w.user32.GetClipboardData(CF_UNICODETEXT)
                if not h:
                    return ""
                ptr = _w.kernel32.GlobalLock(h)
                if not ptr:
                    return ""
                try:
                    return ctypes.wstring_at(ptr)
                finally:
                    _w.kernel32.GlobalUnlock(h)
            finally:
                _w.user32.CloseClipboard()
        time.sleep(0.01)
    return ""


def set_text(text: str, *, timeout: float = 0.5) -> bool:
    """Write UTF-16 text into the system clipboard."""
    data = (text + "\0").encode("utf-16-le")
    h = _w.kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not h:
        return False
    ptr = _w.kernel32.GlobalLock(h)
    if not ptr:
        return False
    ctypes.memmove(ptr, data, len(data))
    _w.kernel32.GlobalUnlock(h)

    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if _w.user32.OpenClipboard(0):
            try:
                _w.user32.EmptyClipboard()
                if _w.user32.SetClipboardData(CF_UNICODETEXT, h):
                    return True
            finally:
                _w.user32.CloseClipboard()
        time.sleep(0.01)
    return False


def clear() -> bool:
    """Clear the clipboard."""
    if _w.user32.OpenClipboard(0):
        try:
            return bool(_w.user32.EmptyClipboard())
        finally:
            _w.user32.CloseClipboard()
    return False
