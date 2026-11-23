"""BASIRA (بصيرة) - insight: seeing a thing and grasping what it is.

Desktop automation where you name what you want instead of guessing where it
is. AutoIt's simplicity, YOLOE's open-vocabulary eyes, Win32's latency floor.

    from basira import auto

    auto.click("cat")                      # finds a cat on screen, clicks it
    auto.click("submit button")            # words, not coordinates
    auto.see("cat").confidence             # 0.87
    auto.look(region).labels()             # what is in this box?

    panel = auto.find("Chrome").client_rect
    auto.click("play button", within=panel)  # scoped search

The classical distinction the name carries: `basar` is bare sight, `basira` is
sight that understands. The pixel layer below sees colours; this layer sees
a cat.

Everything degrades gracefully: with no torch installed, every pixel, input and
window API still works and only the word-based calls raise, with the install
hint attached.

Latency, honestly stated (measured, RTX 3060 Ti, 3440x1440 @144Hz):
    click / key / type .......... tens of microseconds per event
    any screen read ............. 6.94 ms, set by the compositor, not by us
    window find ................. 46 us for a full 264-window sweep
    open-vocabulary detection ... model-dependent; auto.vision.status() reports
                                  the real measured mean for this machine
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
from .vision import (Detection, DetectionSet, Vision, VisionUnavailable)
from .window import (Window, active_window, all_windows, find_window,
                     find_windows, wait_for_window, window_at)

__version__ = "0.1.0"


def _looks_like_colour(value) -> bool:
    """Is this a colour spec rather than a word?

    The ambiguity is real and worth resolving explicitly: 'face' is a valid hex
    triple AND an object you might want to click. Resolution rule - ints and
    3-tuples are always colours; a string is a colour only when prefixed with
    '#' or when it is 6 hex digits. A bare 3-letter string stays a word, so
    'cat', 'fab' and 'ace' name things, while '#fab' names a colour.
    """
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, tuple):
        return len(value) == 3
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("#"):
            return True
        return (len(s) == 6
                and all(c in "0123456789abcdefABCDEF" for c in s))
    return False


class Timeout(RuntimeError):
    """A wait expired. Carries what was being waited for, because 'Timeout'
    alone in a traceback tells a user nothing about which step failed."""

    def __init__(self, what: str, seconds: float):
        super().__init__(f"timed out after {seconds:.2f}s waiting for {what}")
        self.what = what
        self.seconds = seconds


class NotFound(LookupError):
    """A named thing was not on screen.

    Raised rather than returning None when a word is used as a click target,
    because `click("cat")` on an absent cat must not silently click wherever
    the pointer happened to be. The non-raising form is `see()`, which returns
    None and lets the script decide.
    """

    def __init__(self, what: str, within=None):
        where = "" if within is None else f" within {tuple(within)}"
        super().__init__(
            f"could not see {what!r} on screen{where}. "
            f"Try auto.look({'' if within is None else 'region'}).labels() to "
            f"see what is actually there, a lower confidence "
            f"(auto.vision.confidence), or a different wording."
        )
        self.what = what
        self.within = within


class Auto:
    """The façade. One instance is created as `basira.auto`; constructing more
    is fine and they share no mutable global state beyond the OS itself."""

    def __init__(self, *, humanize=None, precise_timing: bool = True,
                 vision_scale: str = "s", vision_device: str | None = None,
                 confidence: float = 0.25):
        self.screen = Screen()
        self.profile = _h.resolve(humanize)
        self.mouse = Mouse(self.profile)
        self.keyboard = Keyboard(self.profile)
        self.clock = _clock
        self.vision = Vision(scale=vision_scale, device=vision_device,
                             confidence=confidence)
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
            "vision": self.vision.status(),
        }

    # ----------------------------------------------------------------- mouse #
    def _as_point(self, target, *, within=None) -> Point:
        """Resolve anything a user might point at into a screen coordinate.

        Accepts a Point, an (x, y) tuple, a Region (its centre), a Window (its
        client centre), a Detection, OR A WORD. The word case is the whole
        reason this library exists: `auto.click("cat")` resolves the noun
        through open-vocabulary detection and lands on the cat's centre.

        Removing this ladder is what makes the beginner and expert paths the
        same path - a script that starts as `click("button")` can later pass an
        exact Region without rewriting anything.
        """
        if target is None:
            return self.mouse.position
        if isinstance(target, str):
            d = self.see(target, within=within)
            if d is None:
                raise NotFound(target, within)
            return d.center
        if isinstance(target, Detection):
            return target.center
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

    def move(self, target=None, y=None, *, within=None, **kw) -> Point:
        if y is not None:
            target = (target, y)
        p = self._as_point(target, within=within)
        return self.mouse.move(p.x, p.y, **kw)

    def click(self, target=None, y=None, *, button="left", count=1,
              within=None, **kw) -> Point:
        """Click a point, a region, a window - or a named thing.

            auto.click(840, 390)
            auto.click("cat")
            auto.click("play button", within=player_panel)
        """
        if y is not None:
            target = (target, y)
        p = None if target is None else self._as_point(target, within=within)
        return self.mouse.click(None if p is None else p.x,
                                None if p is None else p.y,
                                button=button, count=count, **kw)

    def double_click(self, target=None, y=None, *, within=None, **kw) -> Point:
        return self.click(target, y, count=2, within=within, **kw)

    def right_click(self, target=None, y=None, *, within=None, **kw) -> Point:
        return self.click(target, y, button="right", within=within, **kw)

    def drag(self, start, end, *, button="left", within=None, **kw) -> Point:
        """Drag between any two targets, words included:

            auto.drag("red card", "discard pile")
        """
        a = self._as_point(start, within=within)
        b = self._as_point(end, within=within)
        return self.mouse.drag(a.x, a.y, b.x, b.y, button=button, **kw)

    def scroll(self, clicks: int, target=None, *, within=None, **kw):
        if target is not None:
            self.move(target, within=within)
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

    # ---------------------------------------------------------------- vision #
    def look(self, within=None, *, prompts: Iterable[str] | None = None,
             fresh: bool = False, confidence: float | None = None) -> DetectionSet:
        """What is on screen (or in a region)? Returns everything found.

            auto.look().labels()                  # explore: what's here?
            auto.look(panel, prompts=["cat","dog"])

        With `prompts=None` the prompt-free model answers from its built-in
        vocabulary, which is how you discover the right words for a screen you
        have never automated before.
        """
        f = self.grab() if fresh else self.frame()
        return self.vision.detect(f, prompts, confidence=confidence,
                                  region=None if within is None else Region(*within))

    def see(self, what: str, within=None, *, confidence: float | None = None,
            fresh: bool = False, largest: bool = False) -> Detection | None:
        """Find one named thing, or None.

            cat = auto.see("cat")
            if cat: auto.click(cat)

        Returns the highest-confidence match by default; `largest=True` picks
        by area instead, which disambiguates a window from its own thumbnail.
        """
        got = self.look(within, prompts=[what], fresh=fresh, confidence=confidence)
        return got.largest(what) if largest else got.best(what)

    def see_all(self, what: str, within=None, *, confidence: float | None = None,
                fresh: bool = False) -> list[Detection]:
        """Every instance of a named thing, highest confidence first."""
        return self.look(within, prompts=[what], fresh=fresh,
                         confidence=confidence).all(what)

    def sees(self, what: str, within=None, **kw) -> bool:
        """Is this thing on screen right now?"""
        return self.see(what, within, **kw) is not None

    def region(self, what: str, within=None, **kw) -> Region | None:
        """The bounding box of a named thing - the handle for scoping a later
        search: `auto.click("button", within=auto.region("dialog"))`."""
        d = self.see(what, within, **kw)
        return None if d is None else d.box

    def wait_to_see(self, what: str, within=None, *, timeout: float = 15.0,
                    confidence: float | None = None,
                    required: bool = False) -> Detection | None:
        """Poll until a named thing appears.

        Each poll costs one frame plus one inference, so this is the expensive
        wait in the library - and the honest replacement for a guessed sleep
        after an action whose result takes an unknown time to render.
        """
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            d = self.see(what, within, fresh=True, confidence=confidence)
            if d is not None:
                return d
        if required:
            raise Timeout(f"{what!r} to appear on screen", timeout)
        return None

    def click_all(self, what: str, within=None, *, confidence: float | None = None,
                  button: str = "left", limit: int | None = None) -> int:
        """Click every instance of a named thing. Returns how many were clicked.

        Detections come from ONE frame, so the coordinates are mutually
        consistent - unlike a loop that re-detects after each click and can
        chase a shifting layout.
        """
        found = self.see_all(what, within, confidence=confidence, fresh=True)
        if limit is not None:
            found = found[:limit]
        for d in found:
            self.click(d, button=button)
        return len(found)

    def click_when_seen(self, what: str, within=None, *, timeout: float = 15.0,
                        confidence: float | None = None,
                        required: bool = True, **kw) -> Detection | None:
        """Wait for a thing, then click it. The single most common bot step.

            auto.click_when_seen("accept button")

        Clicks the detection from the frame that found it, so the click cannot
        land on a stale position - and raises by default, because a bot that
        silently skips its own step is worse than one that stops.
        """
        d = self.wait_to_see(what, within, timeout=timeout,
                             confidence=confidence, required=required)
        if d is None:
            return None
        self.click(d, **kw)
        return d

    def wait_to_vanish(self, what: str, within=None, *, timeout: float = 15.0,
                       confidence: float | None = None,
                       required: bool = False) -> bool:
        """Wait until a named thing is gone - a spinner, a loading overlay, a
        dialog you just dismissed. The counterpart to `wait_to_see`, and the
        honest replacement for sleeping until you guess it's done."""
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if self.see(what, within, fresh=True, confidence=confidence) is None:
                return True
        if required:
            raise Timeout(f"{what!r} to disappear from screen", timeout)
        return False

    def vocabulary(self) -> list[str]:
        """Words the prompt-free model already knows. Print this instead of
        guessing nouns and wondering why nothing matched."""
        return self.vision.vocabulary()

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

    def exists(self, what, region=None, *, tolerance: int = 0) -> bool:
        """Is this on screen? Accepts a colour ('#ff0000', 0xff0000, (255,0,0))
        or a word ('cat'). Colours go to the pixel layer, words to vision."""
        if _looks_like_colour(what):
            return self.frame(region).find(what, tolerance) is not None
        return self.sees(str(what), region)

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
    "auto", "Auto", "Timeout", "NotFound", "InputBlocked", "VisionUnavailable",
    "Frame", "Point", "Region", "Screen", "Window",
    "Detection", "DetectionSet", "Vision",
    "Mouse", "Keyboard", "Clock", "Profile", "clipboard",
    "INSTANT", "FAST", "NATURAL", "CAREFUL",
    "find_window", "find_windows", "wait_for_window", "all_windows",
    "active_window", "window_at", "probe_window_dc", "KEYS", "__version__",
]
