"""Screen acquisition and querying.

The measurement that shapes this file: on this host a screen-DC BitBlt costs
6.94 ms regardless of whether it copies 1 pixel or 5 megapixels, and that
6.94 ms equals both DwmFlush() and the 144 Hz frame interval. Screen reading is
frame-quantized by the compositor, not bandwidth-bound.

Consequence: a per-query API (AutoIt's `PixelGetColor`, pyautogui's `pixel()`)
pays one full compositor frame per question. Asking 100 questions about one
moment of the screen costs 694 ms and - worse - the answers come from 100
different moments, so they cannot be reasoned about together.

So the unit here is the Frame: acquire once, query in RAM. The cost of N
queries is one frame plus N memory scans.

A window DC blit is ~385x cheaper (0.018 ms cached) but returns black for
GPU-composited windows - measured black on 7 of 8 live windows here (Chrome,
Electron, UWP), content on 1 (legacy GDI Explorer). It is therefore a
capability to be *probed and validated*, never assumed.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import time
from typing import Iterator, NamedTuple

import numpy as np

from . import _win32 as _w

_REFRESH_MS = _w.refresh_interval_ms()


class Point(NamedTuple):
    x: int
    y: int


class Region(NamedTuple):
    x: int
    y: int
    width: int
    height: int

    @property
    def center(self) -> Point:
        return Point(self.x + self.width // 2, self.y + self.height // 2)


def _as_rgb(color) -> tuple[int, int, int]:
    """Accept '#ff8800', 0xff8800, (255,136,0) - all the shapes a beginner tries."""
    if isinstance(color, tuple):
        if len(color) != 3:
            raise ValueError(f"colour tuple needs 3 channels, got {len(color)}")
        return (int(color[0]), int(color[1]), int(color[2]))
    if isinstance(color, str):
        s = color.lstrip("#").strip()
        if len(s) == 3:
            s = "".join(c * 2 for c in s)
        if len(s) != 6:
            raise ValueError(f"hex colour must be 3 or 6 digits, got {color!r}")
        v = int(s, 16)
    else:
        v = int(color)
    return ((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF)


# --------------------------------------------------------------------------- #
# Frame
# --------------------------------------------------------------------------- #
class Frame:
    """An immutable moment of the screen. Every query below hits RAM only.

    Pixels are stored as the BGRA block GDI hands back, with no conversion on
    acquisition: converting 5 MPx to RGB costs more than most scripts will ever
    spend querying, and the packed-uint32 view makes exact colour search a
    single vectorised compare over the raw buffer anyway.
    """

    __slots__ = ("_bgra", "_u32", "origin", "width", "height", "timestamp", "seq", "source")

    def __init__(self, bgra: np.ndarray, origin: Point, timestamp: float, seq: int, source: str):
        self._bgra = bgra
        self._u32 = bgra.view(np.uint32).reshape(bgra.shape[0], bgra.shape[1])
        self.origin = origin
        self.height, self.width = bgra.shape[0], bgra.shape[1]
        self.timestamp = timestamp
        self.seq = seq
        self.source = source

    # -- introspection ----------------------------------------------------- #
    @property
    def age_ms(self) -> float:
        return (time.perf_counter() - self.timestamp) * 1000.0

    @property
    def rgb(self) -> np.ndarray:
        """H x W x 3 uint8 RGB view. Materialised only when asked for."""
        return self._bgra[:, :, 2::-1]

    @property
    def bgra(self) -> np.ndarray:
        return self._bgra

    def __repr__(self) -> str:
        return (f"<Frame #{self.seq} {self.width}x{self.height} @{tuple(self.origin)} "
                f"via {self.source} age={self.age_ms:.1f}ms>")

    # -- point queries ----------------------------------------------------- #
    def pixel(self, x: int, y: int, *, absolute: bool = True) -> int:
        """0xRRGGBB at a screen (or frame-local) coordinate."""
        if absolute:
            x -= self.origin.x
            y -= self.origin.y
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise IndexError(f"({x},{y}) outside frame {self.width}x{self.height}")
        b, g, r = self._bgra[y, x, 0], self._bgra[y, x, 1], self._bgra[y, x, 2]
        return (int(r) << 16) | (int(g) << 8) | int(b)

    def matches(self, x: int, y: int, color, tolerance: int = 0, *, absolute: bool = True) -> bool:
        r, g, b = _as_rgb(color)
        got = self.pixel(x, y, absolute=absolute)
        if tolerance == 0:
            return got == ((r << 16) | (g << 8) | b)
        return (abs(((got >> 16) & 0xFF) - r) <= tolerance
                and abs(((got >> 8) & 0xFF) - g) <= tolerance
                and abs((got & 0xFF) - b) <= tolerance)

    # -- search ------------------------------------------------------------ #
    def _mask(self, color, tolerance: int) -> np.ndarray:
        r, g, b = _as_rgb(color)
        if tolerance == 0:
            # Alpha is undefined coming out of BitBlt, so mask it off and run a
            # single uint32 compare over the buffer instead of three uint8 ones.
            packed = (r << 16) | (g << 8) | b
            return (self._u32 & np.uint32(0x00FFFFFF)) == np.uint32(packed)
        px = self._bgra[:, :, :3].astype(np.int16)
        return ((np.abs(px[:, :, 2] - r) <= tolerance)
                & (np.abs(px[:, :, 1] - g) <= tolerance)
                & (np.abs(px[:, :, 0] - b) <= tolerance))

    def find(self, color, tolerance: int = 0, *, absolute: bool = True) -> Point | None:
        """First matching pixel in scan order, or None."""
        mask = self._mask(color, tolerance)
        flat = mask.reshape(-1)
        idx = int(np.argmax(flat))
        if not flat[idx]:
            return None
        y, x = divmod(idx, self.width)
        return Point(x + self.origin.x, y + self.origin.y) if absolute else Point(x, y)

    def find_all(self, color, tolerance: int = 0, *, absolute: bool = True,
                 limit: int | None = None) -> list[Point]:
        mask = self._mask(color, tolerance)
        ys, xs = np.nonzero(mask)
        if limit is not None:
            ys, xs = ys[:limit], xs[:limit]
        ox, oy = (self.origin if absolute else Point(0, 0))
        return [Point(int(x) + ox, int(y) + oy) for x, y in zip(xs, ys)]

    def count(self, color, tolerance: int = 0) -> int:
        return int(self._mask(color, tolerance).sum())

    def centroid(self, color, tolerance: int = 0, *, absolute: bool = True) -> Point | None:
        """Centre of mass of a colour - what you actually want for 'click the
        red button', since `find` returns its top-left-most antialiased edge."""
        mask = self._mask(color, tolerance)
        ys, xs = np.nonzero(mask)
        if len(xs) == 0:
            return None
        ox, oy = (self.origin if absolute else Point(0, 0))
        return Point(int(xs.mean()) + ox, int(ys.mean()) + oy)

    def bounds(self, color, tolerance: int = 0, *, absolute: bool = True) -> Region | None:
        mask = self._mask(color, tolerance)
        ys, xs = np.nonzero(mask)
        if len(xs) == 0:
            return None
        ox, oy = (self.origin if absolute else Point(0, 0))
        x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
        return Region(x0 + ox, y0 + oy, x1 - x0 + 1, y1 - y0 + 1)

    def crop(self, region: Region) -> "Frame":
        """A sub-frame. Shares the parent buffer; no copy."""
        lx, ly = region.x - self.origin.x, region.y - self.origin.y
        sub = self._bgra[ly:ly + region.height, lx:lx + region.width]
        return Frame(sub, Point(region.x, region.y), self.timestamp, self.seq, self.source + "/crop")

    def changed_from(self, other: "Frame | None", tolerance: int = 0) -> bool:
        """Did anything move between two frames? The cheap building block for
        'wait until the screen settles'."""
        if other is None or other._bgra.shape != self._bgra.shape:
            return True
        if tolerance == 0:
            return not np.array_equal(self._u32 & 0x00FFFFFF, other._u32 & 0x00FFFFFF)
        diff = np.abs(self._bgra[:, :, :3].astype(np.int16) - other._bgra[:, :, :3].astype(np.int16))
        return bool((diff > tolerance).any())


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #
class _GdiGrabber:
    """A reusable DC + DIB section. Creating these per call costs more than the
    blit itself for small regions, so they are cached per (size, source)."""

    __slots__ = ("hdc_src", "hdc_mem", "hbm", "bmi", "buf", "width", "height", "_owner_hwnd")

    def __init__(self, hdc_src, width: int, height: int, owner_hwnd=None):
        self.hdc_src = hdc_src
        self.width, self.height = width, height
        self._owner_hwnd = owner_hwnd
        screen_dc = _w.user32.GetDC(0)
        self.hdc_mem = _w.gdi32.CreateCompatibleDC(screen_dc)
        self.hbm = _w.gdi32.CreateCompatibleBitmap(screen_dc, width, height)
        _w.gdi32.SelectObject(self.hdc_mem, self.hbm)
        _w.user32.ReleaseDC(0, screen_dc)

        self.bmi = _w.BITMAPINFO()
        self.bmi.bmiHeader.biSize = ctypes.sizeof(_w.BITMAPINFOHEADER)
        self.bmi.bmiHeader.biWidth = width
        self.bmi.bmiHeader.biHeight = -height  # top-down
        self.bmi.bmiHeader.biPlanes = 1
        self.bmi.bmiHeader.biBitCount = 32
        self.bmi.bmiHeader.biCompression = 0
        self.buf = ctypes.create_string_buffer(width * height * 4)

    def grab(self, sx: int, sy: int) -> np.ndarray:
        _w.gdi32.BitBlt(self.hdc_mem, 0, 0, self.width, self.height,
                        self.hdc_src, sx, sy, _w.SRCCOPY)
        _w.gdi32.GetDIBits(self.hdc_mem, self.hbm, 0, self.height,
                           self.buf, ctypes.byref(self.bmi), _w.DIB_RGB_COLORS)
        return np.frombuffer(self.buf, np.uint8).reshape(self.height, self.width, 4)

    def close(self):
        if self.hbm:
            _w.gdi32.DeleteObject(self.hbm)
            self.hbm = None
        if self.hdc_mem:
            _w.gdi32.DeleteDC(self.hdc_mem)
            self.hdc_mem = None
        if self._owner_hwnd is not None and self.hdc_src:
            _w.user32.ReleaseDC(self._owner_hwnd, self.hdc_src)
            self.hdc_src = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def probe_window_dc(hwnd) -> dict:
    """Can this window be read directly, bypassing the compositor frame lock?

    Returns a verdict dict rather than a bool because the interesting part is
    *why*: a window DC that blits in 18 us and returns a uniform black field is
    the single most seductive wrong answer in Windows automation.
    """
    rect = w.RECT()
    if not _w.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return {"usable": False, "reason": "no window rect"}
    ww, hh = rect.right - rect.left, rect.bottom - rect.top
    if ww < 8 or hh < 8:
        return {"usable": False, "reason": "window too small"}

    probe_w, probe_h = min(128, ww), min(128, hh)
    hdc_win = _w.user32.GetWindowDC(hwnd)
    if not hdc_win:
        return {"usable": False, "reason": "GetWindowDC failed"}
    try:
        g = _GdiGrabber(hdc_win, probe_w, probe_h)
        try:
            arr = g.grab(0, 0)
            distinct = len(np.unique(arr.view(np.uint32)))
            t0 = time.perf_counter()
            for _ in range(5):
                g.grab(0, 0)
            per_ms = (time.perf_counter() - t0) * 1000.0 / 5
            if distinct <= 1:
                return {"usable": False, "reason": "returns a flat field (GPU-composited)",
                        "distinct_colors": distinct, "ms": per_ms}
            if per_ms > _REFRESH_MS * 0.8:
                return {"usable": False, "reason": "no faster than a screen read",
                        "distinct_colors": distinct, "ms": per_ms}
            return {"usable": True, "reason": "live pixels, sub-frame latency",
                    "distinct_colors": distinct, "ms": per_ms}
        finally:
            g.close()
    finally:
        _w.user32.ReleaseDC(hwnd, hdc_win)


# --------------------------------------------------------------------------- #
# Screen
# --------------------------------------------------------------------------- #
class Screen:
    """Frame source.

    `grab()` always costs a fresh compositor frame. `frame()` returns the last
    one if it is younger than `max_age_ms`, which is what makes a script that
    asks twenty questions about one moment cost one frame instead of twenty.
    """

    def __init__(self, *, default_max_age_ms: float | None = None):
        self.dpi_mode = _w.enable_dpi_awareness()
        self.refresh_ms = _REFRESH_MS
        # Default staleness tolerance is just under one frame: anything younger
        # genuinely cannot differ, because the compositor has not published a
        # new frame yet. So this costs nothing in correctness and removes the
        # duplicate-read penalty entirely.
        self.default_max_age_ms = (
            default_max_age_ms if default_max_age_ms is not None else _REFRESH_MS * 0.9
        )
        self._grabbers: dict[tuple, _GdiGrabber] = {}
        self._screen_dc = _w.user32.GetDC(0)
        self._last: Frame | None = None
        self._seq = 0
        self.stats = {"grabs": 0, "cache_hits": 0, "queries": 0}

    # -- geometry ---------------------------------------------------------- #
    @property
    def size(self) -> tuple[int, int]:
        return (_w.user32.GetSystemMetrics(_w.SM_CXSCREEN),
                _w.user32.GetSystemMetrics(_w.SM_CYSCREEN))

    @property
    def virtual_rect(self) -> Region:
        return Region(*_w.virtual_screen_rect())

    # -- acquisition ------------------------------------------------------- #
    def _grabber(self, key: tuple, hdc_src, width: int, height: int, owner=None) -> _GdiGrabber:
        g = self._grabbers.get(key)
        if g is None or g.width != width or g.height != height:
            if g is not None:
                g.close()
            g = _GdiGrabber(hdc_src, width, height, owner)
            self._grabbers[key] = g
        return g

    def grab(self, region: Region | tuple | None = None) -> Frame:
        """Acquire a fresh frame. Costs one compositor frame (~6.94 ms here)."""
        if region is None:
            x, y, width, height = self.virtual_rect
        else:
            x, y, width, height = Region(*region)
        g = self._grabber(("screen", width, height), self._screen_dc, width, height)
        arr = g.grab(x, y).copy()  # copy: the grabber buffer is reused next call
        self._seq += 1
        self.stats["grabs"] += 1
        self._last = Frame(arr, Point(x, y), time.perf_counter(), self._seq, "screen-dc")
        return self._last

    def frame(self, region: Region | tuple | None = None, *,
              max_age_ms: float | None = None) -> Frame:
        """The frame a script should normally use: reuses the previous one while
        it is younger than one compositor frame."""
        limit = self.default_max_age_ms if max_age_ms is None else max_age_ms
        last = self._last
        if last is not None and last.age_ms <= limit:
            want = None if region is None else Region(*region)
            if want is None:
                if last.source == "screen-dc" and (last.width, last.height) == self.virtual_rect[2:]:
                    self.stats["cache_hits"] += 1
                    return last
            elif (last.origin.x <= want.x and last.origin.y <= want.y
                  and last.origin.x + last.width >= want.x + want.width
                  and last.origin.y + last.height >= want.y + want.height):
                self.stats["cache_hits"] += 1
                return last if (want.width, want.height) == (last.width, last.height) else last.crop(want)
        return self.grab(region)

    def grab_window(self, hwnd, *, client_only: bool = False) -> Frame:
        """Frame of one window via PrintWindow - works on occluded and
        partially off-screen windows, where a screen read would capture
        whatever is covering them. Measured 6.96 ms here: same frame lock, but
        correct content for the window you asked about."""
        rect = w.RECT()
        _w.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        ww, hh = rect.right - rect.left, rect.bottom - rect.top
        g = self._grabber(("pw", int(hwnd), ww, hh), None, ww, hh)
        flags = _w.PW_RENDERFULLCONTENT | (_w.PW_CLIENTONLY if client_only else 0)
        _w.user32.PrintWindow(hwnd, g.hdc_mem, flags)
        _w.gdi32.GetDIBits(g.hdc_mem, g.hbm, 0, hh, g.buf, ctypes.byref(g.bmi), _w.DIB_RGB_COLORS)
        arr = np.frombuffer(g.buf, np.uint8).reshape(hh, ww, 4).copy()
        self._seq += 1
        self.stats["grabs"] += 1
        f = Frame(arr, Point(rect.left, rect.top), time.perf_counter(), self._seq, "printwindow")
        self._last = f
        return f

    # -- polling ----------------------------------------------------------- #
    def frames(self, region=None, *, timeout: float | None = None) -> Iterator[Frame]:
        """Successive distinct frames. Since acquisition already blocks for one
        compositor frame, this is a vsync-paced loop with no sleep needed - and
        no busy-wait either."""
        deadline = None if timeout is None else time.perf_counter() + timeout
        while deadline is None or time.perf_counter() < deadline:
            yield self.grab(region)

    def wait_for(self, predicate, region=None, *, timeout: float = 10.0) -> Frame | None:
        """Poll until `predicate(frame)` is truthy. One frame per poll, exactly."""
        for f in self.frames(region, timeout=timeout):
            if predicate(f):
                return f
        return None

    def wait_for_color(self, color, region=None, *, tolerance: int = 0,
                       timeout: float = 10.0) -> Point | None:
        box: list[Point | None] = [None]

        def seen(f: Frame) -> bool:
            box[0] = f.find(color, tolerance)
            return box[0] is not None

        return box[0] if self.wait_for(seen, region, timeout=timeout) else None

    def wait_until_still(self, region=None, *, settle_frames: int = 2,
                         tolerance: int = 2, timeout: float = 10.0) -> bool:
        """Wait for the screen to stop changing - the honest replacement for the
        fixed `Sleep(2000)` that litters AutoIt scripts."""
        prev: Frame | None = None
        stable = 0
        for f in self.frames(region, timeout=timeout):
            if prev is not None and not f.changed_from(prev, tolerance):
                stable += 1
                if stable >= settle_frames:
                    return True
            else:
                stable = 0
            prev = f
        return False

    def close(self):
        for g in self._grabbers.values():
            g.close()
        self._grabbers.clear()
        if self._screen_dc:
            _w.user32.ReleaseDC(0, self._screen_dc)
            self._screen_dc = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
