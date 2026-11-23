"""Input injection: mouse, keyboard, and the timing substrate underneath both.

Measured constraints that shape this file:

* `SendInput` with one event: 26 us. With 8 events in one call: 5.9 us/event.
  With 32: 13.2 us/event. With 128: 16.0 us/event. The win peaks around 8 and
  then reverses, so `_BATCH = 8` is a measured constant, not a guess.
* Absolute mouse positioning takes normalised 0..65535 coordinates spanning the
  *virtual* desktop. Without `MOUSEEVENTF_VIRTUALDESK` every coordinate on a
  multi-monitor setup silently lands on the primary display.
* `time.sleep()` on Windows rounds up to the system timer resolution, which is
  15.6 ms by default - larger than the entire 1-7 ms organic polling window
  this library wants to emit. Fixed below with `timeBeginPeriod` plus a
  spin-tail; both are measured in the test suite rather than assumed.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import time

from . import _win32 as _w
from . import humanize as _h
from .screen import Point

_BATCH = 8  # measured optimum; see module docstring


# --------------------------------------------------------------------------- #
# Timing
# --------------------------------------------------------------------------- #
class Clock:
    """Sub-millisecond sleeping.

    `timeBeginPeriod(1)` drops the global timer tick to ~1 ms, and the last
    fraction is spun out against `perf_counter`. The spin window is kept small
    deliberately: burning a core to be punctual is a real cost, and a 1.5 ms
    tail is the measured break-even against the scheduler's own jitter.
    """

    def __init__(self):
        self._winmm = None
        self._period = 0
        self.spin_threshold_s = 0.0015

    def begin_precision(self) -> bool:
        if self._period:
            return True
        try:
            self._winmm = ctypes.WinDLL("winmm")
            if self._winmm.timeBeginPeriod(1) == 0:
                self._period = 1
                return True
        except Exception:
            pass
        return False

    def end_precision(self):
        if self._period and self._winmm:
            self._winmm.timeEndPeriod(self._period)
            self._period = 0

    def sleep(self, seconds: float):
        if seconds <= 0:
            return
        deadline = time.perf_counter() + seconds
        coarse = seconds - self.spin_threshold_s
        if coarse > 0:
            time.sleep(coarse)
        while time.perf_counter() < deadline:
            pass

    def sleep_ms(self, ms: float):
        self.sleep(ms / 1000.0)

    def __del__(self):
        try:
            self.end_precision()
        except Exception:
            pass


_clock = Clock()


# --------------------------------------------------------------------------- #
# Event construction
# --------------------------------------------------------------------------- #
def _norm_abs(x: int, y: int) -> tuple[int, int]:
    """Screen pixels -> the 0..65535 virtual-desktop space SendInput wants."""
    vx, vy, vw, vh = _w.virtual_screen_rect()
    nx = int(round((x - vx) * 65535 / max(1, vw - 1)))
    ny = int(round((y - vy) * 65535 / max(1, vh - 1)))
    return (max(0, min(65535, nx)), max(0, min(65535, ny)))


def _mouse_event(flags: int, x: int = 0, y: int = 0, data: int = 0) -> _w.INPUT:
    ev = _w.INPUT(type=_w.INPUT_MOUSE)
    ev.u.mi = _w.MOUSEINPUT(x, y, data, flags, 0, 0)
    return ev


def _key_event(vk: int, flags: int = 0, scan: int = 0) -> _w.INPUT:
    ev = _w.INPUT(type=_w.INPUT_KEYBOARD)
    ev.u.ki = _w.KEYBDINPUT(vk, scan, flags, 0, 0)
    return ev


def _send(events: list[_w.INPUT]) -> int:
    """Push events in measured-optimal chunks. Returns the count accepted.

    A short write here is not a detail to swallow: it means the input queue
    rejected events (a UIPI boundary, a secure desktop, or a session lock), and
    a script that keeps going on the assumption its click landed is worse than
    one that stops.
    """
    sent = 0
    n = len(events)
    i = 0
    while i < n:
        chunk = events[i:i + _BATCH]
        arr = (_w.INPUT * len(chunk))(*chunk)
        got = _w.user32.SendInput(len(chunk), arr, _w.INPUT_SIZE)
        sent += got
        if got != len(chunk):
            err = ctypes.get_last_error()
            raise InputBlocked(
                f"SendInput accepted {got}/{len(chunk)} events (WinError {err}). "
                "Usual causes: the foreground window runs elevated and this "
                "process does not, a UAC prompt or the lock screen is up, or "
                "the session is disconnected."
            )
        i += _BATCH
    return sent


class InputBlocked(RuntimeError):
    """Input was refused by the OS, not merely ineffective."""


# --------------------------------------------------------------------------- #
# Mouse
# --------------------------------------------------------------------------- #
_BUTTONS = {
    "left": (_w.MOUSEEVENTF_LEFTDOWN, _w.MOUSEEVENTF_LEFTUP, 0),
    "right": (_w.MOUSEEVENTF_RIGHTDOWN, _w.MOUSEEVENTF_RIGHTUP, 0),
    "middle": (_w.MOUSEEVENTF_MIDDLEDOWN, _w.MOUSEEVENTF_MIDDLEUP, 0),
    "x1": (_w.MOUSEEVENTF_XDOWN, _w.MOUSEEVENTF_XUP, 1),
    "x2": (_w.MOUSEEVENTF_XDOWN, _w.MOUSEEVENTF_XUP, 2),
}


class Mouse:
    def __init__(self, profile: _h.Profile | None = None):
        self.profile = profile or _h.INSTANT
        self._actions = 0

    # -- state ------------------------------------------------------------- #
    @property
    def position(self) -> Point:
        pt = w.POINT()
        _w.user32.GetCursorPos(ctypes.byref(pt))
        return Point(pt.x, pt.y)

    # -- motion ------------------------------------------------------------ #
    def move(self, x: int, y: int, *, humanize=None) -> Point:
        """Move the pointer. With a profile, follows a plausible trajectory;
        without one, a single absolute jump (~26 us)."""
        p = self.profile if humanize is None else _h.resolve(humanize)
        flags = _w.MOUSEEVENTF_MOVE | _w.MOUSEEVENTF_ABSOLUTE | _w.MOUSEEVENTF_VIRTUALDESK

        if p is _h.INSTANT or p.steps_per_100px <= 0:
            nx, ny = _norm_abs(x, y)
            _send([_mouse_event(flags, nx, ny)])
            return Point(x, y)

        start = self.position
        pts = p.path(start.x, start.y, x, y)
        # Emit in batches, pacing between them. Inside a batch the events share
        # one input tick, which is exactly how a real HID report arrives.
        buf: list[_w.INPUT] = []
        for px, py in pts:
            nx, ny = _norm_abs(px, py)
            buf.append(_mouse_event(flags, nx, ny))
            if len(buf) >= _BATCH:
                _send(buf)
                buf = []
                _clock.sleep_ms(p._span(p.poll_interval_ms, self._actions))
        if buf:
            _send(buf)
        self._actions += 1
        return Point(x, y)

    def move_relative(self, dx: int, dy: int, *, humanize=None) -> Point:
        here = self.position
        return self.move(here.x + dx, here.y + dy, humanize=humanize)

    # -- buttons ----------------------------------------------------------- #
    def down(self, button: str = "left"):
        d, _u, data = _BUTTONS[button]
        _send([_mouse_event(d, 0, 0, data)])

    def up(self, button: str = "left"):
        _d, u, data = _BUTTONS[button]
        _send([_mouse_event(u, 0, 0, data)])

    def click(self, x: int | None = None, y: int | None = None, *,
              button: str = "left", count: int = 1, humanize=None) -> Point:
        p = self.profile if humanize is None else _h.resolve(humanize)
        if x is not None and y is not None:
            self.move(x, y, humanize=humanize)
        d, u, data = _BUTTONS[button]

        if p is _h.INSTANT:
            # one syscall for the whole gesture: down+up pairs batched together
            _send([ev for _ in range(count)
                   for ev in (_mouse_event(d, 0, 0, data), _mouse_event(u, 0, 0, data))])
        else:
            for i in range(count):
                _send([_mouse_event(d, 0, 0, data)])
                _clock.sleep_ms(p._span(p.click_hold_ms, self._actions))
                _send([_mouse_event(u, 0, 0, data)])
                if i < count - 1:
                    _clock.sleep_ms(p._span((60.0, 130.0), self._actions))
            self._actions += 1
        return self.position

    def double_click(self, x=None, y=None, *, button="left", humanize=None):
        return self.click(x, y, button=button, count=2, humanize=humanize)

    def drag(self, x0: int, y0: int, x1: int, y1: int, *,
             button: str = "left", humanize=None):
        """Press, travel, release. The travel is a real path even in instant
        mode, because many UIs ignore a drag that teleports: they need
        intermediate move messages to start their drag state machine."""
        p = self.profile if humanize is None else _h.resolve(humanize)
        self.move(x0, y0, humanize=humanize)
        self.down(button)
        try:
            if p is _h.INSTANT:
                # minimum viable path: a handful of samples so the target's
                # drag handler actually fires
                flags = (_w.MOUSEEVENTF_MOVE | _w.MOUSEEVENTF_ABSOLUTE
                         | _w.MOUSEEVENTF_VIRTUALDESK)
                evs = []
                for i in range(1, 9):
                    t = i / 8
                    nx, ny = _norm_abs(int(x0 + (x1 - x0) * t), int(y0 + (y1 - y0) * t))
                    evs.append(_mouse_event(flags, nx, ny))
                _send(evs)
            else:
                self.move(x1, y1, humanize=humanize)
        finally:
            self.up(button)
        return Point(x1, y1)

    def scroll(self, clicks: int, *, horizontal: bool = False, humanize=None):
        p = self.profile if humanize is None else _h.resolve(humanize)
        flags = _w.MOUSEEVENTF_HWHEEL if horizontal else _w.MOUSEEVENTF_WHEEL
        step = 120 if clicks > 0 else -120
        n = abs(clicks)
        if p is _h.INSTANT:
            _send([_mouse_event(flags, 0, 0, step) for _ in range(n)])
        else:
            for _ in range(n):
                _send([_mouse_event(flags, 0, 0, step)])
                _clock.sleep_ms(p._span((35.0, 95.0), self._actions))
            self._actions += 1
