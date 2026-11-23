"""YOLOE-26: the one-import surface.

    from yoloe26 import auto

    auto.find("notepad").activate()
    auto.type("hello")
    auto.click(auto.locate("#ff0000"))

Everything below delegates to the measured primitives in the sibling modules.
The design rule is that the beginner path and the expert path are the SAME
code path, differing only in which arguments are supplied - so there is no
"simple API" that must be abandoned once a script grows up.

Latency, honestly stated (measured on an RTX 3060 Ti, 3440x1440 @144Hz):
    click / key / type .......... tens of microseconds per event
    any screen read ............. 6.94 ms, set by the compositor, not by us
    window find ................. 46 us for a full 264-window sweep
"""
from __future__ import annotations

import time
from typing import Callable, Iterable, Sequence

from . import clipboard
from . import humanize as _h
from .humanize import CAREFUL, FAST, INSTANT, NATURAL, Profile
from .keyboard import KEYS, Keyboard
from .mouse import Clock, InputBlocked, Mouse, _clock
from .screen import Frame, Point, Region, Screen, probe_window_dc
from .window import (Window, active_window, all_windows, find_window,
                     find_windows, wait_for_window, window_at)

__version__ = "0.1.0"


class Timeout(RuntimeError):
    """A wait expired. Carries what was being waited for, because 'Timeout'
    alone in a traceback tells a user nothing about which step failed."""

    def __init__(self, what: str, seconds: float):
        super().__init__(f"timed out after {seconds:.2f}s waiting for {what}")
        self.what = what
        self.seconds = seconds


class Auto:
    """The façade. One instance is created as `yoloe26.auto`; constructing more
    is fine and they share no mutable global state beyond the OS itself."""

    def __init__(self, *, humanize=None, precise_timing: bool = True):
        self.screen = Screen()
        self.profile = _h.resolve(humanize)
        self.mouse = Mouse(self.profile)
        self.keyboard = Keyboard(self.profile)
        self.clock = _clock
        if precise_timing:
            self.clock.begin_precision()

    # ---------------------------------------------------------------- config #
    def humanize(self, spec=True) -> "Auto":
        """Turn organic behaviour on (`auto.humanize()`), off
        (`auto.humanize(False)`), or to a named/custom profile. Returns self so
        it chains."""
        self.profile = _h.resolve(spec)
        self.mouse.profile = self.profile
        self.keyboard.profile = self.profile
        return self

    @property
    def humanized(self) -> bool:
        return self.profile is not _h.INSTANT

    def describe_environment(self) -> dict:
        """What the library actually measured about this machine. Printing this
        is the first thing to do when a script behaves differently on another
        host - DPI mode and refresh rate explain most of it."""
        vr = self.screen.virtual_rect
        return {
            "version": __version__,
            "dpi_mode": self.screen.dpi_mode,
            "refresh_ms": round(self.screen.refresh_ms, 4),
            "screen_read_floor_ms": round(self.screen.refresh_ms, 4),
            "primary_size": self.screen.size,
            "virtual_rect": tuple(vr),
            "profile": self.profile.name,
            "precise_timer": bool(self.clock._period),
            "stats": dict(self.screen.stats),
        }

    # ----------------------------------------------------------------- mouse #
    def _as_point(self, target) -> Point:
        """Accept a Point, a (x, y) tuple, a Region (its centre), or a Window
        (its client centre). Removes the single most common beginner friction:
        having to unpack whatever the previous call returned."""
        if target is None:
            return self.mouse.position
        if isinstance(target, Window):
            return target.client_rect.center
        if isinstance(target, Region):
            return target.center
        if isinstance(target, Point):
            return target
        if isinstance(target, Sequence) and len(target) == 2:
            return Point(int(target[0]), int(target[1]))
        if isinstance(target, Sequence) and len(target) == 4:
            return Region(*target).center
        raise TypeError(f"cannot read a screen point from {target!r}")

    def move(self, target=None, y=None, **kw) -> Point:
        if y is not None:
            target = (target, y)
        p = self._as_point(target)
        return self.mouse.move(p.x, p.y, **kw)

    def click(self, target=None, y=None, *, button="left", count=1, **kw) -> Point:
        if y is not None:
            target = (target, y)
        p = None if target is None else self._as_point(target)
        return self.mouse.click(None if p is None else p.x,
                                None if p is None else p.y,
                                button=button, count=count, **kw)

    def double_click(self, target=None, y=None, **kw) -> Point:
        return self.click(target, y, count=2, **kw)

    def right_click(self, target=None, y=None, **kw) -> Point:
        return self.click(target, y, button="right", **kw)

    def drag(self, start, end, *, button="left", **kw) -> Point:
        a, b = self._as_point(start), self._as_point(end)
        return self.mouse.drag(a.x, a.y, b.x, b.y, button=button, **kw)

    def scroll(self, clicks: int, target=None, **kw):
        if target is not None:
            self.move(target)
        return self.mouse.scroll(clicks, **kw)

    @property
    def cursor(self) -> Point:
        return self.mouse.position

    # -------------------------------------------------------------- keyboard #
    def type(self, text: str, **kw):
        return self.keyboard.type(text, **kw)

    def press(self, *keys: str, **kw):
        """`press('ctrl', 's')` or `press('ctrl+s')` - both read naturally, so
        both work."""
        if len(keys) == 1 and ("+" in keys[0]) and len(keys[0]) > 1:
            keys = tuple(k for k in keys[0].split("+") if k)
        return self.keyboard.press(*keys, **kw)

    def tap(self, key: str, **kw):
        return self.keyboard.tap(key, **kw)

    def hold(self, *keys: str):
        """Context manager: `with auto.hold('shift'): auto.click(...)`."""
        kb = self.keyboard

        class _Hold:
            def __enter__(self_inner):
                for k in keys:
                    kb.down(k)
                return self_inner

            def __exit__(self_inner, *exc):
                for k in reversed(keys):
                    kb.up(k)
                return False

        return _Hold()

    def key_down(self, key: str):
        return self.keyboard.down(key)

    def key_up(self, key: str):
        return self.keyboard.up(key)

    def is_pressed(self, key: str) -> bool:
        return self.keyboard.is_down(key)

    # ----------------------------------------------------------- clipboard -- #
    @property
    def clipboard(self) -> str:
        return clipboard.get_text()

    @clipboard.setter
    def clipboard(self, text: str):
        clipboard.set_text(text)

    def copy(self) -> str:
        """Trigger Ctrl+C and read clipboard text."""
        self.press("ctrl", "c")
        time.sleep(0.05)
        return self.clipboard

    def paste(self, text: str | None = None):
        """Set clipboard text and trigger Ctrl+V, or just trigger Ctrl+V."""
        if text is not None:
            self.clipboard = text
        self.press("ctrl", "v")

    # ---------------------------------------------------------------- screen #
    def grab(self, region=None) -> Frame:
        """A fresh frame. Costs one compositor frame."""
        return self.screen.grab(region)

    def frame(self, region=None, **kw) -> Frame:
        """The current frame, reusing the last one if it is younger than one
        compositor frame. This is the one to use inside a loop."""
        return self.screen.frame(region, **kw)

    def pixel(self, target, y=None) -> int:
        if y is not None:
            target = (target, y)
        p = self._as_point(target)
        return self.frame().pixel(p.x, p.y)

    def locate(self, color, region=None, *, tolerance: int = 0,
               center: bool = True) -> Point | None:
        """Where is this colour? Returns the centre of mass by default, which
        is what 'click the red button' means - `find`'s first-scanline hit
        lands on an antialiased edge pixel instead."""
        f = self.frame(region)
        return f.centroid(color, tolerance) if center else f.find(color, tolerance)

    def locate_all(self, color, region=None, *, tolerance: int = 0,
                   limit: int | None = None) -> list[Point]:
        return self.frame(region).find_all(color, tolerance, limit=limit)

    def count_color(self, color, region=None, *, tolerance: int = 0) -> int:
        return self.frame(region).count(color, tolerance)

    def exists(self, color, region=None, *, tolerance: int = 0) -> bool:
        return self.frame(region).find(color, tolerance) is not None

    # ------------------------------------------------------------------ wait #
    def wait(self, seconds: float):
        """Sub-millisecond-accurate sleep. The default Windows timer rounds to
        15.6 ms; this does not."""
        self.clock.sleep(seconds)

    def wait_for(self, condition: Callable[[], bool] | Callable[[Frame], bool], *,
                 timeout: float = 10.0, interval: float | None = None,
                 what: str = "condition", required: bool = False):
        """Poll a condition. A zero-argument callable is polled directly; a
        one-argument callable receives each fresh frame, so screen conditions
        cost exactly one frame per poll rather than two."""
        import inspect
        try:
            takes_frame = len(inspect.signature(condition).parameters) >= 1
        except (TypeError, ValueError):
            takes_frame = False

        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if takes_frame:
                if condition(self.screen.grab()):
                    return True
            else:
                if condition():
                    return True
                self.clock.sleep(interval if interval is not None
                                 else self.screen.refresh_ms / 1000.0)
        if required:
            raise Timeout(what, timeout)
        return False

    def wait_for_color(self, color, region=None, *, tolerance: int = 0,
                       timeout: float = 10.0, required: bool = False) -> Point | None:
        p = self.screen.wait_for_color(color, region, tolerance=tolerance, timeout=timeout)
        if p is None and required:
            raise Timeout(f"colour {color!r} on screen", timeout)
        return p

    def wait_until_still(self, region=None, **kw) -> bool:
        """Wait for the screen to stop changing. The honest replacement for a
        guessed `sleep(2)` after opening a dialog."""
        return self.screen.wait_until_still(region, **kw)

    # ---------------------------------------------------------------- window #
    def find(self, title: str | None = None, **kw) -> Window | None:
        return find_window(title, **kw)

    def find_all(self, title: str | None = None, **kw) -> list[Window]:
        return find_windows(title, **kw)

    def wait_window(self, title: str | None = None, *, timeout: float = 10.0,
                    required: bool = False, **kw) -> Window | None:
        wd = wait_for_window(title, timeout=timeout, **kw)
        if wd is None and required:
            raise Timeout(f"window {title!r}", timeout)
        return wd

    @property
    def active(self) -> Window | None:
        return active_window()

    def window_at(self, target, y=None, **kw) -> Window | None:
        if y is not None:
            target = (target, y)
        p = self._as_point(target)
        return window_at(p.x, p.y, **kw)

    def windows(self, **kw) -> list[Window]:
        return all_windows(**kw)

    # ------------------------------------------------------------------ misc #
    def close(self):
        self.screen.close()
        self.clock.end_precision()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


auto = Auto()

__all__ = [
    "auto", "Auto", "Timeout", "InputBlocked",
    "Frame", "Point", "Region", "Screen", "Window",
    "Mouse", "Keyboard", "Clock", "Profile",
    "INSTANT", "FAST", "NATURAL", "CAREFUL",
    "find_window", "find_windows", "wait_for_window", "all_windows",
    "active_window", "window_at", "probe_window_dc", "KEYS", "__version__",
]
