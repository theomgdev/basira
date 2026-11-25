"""Click-through screen overlay for seeing what the automation sees.

The whole point of an open-vocabulary automation library is that you write
`auto.click("cat")` and it works. The cost of that convenience is that when it
*doesn't* work you have no idea why: the model's boxes, the OCR hits and the
region you are actually watching are all invisible. Every diagnosis then turns
into asking the user for screenshots and replaying them offline.

This module draws them on the screen instead, in a window that cannot be
clicked, cannot take focus, and cannot appear in Alt-Tab -- so it is safe to
leave switched on while the automation it is debugging keeps running.

    auto.show(auto.look())                 # every detection, 2 s
    auto.show(auto.read(), seconds=5)      # every OCR hit
    auto.show(region, color="lime")        # the area being watched
    auto.hide()                            # clear it

Mechanisms worth knowing before changing this file:

* `WS_EX_TRANSPARENT` is load-bearing. Without it the overlay is a giant
  invisible wall that eats every click in its rectangle -- an automation
  library's debug view would break the automation it exists to show.
* `WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW` keep it from stealing focus. Taking
  focus minimises fullscreen exclusive games, i.e. exactly the case you most
  want to debug.
* `UpdateLayeredWindow(ULW_ALPHA)` expects **premultiplied** BGRA: each colour
  channel already scaled by alpha/255. Passing straight alpha gives bright
  haloes around everything. Opaque pixels (alpha 255) are unaffected, which is
  why text is drawn on a solid label chip rather than straight onto the glass.
* GDI text drawing does not touch the alpha channel, so `TextOutW` onto a
  transparent DIB writes invisible glyphs. The label chip is filled opaque
  first, which makes the alpha correct for free.
* A layered window needs no message pump and no `WM_PAINT`: the compositor
  keeps the bitmap handed to `UpdateLayeredWindow`. Content persists until the
  next update, so drawing is a single call with no event loop to service.
"""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes as w

import numpy as np

from ._win32 import (AC_SRC_ALPHA, AC_SRC_OVER, ANTIALIASED_QUALITY,
                     BITMAPINFO, BLENDFUNCTION, DEFAULT_CHARSET, DEFAULT_PITCH,
                     DIB_RGB_COLORS, FW_BOLD, SW_HIDE, SW_SHOWNOACTIVATE,
                     TRANSPARENT_BKMODE, ULW_ALPHA, WNDCLASSW, WNDPROC,
                     WS_EX_LAYERED, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW,
                     WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_POPUP, gdi32,
                     module_handle, user32, virtual_screen_rect)

# Named colours, as RGB. Chosen to stay readable over both bright UI and dark
# game scenes; "lime" and "cyan" survive almost any background.
COLORS = {
    "lime": (60, 255, 60),
    "cyan": (60, 230, 255),
    "magenta": (255, 70, 220),
    "yellow": (255, 220, 50),
    "orange": (255, 150, 40),
    "red": (255, 70, 70),
    "white": (245, 245, 245),
    "blue": (90, 160, 255),
}
DEFAULT_COLOR = "lime"

# A palette cycled per label so different classes are visually separable
# without the caller having to assign colours by hand.
PALETTE = ("lime", "cyan", "magenta", "yellow", "orange", "blue")


def _rgb(color) -> tuple[int, int, int]:
    """Accept a name, an #rrggbb string, or an (r, g, b) tuple."""
    if isinstance(color, str):
        key = color.strip().lower()
        if key in COLORS:
            return COLORS[key]
        if key.startswith("#") and len(key) == 7:
            return (int(key[1:3], 16), int(key[3:5], 16), int(key[5:7], 16))
        raise ValueError(
            f"unknown colour {color!r}; use a name from "
            f"{sorted(COLORS)}, an #rrggbb string, or an (r, g, b) tuple")
    r, g, b = color
    return (int(r), int(g), int(b))


class Overlay:
    """A click-through, per-pixel-alpha window spanning the virtual desktop.

    Created lazily on first draw and reused afterwards, because registering a
    window class and allocating a full-desktop DIB is far more expensive than
    the drawing itself.
    """

    _CLASS_NAME = "BasiraOverlay"
    _class_registered = False
    # The WNDPROC trampoline must outlive the window: ctypes callbacks that go
    # out of scope are garbage-collected and the next message crashes Windows.
    _wndproc_ref = None

    def __init__(self):
        self._hwnd = None
        self._mem_dc = None
        self._bitmap = None
        self._old_bitmap = None
        self._pixels: np.ndarray | None = None
        self._origin = (0, 0)
        self._size = (0, 0)
        self._hide_timer: threading.Timer | None = None
        self._font_cache: dict[int, int] = {}
        self._visible = False

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def _register_class(cls):
        if cls._class_registered:
            return

        def _proc(hwnd, msg, wparam, lparam):
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        cls._wndproc_ref = WNDPROC(_proc)
        wc = WNDCLASSW()
        wc.lpfnWndProc = cls._wndproc_ref
        wc.hInstance = module_handle()
        wc.lpszClassName = cls._CLASS_NAME
        if not user32.RegisterClassW(ctypes.byref(wc)):
            err = ctypes.get_last_error()
            # 1410 = class already exists, which is fine on a module reload.
            if err not in (0, 1410):
                raise OSError(f"RegisterClassW failed (error {err})")
        cls._class_registered = True

    def _ensure_window(self):
        if self._hwnd:
            return

        self._register_class()
        vx, vy, vw, vh = virtual_screen_rect()
        self._origin = (vx, vy)
        self._size = (vw, vh)

        self._hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST
            | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
            self._CLASS_NAME, "Basira Overlay", WS_POPUP,
            vx, vy, vw, vh,
            None, None, module_handle(), None)
        if not self._hwnd:
            raise OSError(f"CreateWindowExW failed "
                          f"(error {ctypes.get_last_error()})")

        # Top-down DIB (negative height) so row 0 is the top row and the numpy
        # view matches screen orientation without flipping on every draw.
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(bmi.bmiHeader)
        bmi.bmiHeader.biWidth = vw
        bmi.bmiHeader.biHeight = -vh
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0  # BI_RGB

        screen_dc = user32.GetDC(None)
        try:
            bits = ctypes.c_void_p()
            self._bitmap = gdi32.CreateDIBSection(
                screen_dc, ctypes.byref(bmi), DIB_RGB_COLORS,
                ctypes.byref(bits), None, 0)
            if not self._bitmap:
                raise OSError("CreateDIBSection failed")
            self._mem_dc = gdi32.CreateCompatibleDC(screen_dc)
            self._old_bitmap = gdi32.SelectObject(self._mem_dc, self._bitmap)
        finally:
            user32.ReleaseDC(None, screen_dc)

        # Writing through this view writes straight into the bitmap the
        # compositor reads, so there is no copy between draw and present.
        buf = (ctypes.c_uint8 * (vw * vh * 4)).from_address(bits.value)
        self._pixels = np.frombuffer(buf, dtype=np.uint8).reshape(vh, vw, 4)

        user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)

    def close(self):
        """Destroy the window and release every GDI object."""
        self._cancel_timer()
        if self._mem_dc:
            if self._old_bitmap:
                gdi32.SelectObject(self._mem_dc, self._old_bitmap)
            gdi32.DeleteDC(self._mem_dc)
        if self._bitmap:
            gdi32.DeleteObject(self._bitmap)
        for font in self._font_cache.values():
            gdi32.DeleteObject(font)
        self._font_cache.clear()
        if self._hwnd:
            user32.DestroyWindow(self._hwnd)
        self._hwnd = self._mem_dc = self._bitmap = self._old_bitmap = None
        self._pixels = None
        self._visible = False

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # -- drawing primitives ------------------------------------------------

    def clear(self):
        """Erase everything drawn so far (does not hide the window)."""
        self._ensure_window()
        self._pixels[:] = 0

    def _blend(self, y0: int, y1: int, x0: int, x1: int,
               rgb: tuple[int, int, int], alpha: int):
        """Write a premultiplied BGRA block, clipped to the desktop."""
        h, wd = self._size[1], self._size[0]
        y0, y1 = max(0, y0), min(h, y1)
        x0, x1 = max(0, x0), min(wd, x1)
        if y0 >= y1 or x0 >= x1:
            return
        r, g, b = rgb
        a = max(0, min(255, int(alpha)))
        # Premultiply: UpdateLayeredWindow(ULW_ALPHA) multiplies nothing itself,
        # so unpremultiplied colour shows up as a bright halo.
        scale = a / 255.0
        block = self._pixels[y0:y1, x0:x1]
        block[..., 0] = int(b * scale)
        block[..., 1] = int(g * scale)
        block[..., 2] = int(r * scale)
        block[..., 3] = a

    def rect(self, region, *, color=DEFAULT_COLOR, width: int = 2,
             alpha: int = 255, fill_alpha: int = 0):
        """Outline a region, optionally tinting its interior."""
        self._ensure_window()
        rgb = _rgb(color)
        ox, oy = self._origin
        x0, y0 = int(region.x) - ox, int(region.y) - oy
        x1, y1 = x0 + int(region.width), y0 + int(region.height)

        if fill_alpha > 0:
            self._blend(y0, y1, x0, x1, rgb, fill_alpha)
        t = max(1, int(width))
        self._blend(y0, y0 + t, x0, x1, rgb, alpha)            # top
        self._blend(y1 - t, y1, x0, x1, rgb, alpha)            # bottom
        self._blend(y0, y1, x0, x0 + t, rgb, alpha)            # left
        self._blend(y0, y1, x1 - t, x1, rgb, alpha)            # right

    def crosshair(self, point, *, color="red", size: int = 14,
                  width: int = 2, alpha: int = 255):
        """Mark a point -- an aim point, a click target, a centroid."""
        self._ensure_window()
        rgb = _rgb(color)
        ox, oy = self._origin
        x, y = int(point.x) - ox, int(point.y) - oy
        t = max(1, int(width))
        self._blend(y - t // 2, y - t // 2 + t, x - size, x + size, rgb, alpha)
        self._blend(y - size, y + size, x - t // 2, x - t // 2 + t, rgb, alpha)

    def _font(self, size: int) -> int:
        font = self._font_cache.get(size)
        if font is None:
            font = gdi32.CreateFontW(
                -abs(size), 0, 0, 0, FW_BOLD, 0, 0, 0,
                DEFAULT_CHARSET, 0, 0, ANTIALIASED_QUALITY, DEFAULT_PITCH,
                "Segoe UI")
            self._font_cache[size] = font
        return font

    def label(self, point, text: str, *, color=DEFAULT_COLOR,
              text_color="white", size: int = 15, alpha: int = 230,
              above: bool = True):
        """Draw text on a solid chip anchored at a point.

        The chip is opaque by construction, which also fixes the alpha channel
        that `TextOutW` leaves untouched -- GDI text on a transparent DIB is
        otherwise invisible.
        """
        self._ensure_window()
        if not text:
            return
        rgb = _rgb(color)
        trgb = _rgb(text_color)
        ox, oy = self._origin
        font = self._font(size)
        old_font = gdi32.SelectObject(self._mem_dc, font)
        try:
            extent = w.SIZE()
            gdi32.GetTextExtentPoint32W(self._mem_dc, text, len(text),
                                        ctypes.byref(extent))
            pad_x, pad_y = 5, 2
            cw = extent.cx + pad_x * 2
            ch = extent.cy + pad_y * 2
            x0 = int(point.x) - ox
            y0 = int(point.y) - oy - (ch if above else 0)

            # Keep the chip on screen: a box at the top edge would push its
            # label off the desktop and silently lose the most useful text.
            x0 = max(0, min(self._size[0] - cw, x0))
            y0 = max(0, min(self._size[1] - ch, y0))

            self._blend(y0, y0 + ch, x0, x0 + cw, rgb, alpha)

            gdi32.SetBkMode(self._mem_dc, TRANSPARENT_BKMODE)
            gdi32.SetTextColor(self._mem_dc,
                               trgb[0] | (trgb[1] << 8) | (trgb[2] << 16))
            gdi32.TextOutW(self._mem_dc, x0 + pad_x, y0 + pad_y,
                           text, len(text))
            # TextOutW wrote colour but left alpha as the chip set it, which is
            # already opaque -- so premultiplication is identity here and the
            # glyphs come out at full strength.
            self._pixels[y0:y0 + ch, x0:x0 + cw, 3] = max(alpha, 255 if alpha > 200 else alpha)
        finally:
            gdi32.SelectObject(self._mem_dc, old_font)

    # -- presentation ------------------------------------------------------

    def present(self):
        """Push the drawn bitmap to the compositor."""
        self._ensure_window()
        src = w.POINT(0, 0)
        dst = w.POINT(self._origin[0], self._origin[1])
        size = w.SIZE(self._size[0], self._size[1])
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        ok = user32.UpdateLayeredWindow(
            self._hwnd, None, ctypes.byref(dst), ctypes.byref(size),
            self._mem_dc, ctypes.byref(src), 0,
            ctypes.byref(blend), ULW_ALPHA)
        if not ok:
            raise OSError(f"UpdateLayeredWindow failed "
                          f"(error {ctypes.get_last_error()})")
        if not self._visible:
            user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
            self._visible = True

    def _cancel_timer(self):
        if self._hide_timer is not None:
            self._hide_timer.cancel()
            self._hide_timer = None

    def hide(self):
        """Hide the overlay without destroying it."""
        self._cancel_timer()
        if self._hwnd:
            self._pixels[:] = 0
            user32.ShowWindowAsync(self._hwnd, SW_HIDE)
            self._visible = False

    def expire_in(self, seconds: float):
        """Auto-hide after a delay, without blocking the caller.

        Non-blocking on purpose: the common case is drawing boxes every frame
        inside a running automation loop, where a sleep would be the thing you
        are trying to measure.
        """
        self._cancel_timer()
        if seconds and seconds > 0:
            self._hide_timer = threading.Timer(seconds, self.hide)
            self._hide_timer.daemon = True
            self._hide_timer.start()


_overlay: Overlay | None = None


def overlay() -> Overlay:
    """The process-wide overlay, created on first use."""
    global _overlay
    if _overlay is None:
        _overlay = Overlay()
    return _overlay


def _label_color(label: str, assigned: dict) -> str:
    if label not in assigned:
        assigned[label] = PALETTE[len(assigned) % len(PALETTE)]
    return assigned[label]


def show(items, *, color=None, seconds: float | None = 2.0,
         labels: bool = True, width: int = 2, fill_alpha: int = 0,
         clear: bool = True, text_size: int = 15,
         crosshair_at=None) -> Overlay:
    """Draw detections, OCR hits, regions or points on the screen.

    Accepts whatever the vision and OCR calls return, so the debug line is a
    mirror of the call being debugged:

        auto.show(auto.look())
        auto.show(auto.read("Onayla"))
        auto.show(my_region, color="cyan", seconds=None)

    `seconds=None` leaves it up until the next `show()` or `hide()`, which is
    what you want when drawing every frame of a loop.
    """
    ov = overlay()
    if clear:
        ov.clear()

    assigned: dict[str, str] = {}
    drawn = 0

    for item in _iter_drawables(items):
        kind, box, text, score = item
        if color is not None:
            c = color
        elif text and kind == "detection":
            c = _label_color(text, assigned)
        else:
            c = DEFAULT_COLOR

        if box is not None:
            ov.rect(box, color=c, width=width, fill_alpha=fill_alpha)
            if labels and text:
                caption = f"{text} {score:.2f}" if score is not None else text
                from .screen import Point
                ov.label(Point(box.x, box.y), caption, color=c,
                         size=text_size)
            drawn += 1
        elif text is None:
            continue

    if crosshair_at is not None:
        ov.crosshair(crosshair_at)

    ov.present()
    ov.expire_in(seconds) if seconds else ov._cancel_timer()
    return ov


def _iter_drawables(items):
    """Normalise anything drawable into (kind, box, text, score) tuples."""
    from .screen import Point, Region

    if items is None:
        return
    # A bare Region or Point arrives without being iterable in a useful way.
    if isinstance(items, Region):
        yield ("region", items, None, None)
        return
    if isinstance(items, Point):
        yield ("point", None, None, None)
        return
    if isinstance(items, (str, bytes)):
        raise TypeError("show() draws boxes, not strings; pass "
                        "auto.read(...) or auto.look(...) results")

    try:
        iterator = iter(items)
    except TypeError:
        iterator = iter([items])

    for it in iterator:
        box = getattr(it, "box", None)
        if box is None and isinstance(it, Region):
            yield ("region", it, None, None)
            continue
        if box is None:
            continue
        label = getattr(it, "label", None)
        text = getattr(it, "text", None)
        conf = getattr(it, "confidence", None)
        if label is not None:
            yield ("detection", box, label, conf)
        elif text is not None:
            yield ("text", box, text, conf)
        else:
            yield ("region", box, None, None)


def hide():
    """Clear and hide the overlay."""
    if _overlay is not None:
        _overlay.hide()


def flash(items, seconds: float = 2.0, **kw):
    """Draw, block for `seconds`, then hide. For one-shot inspection."""
    show(items, seconds=None, **kw)
    time.sleep(seconds)
    hide()
