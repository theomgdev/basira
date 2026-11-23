"""Window discovery and control.

Cheap enough to be used freely: a full `EnumWindows` sweep over 264 top-level
windows measured 46 us on this host, and `GetForegroundWindow` is 0.1 us. So
window lookup needs no cache, no handle bookkeeping, and no "find once and hope
it stays valid" pattern - a script can simply re-find its window every
iteration and pay nothing.

What is NOT cheap is waiting for a window to become ready. `IsWindowVisible`
returns true for a window that is still a grey rectangle with no content, which
is the window-automation equivalent of `readyState === 'complete'` on a SPA.
`responsive()` and `wait_ready()` below exist because acting on a cold window is
the most common source of silent failure.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import time
from typing import Callable, Iterator

from . import _win32 as _w
from .screen import Point, Region

SW_HIDE, SW_SHOWNORMAL, SW_SHOWMINIMIZED = 0, 1, 2
SW_MAXIMIZE, SW_SHOW, SW_MINIMIZE, SW_RESTORE = 3, 5, 6, 9

GA_PARENT, GA_ROOT, GA_ROOTOWNER = 1, 2, 3
GWL_STYLE, GWL_EXSTYLE = -16, -20
WS_VISIBLE = 0x10000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008

HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = 0x0002, 0x0001, 0x0010

_w.user32.SendMessageTimeoutW.argtypes = (
    w.HWND, w.UINT, w.WPARAM, w.LPARAM, w.UINT, w.UINT, ctypes.POINTER(w.DWORD))
_w.user32.SendMessageTimeoutW.restype = ctypes.c_long


class Window:
    """A live handle. Every property reads the OS fresh, because a cached title
    or rect is a lie the moment the user drags the window."""

    __slots__ = ("hwnd",)

    def __init__(self, hwnd):
        self.hwnd = int(hwnd)

    # -- identity ---------------------------------------------------------- #
    def __repr__(self) -> str:
        return f"<Window {self.hwnd} [{self.class_name}] {self.title[:40]!r}>"

    def __eq__(self, other) -> bool:
        return isinstance(other, Window) and other.hwnd == self.hwnd

    def __hash__(self) -> int:
        return hash(self.hwnd)

    def __bool__(self) -> bool:
        return self.exists

    @property
    def exists(self) -> bool:
        return bool(_w.user32.IsWindow(self.hwnd))

    @property
    def title(self) -> str:
        n = _w.user32.GetWindowTextLengthW(self.hwnd)
        buf = ctypes.create_unicode_buffer(n + 2)
        _w.user32.GetWindowTextW(self.hwnd, buf, n + 2)
        return buf.value

    @property
    def class_name(self) -> str:
        buf = ctypes.create_unicode_buffer(256)
        _w.user32.GetClassNameW(self.hwnd, buf, 256)
        return buf.value

    @property
    def pid(self) -> int:
        p = w.DWORD()
        _w.user32.GetWindowThreadProcessId(self.hwnd, ctypes.byref(p))
        return p.value

    @property
    def process_name(self) -> str:
        """Image name of the owning process. Needs no elevation for normal
        user processes thanks to QueryFullProcessImageName."""
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = _w.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, self.pid)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = w.DWORD(1024)
            if _w.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return buf.value.rsplit("\\", 1)[-1]
            return ""
        finally:
            _w.kernel32.CloseHandle(h)

    # -- geometry ---------------------------------------------------------- #
    @property
    def rect(self) -> Region:
        r = w.RECT()
        _w.user32.GetWindowRect(self.hwnd, ctypes.byref(r))
        return Region(r.left, r.top, r.right - r.left, r.bottom - r.top)

    @property
    def client_rect(self) -> Region:
        """The drawable area in screen coordinates - what you actually want to
        capture or click into, since the window rect includes the invisible
        resize border DWM adds (8 px here, which is why naive automation clicks
        land slightly off)."""
        r = w.RECT()
        _w.user32.GetClientRect(self.hwnd, ctypes.byref(r))
        pt = w.POINT(0, 0)
        _w.user32.ClientToScreen(self.hwnd, ctypes.byref(pt))
        return Region(pt.x, pt.y, r.right, r.bottom)

    def to_client(self, x: int, y: int) -> Point:
        pt = w.POINT(x, y)
        _w.user32.ScreenToClient(self.hwnd, ctypes.byref(pt))
        return Point(pt.x, pt.y)

    def to_screen(self, x: int, y: int) -> Point:
        pt = w.POINT(x, y)
        _w.user32.ClientToScreen(self.hwnd, ctypes.byref(pt))
        return Point(pt.x, pt.y)

    # -- state ------------------------------------------------------------- #
    @property
    def visible(self) -> bool:
        return bool(_w.user32.IsWindowVisible(self.hwnd))

    @property
    def minimized(self) -> bool:
        return bool(_w.user32.IsIconic(self.hwnd))

    @property
    def maximized(self) -> bool:
        return bool(_w.user32.IsZoomed(self.hwnd))

    @property
    def enabled(self) -> bool:
        return bool(_w.user32.IsWindowEnabled(self.hwnd))

    @property
    def active(self) -> bool:
        return _w.user32.GetForegroundWindow() == self.hwnd

    @property
    def topmost(self) -> bool:
        return bool(_w.user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)

    def responsive(self, timeout_ms: int = 200) -> bool:
        """Is the window's message loop actually pumping?

        A hung or still-initialising window passes `visible` and `enabled` but
        never answers WM_NULL. This is the difference between "the window
        exists" and "the window can receive my click".
        """
        res = w.DWORD()
        SMTO_ABORTIFHUNG = 0x0002
        ok = _w.user32.SendMessageTimeoutW(
            self.hwnd, 0x0000, 0, 0, SMTO_ABORTIFHUNG, timeout_ms, ctypes.byref(res))
        return bool(ok)

    def wait_ready(self, timeout: float = 10.0, *, min_area: int = 100) -> bool:
        """Visible, non-minimised, responsive, and actually sized. The gate to
        put in front of the first click on a freshly launched window."""
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if (self.exists and self.visible and not self.minimized
                    and self.enabled and self.responsive()):
                r = self.rect
                if r.width * r.height >= min_area:
                    return True
            time.sleep(0.016)
        return False

    # -- control ----------------------------------------------------------- #
    def activate(self, *, timeout: float = 1.0) -> bool:
        """Bring to foreground and verify it took.

        Windows refuses foreground changes from a process that does not own the
        current foreground window (SetForegroundWindow silently returns false),
        so this reports the real outcome instead of assuming success. Attaching
        to the foreground thread's input queue is the sanctioned way to earn
        the right.
        """
        if self.minimized:
            _w.user32.ShowWindow(self.hwnd, SW_RESTORE)
        if _w.user32.SetForegroundWindow(self.hwnd):
            return True

        fg = _w.user32.GetForegroundWindow()
        fg_tid = _w.user32.GetWindowThreadProcessId(fg, None)
        my_tid = _w.kernel32.GetCurrentThreadId()
        attached = _w.user32.AttachThreadInput(my_tid, fg_tid, True)
        try:
            _w.user32.BringWindowToTop(self.hwnd)
            _w.user32.SetForegroundWindow(self.hwnd)
        finally:
            if attached:
                _w.user32.AttachThreadInput(my_tid, fg_tid, False)

        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if self.active:
                return True
            time.sleep(0.01)
        return False

    def minimize(self):
        _w.user32.ShowWindow(self.hwnd, SW_MINIMIZE)

    def maximize(self):
        _w.user32.ShowWindow(self.hwnd, SW_MAXIMIZE)

    def restore(self):
        _w.user32.ShowWindow(self.hwnd, SW_RESTORE)

    def hide(self):
        _w.user32.ShowWindow(self.hwnd, SW_HIDE)

    def show(self):
        _w.user32.ShowWindow(self.hwnd, SW_SHOW)

    def close(self):
        _w.user32.PostMessageW(self.hwnd, 0x0010, 0, 0)  # WM_CLOSE

    def move(self, x: int = None, y: int = None, width: int = None, height: int = None):
        r = self.rect
        _w.user32.MoveWindow(
            self.hwnd,
            r.x if x is None else x, r.y if y is None else y,
            r.width if width is None else width,
            r.height if height is None else height, True)

    def set_topmost(self, on: bool = True):
        _w.user32.SetWindowPos(self.hwnd, HWND_TOPMOST if on else HWND_NOTOPMOST,
                               0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    # -- children ---------------------------------------------------------- #
    def children(self) -> list["Window"]:
        out = []

        @_w.WNDENUMPROC
        def cb(h, _l):
            out.append(Window(h))
            return True

        _w.user32.EnumChildWindows(self.hwnd, cb, 0)
        return out

    def capture(self, screen, *, client_only: bool = True):
        """A Frame of just this window, correct even when it is occluded."""
        return screen.grab_window(self.hwnd, client_only=client_only)


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def all_windows(*, visible_only: bool = True, skip_tool_windows: bool = True,
                titled_only: bool = False) -> list[Window]:
    """Every top-level window. ~46 us for 264 windows - call it freely."""
    out: list[Window] = []

    @_w.WNDENUMPROC
    def cb(h, _l):
        if visible_only and not _w.user32.IsWindowVisible(h):
            return True
        if skip_tool_windows:
            if _w.user32.GetWindowLongW(h, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
                return True
        if titled_only and _w.user32.GetWindowTextLengthW(h) == 0:
            return True
        out.append(Window(h))
        return True

    _w.user32.EnumWindows(cb, 0)
    return out


def active_window() -> Window | None:
    h = _w.user32.GetForegroundWindow()
    return Window(h) if h else None


def window_at(x: int, y: int, *, deepest: bool = False) -> Window | None:
    """Which window owns a screen point. `deepest=True` descends into child
    controls, which is what you want when identifying what a click will hit."""
    pt = w.POINT(x, y)
    h = _w.user32.WindowFromPoint(pt)
    if not h:
        return None
    if not deepest:
        h = _w.user32.GetAncestor(h, GA_ROOT) or h
    return Window(h)


def find_windows(title: str | None = None, *, class_name: str | None = None,
                 process: str | None = None, exact: bool = False,
                 predicate: Callable[[Window], bool] | None = None,
                 visible_only: bool = True) -> list[Window]:
    """Match on any combination of title / class / process name.

    Substring and case-insensitive by default: a user searching for 'notepad'
    should find 'Untitled - Notepad' without being taught about exact matching
    first. `exact=True` when they need it.
    """
    def title_ok(wd: Window) -> bool:
        if title is None:
            return True
        t = wd.title
        return t == title if exact else title.casefold() in t.casefold()

    def class_ok(wd: Window) -> bool:
        if class_name is None:
            return True
        c = wd.class_name
        return c == class_name if exact else class_name.casefold() in c.casefold()

    def proc_ok(wd: Window) -> bool:
        if process is None:
            return True
        return process.casefold() in wd.process_name.casefold()

    out = []
    for wd in all_windows(visible_only=visible_only, titled_only=title is not None):
        if title_ok(wd) and class_ok(wd) and proc_ok(wd):
            if predicate is None or predicate(wd):
                out.append(wd)
    return out


def find_window(title: str | None = None, **kw) -> Window | None:
    r = find_windows(title, **kw)
    return r[0] if r else None


def wait_for_window(title: str | None = None, *, timeout: float = 10.0,
                    ready: bool = True, **kw) -> Window | None:
    """Wait for a window to appear and (by default) become usable."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        wd = find_window(title, **kw)
        if wd is not None:
            if not ready:
                return wd
            if wd.wait_ready(timeout=max(0.1, deadline - time.perf_counter())):
                return wd
        time.sleep(0.02)
    return None
