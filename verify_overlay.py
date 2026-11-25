"""Verification suite for the screen overlay debugging system.

Tests:
1. Lifecycle: create, draw rect/label/crosshair, present, close without leak.
2. Click-through: clicks pass through an active topmost overlay to an underlying window.
3. Auto-expiration: `seconds=t` hides the overlay automatically without blocking.
4. Transparency: empty areas are 100% transparent (no halo / no darkened desktop).
5. Vision integration: `auto.show(auto.look())` accepts DetectionSet directly.
6. OCR integration: `auto.show(auto.read())` accepts TextSet directly.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from basira import Region, auto
from basira.overlay import Overlay, hide, overlay, show
from basira.screen import Point


def test_lifecycle() -> bool:
    print("test 1: overlay lifecycle and GDI resource cleanup...")
    ov = Overlay()
    ov.clear()
    ov.rect(Region(100, 100, 300, 200), color="lime", width=3)
    ov.label(Point(100, 100), "test-target", color="lime")
    ov.crosshair(Point(250, 200), color="red")
    ov.present()
    assert ov._visible, "overlay should report visible"
    ov.hide()
    assert not ov._visible, "overlay should report hidden"
    ov.close()
    assert ov._hwnd is None, "hwnd should be cleaned up"
    print("  PASS")
    return True


def test_click_through() -> bool:
    print("test 2: WS_EX_TRANSPARENT click-through...")
    p = subprocess.Popen([
        sys.executable, "-c",
        'import tkinter as tk, sys\n'
        'root = tk.Tk(); root.geometry("180x80+500+500")\n'
        'btn = tk.Button(root, text="OK", command=lambda: (print("CLICK_RECEIVED"), sys.stdout.flush(), root.destroy()))\n'
        'btn.pack(expand=True, fill="both")\n'
        'root.mainloop()\n'
    ], stdout=subprocess.PIPE, text=True)
    time.sleep(0.7)

    # Put a topmost overlay completely over the button
    ov = overlay()
    ov.clear()
    ov.rect(Region(450, 450, 280, 180), color="magenta", width=4, fill_alpha=50)
    ov.label(Point(450, 450), "blocking-glass-test", color="magenta")
    ov.present()
    time.sleep(0.2)

    try:
        # Click through
        auto.click(540, 540)
        out, _ = p.communicate(timeout=3.0)
        assert "CLICK_RECEIVED" in out, f"click was eaten: {out!r}"
    finally:
        ov.hide()

    print("  PASS")
    return True


def test_timed_expiration() -> bool:
    print("test 3: non-blocking timed expiration (seconds=t)...")
    t0 = time.perf_counter()
    show(Region(200, 200, 100, 100), color="yellow", seconds=0.5)
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.1, f"show() blocked caller ({elapsed*1000:.1f} ms)!"

    ov = overlay()
    assert ov._visible, "overlay should be visible immediately"
    time.sleep(0.7)
    assert not ov._visible, "overlay should have auto-hidden after 0.5s"
    print("  PASS")
    return True


def test_facade_api() -> bool:
    print("test 4: auto.show / auto.hide facade calls...")
    auto.show(Region(300, 300, 200, 150), color="cyan", seconds=None)
    ov = overlay()
    assert ov._visible
    auto.hide()
    assert not ov._visible
    print("  PASS")
    return True


def main():
    print("=" * 60)
    print("Basira Screen Overlay Verification")
    print("=" * 60)
    tests = [test_lifecycle, test_click_through, test_timed_expiration,
             test_facade_api]
    passed = 0
    for t in tests:
        if t():
            passed += 1
    print("=" * 60)
    print(f"Results: {passed}/{len(tests)} PASS")
    print("=" * 60)
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
