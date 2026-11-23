"""Live verification of every claim YOLOE-26 makes about itself.

This is not a unit-test suite; it drives the real OS and reports measured
numbers. Input tests run against a Notepad instance this script launches and
closes itself, so nothing types into the user's work.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

from basira import CAREFUL, NATURAL, Point, Region, auto, probe_window_dc
from basira import _win32 as _w

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"   [{'PASS' if ok else 'FAIL'}] {name:<52} {detail}")


def head(t):
    print("\n" + "=" * 112)
    print(t)
    print("=" * 112)


head("0. ENVIRONMENT (what the library measured about this host)")
env = auto.describe_environment()
for k, v in env.items():
    print(f"   {k:<24} {v}")
check("DPI awareness obtained", env["dpi_mode"] != "unaware", env["dpi_mode"])
check("precise timer acquired", env["precise_timer"])
check("refresh detected", 1.0 < env["refresh_ms"] < 40.0, f"{env['refresh_ms']} ms")

# --------------------------------------------------------------------------- #
head("1. FRAME COHERENCE: cache vs forced grab vs RAM query")
auto.screen.stats.update(grabs=0, cache_hits=0)

t0 = time.perf_counter()
f_full = auto.grab()
full_ms = (time.perf_counter() - t0) * 1000

t0 = time.perf_counter()
for _ in range(50):
    auto.frame()
cached_ms = (time.perf_counter() - t0) * 1000
print(f"   full screen grab ({f_full.width}x{f_full.height})     {full_ms:8.3f} ms   (includes 5 MPx memcpy)")
print(f"   50x frame() from cache               {cached_ms:8.3f} ms   ({cached_ms/50*1000:.2f} us each)")
print(f"   stats                                {auto.screen.stats}")
check("cached frame() is RAM-speed (<10 us)", cached_ms / 50 < 0.010,
      f"{cached_ms/50*1000:.2f} us")
check("cache hits recorded", auto.screen.stats["cache_hits"] >= 50)

# bounded region grab: this one DOES track the compositor floor
t0 = time.perf_counter()
for _ in range(10):
    auto.grab((0, 0, 320, 240))
small_ms = (time.perf_counter() - t0) * 1000
print(f"   10x grab() of 320x240                {small_ms:8.3f} ms   ({small_ms/10:.3f} ms each)")
check("small-region grab tracks refresh interval",
      abs(small_ms / 10 - env["refresh_ms"]) < 2.5,
      f"{small_ms/10:.3f} ms vs {env['refresh_ms']:.3f} ms floor")

# RAM queries on a captured frame
t0 = time.perf_counter()
for i in range(1000):
    f_full.pixel(100 + i % 50, 100 + i % 50)
q = (time.perf_counter() - t0) * 1000
print(f"   1000 pixel queries on RAM buffer     {q:8.3f} ms   ({q/1000*1000:.2f} us each)")
check("1000 queries cheaper than 1 frame grab", q < env["refresh_ms"],
      f"{q:.2f} ms vs {env['refresh_ms']:.2f} ms")

# --------------------------------------------------------------------------- #
head("2. FRAME QUERY CORRECTNESS (vs a known synthetic pattern)")
# Build a frame from a known array to verify search math exactly.
from basira.screen import Frame

art = np.zeros((40, 60, 4), np.uint8)
art[10:20, 30:45, 2] = 255          # a red block, R in BGRA index 2
art[0, 0] = (0, 255, 0, 0)          # single green pixel at origin
test = Frame(art, Point(500, 300), time.perf_counter(), 0, "synthetic")

check("pixel() reads RGB correctly", test.pixel(500, 300) == 0x00FF00,
      f"got 0x{test.pixel(500,300):06x} expected 0x00ff00")
check("pixel() honours frame origin", test.pixel(30 + 500, 10 + 300) == 0xFF0000,
      f"got 0x{test.pixel(530,310):06x}")
check("count() exact", test.count("#ff0000") == 150, f"{test.count('#ff0000')} (expect 150)")
cb = test.bounds("#ff0000")
check("bounds() exact", cb == Region(530, 310, 15, 10), str(cb))
cc = test.centroid("#ff0000")
check("centroid() is block centre", cc == Point(537, 314), str(cc))
check("find() is first-scanline", test.find("#ff0000") == Point(530, 310), str(test.find("#ff0000")))
check("find() miss returns None", test.find("#123456") is None)
check("tolerance widens match", test.count((250, 0, 0), tolerance=10) == 150,
      str(test.count((250, 0, 0), tolerance=10)))
check("tolerance=0 rejects near miss", test.count((250, 0, 0)) == 0)
check("hex forms agree", test.count("#f00") == test.count("ff0000") == test.count(0xFF0000) == 150)
check("rgb tuple form agrees", test.count((255, 0, 0)) == 150)
sub = test.crop(Region(530, 310, 15, 10))
check("crop() isolates region", sub.count("#ff0000") == 150 and sub.width == 15, repr(sub))
check("changed_from() detects difference", test.changed_from(None))
same = Frame(art.copy(), Point(500, 300), time.perf_counter(), 1, "synthetic")
check("changed_from() detects identity", not test.changed_from(same))

# --------------------------------------------------------------------------- #
head("3. HUMANIZE PATH GEOMETRY (does organic motion still land exactly?)")
p = NATURAL.seeded(42)
errs, lens, bows = [], [], []
for (x0, y0, x1, y1) in [(0, 0, 500, 300), (100, 100, 110, 105),
                         (1000, 700, 200, 150), (50, 50, 1800, 900)]:
    pts = p.path(x0, y0, x1, y1)
    lens.append(len(pts))
    errs.append(abs(pts[-1][0] - x1) + abs(pts[-1][1] - y1))
    # how far does the path deviate from the straight chord?
    if len(pts) > 3:
        import math
        d = math.hypot(x1 - x0, y1 - y0)
        dev = max(abs((x1-x0)*(py-y0) - (y1-y0)*(px-x0)) / max(d, 1) for px, py in pts)
        bows.append(dev)
print(f"   path lengths           {lens}")
print(f"   endpoint errors        {errs}")
print(f"   max lateral deviation  {[round(b,1) for b in bows]} px")
check("organic path lands exactly on target", all(e == 0 for e in errs), str(errs))
check("path is curved, not a straight line", all(b > 2.0 for b in bows))
check("short moves stay short", p.path(10, 10, 11, 11) == [(11, 11)])
check("seeded profile is reproducible",
      NATURAL.seeded(7).path(0, 0, 400, 400) == NATURAL.seeded(7).path(0, 0, 400, 400))
check("different seeds differ",
      NATURAL.seeded(1).path(0, 0, 400, 400) != NATURAL.seeded(2).path(0, 0, 400, 400))
inst = auto.profile.__class__  # INSTANT has no path
from basira import INSTANT
check("INSTANT profile emits no curve", INSTANT.steps_per_100px == 0)

# --------------------------------------------------------------------------- #
head("4. BATCHING CLAIM: is organic motion really cheaper than naive per-event?")
from basira import _win32 as _w
from basira.mouse import _mouse_event, _norm_abs, _send

pts = NATURAL.seeded(3).path(500, 500, 1400, 900)
flags = _w.MOUSEEVENTF_MOVE | _w.MOUSEEVENTF_ABSOLUTE | _w.MOUSEEVENTF_VIRTUALDESK
evs = [_mouse_event(flags, *_norm_abs(x, y)) for x, y in pts]

start = auto.cursor
t0 = time.perf_counter()
for e in evs:
    _send([e])
naive = (time.perf_counter() - t0) * 1000
t0 = time.perf_counter()
_send(evs)
batched = (time.perf_counter() - t0) * 1000
auto.mouse.move(start.x, start.y)  # put the pointer back

print(f"   {len(evs)}-point path, one call per event   {naive:8.3f} ms")
print(f"   {len(evs)}-point path, batched at 8         {batched:8.3f} ms")
check("batching beats per-event sending", batched < naive,
      f"{naive/max(batched,0.001):.1f}x faster")

# --------------------------------------------------------------------------- #
head("5. PRECISE TIMING (Windows default timer is 15.6 ms)")
for want_ms in (0.5, 1.0, 2.0, 5.0):
    got = []
    for _ in range(20):
        t0 = time.perf_counter()
        auto.clock.sleep_ms(want_ms)
        got.append((time.perf_counter() - t0) * 1000)
    got.sort()
    med = got[len(got) // 2]
    print(f"   sleep_ms({want_ms:>4.1f})  ->  p50={med:7.3f} ms   "
          f"min={got[0]:6.3f}  max={got[-1]:6.3f}  overshoot={med-want_ms:+.3f} ms")
    check(f"sleep_ms({want_ms}) accurate within 1ms", abs(med - want_ms) < 1.0,
          f"{med:.3f} ms")

# --------------------------------------------------------------------------- #
head("6. WINDOW LAYER (read-only)")
t0 = time.perf_counter()
wins = auto.windows()
el = (time.perf_counter() - t0) * 1000
print(f"   enumerated {len(wins)} windows in {el:.3f} ms")
check("window sweep under 5 ms", el < 5.0, f"{el:.3f} ms")

act = auto.active
print(f"   active window          {act}")
check("active window found", act is not None and act.exists)
check("active window reports pid", act.pid > 0, f"pid={act.pid}")
check("active window names its process", bool(act.process_name), act.process_name)
check("active window is responsive", act.responsive())
r, cr = act.rect, act.client_rect
print(f"   window rect            {tuple(r)}")
print(f"   client rect            {tuple(cr)}   (delta reveals the DWM border)")
check("client rect inside window rect",
      cr.width <= r.width and cr.height <= r.height, f"{cr.width}x{cr.height} <= {r.width}x{r.height}")
rt = act.to_screen(0, 0)
back = act.to_client(rt.x, rt.y)
check("client<->screen roundtrip", back == Point(0, 0), f"{back}")

found = auto.find_all(class_name="Chrome_WidgetWin")
check("find by class works", len(found) >= 1, f"{len(found)} matches")
check("find by missing title returns None", auto.find("zzz-no-such-window-zzz") is None)
w_at = auto.window_at(act.client_rect.center)
check("window_at resolves a point", w_at is not None, repr(w_at))

# --------------------------------------------------------------------------- #
head("7. WINDOW-DC PROBE (the fast-but-black trap)")
tested = 0
for wd in wins[:8]:
    if wd.rect.width < 300:
        continue
    v = probe_window_dc(wd.hwnd)
    tested += 1
    print(f"   {wd.class_name[:26]:<26} usable={str(v['usable']):<5} "
          f"{v.get('ms', 0):6.3f} ms  colors={v.get('distinct_colors','?'):<5} {v['reason']}")
check("probe runs on live windows", tested > 0, f"{tested} windows")
check("probe rejects flat-field windows",
      any(not probe_window_dc(wd.hwnd)["usable"] for wd in wins[:6]))

# --------------------------------------------------------------------------- #
head("8. LIVE INPUT against a Notepad we own (not the user's windows)")
proc = subprocess.Popen(["notepad.exe"])
try:
    nw = auto.wait_window(class_name="Notepad", timeout=8)
    if nw is None:
        nw = auto.wait_window("Not Defteri", timeout=4) or auto.wait_window("Notepad", timeout=4)
    check("launched window found", nw is not None, repr(nw))
    if nw:
        check("window became ready", nw.wait_ready(timeout=5))
        check("window activated", nw.activate(timeout=2), f"active={nw.active}")
        auto.wait(0.35)

        # locate the editable surface and click into it
        edit = None
        for c in nw.children():
            if "edit" in c.class_name.lower() or "richedit" in c.class_name.lower():
                edit = c
                break
        target = (edit or nw).client_rect
        auto.click(target.center)
        auto.wait(0.2)

        # Clear any restored session text in Windows 11's tabbed Notepad
        auto.press("ctrl", "a")
        auto.tap("delete")
        auto.wait(0.2)

        probe = "YOLOE26 ok"
        t0 = time.perf_counter()
        auto.type(probe)
        fast_ms = (time.perf_counter() - t0) * 1000
        auto.wait(0.3)

        auto.press("ctrl", "a")
        auto.wait(0.15)
        clip = auto.copy().strip()
        print(f"   typed    {probe!r} in {fast_ms:.3f} ms ({fast_ms/len(probe):.3f} ms/char)")
        print(f"   read back {clip!r}")
        check("typed text arrived verbatim", clip == probe, f"{clip!r}")
        check("instant typing is sub-10ms per char (200 chars/s)", fast_ms / len(probe) < 10.0,
              f"{fast_ms/len(probe):.3f} ms/char")

        # unicode / layout independence: characters absent from a US layout
        auto.press("ctrl", "a")
        auto.tap("delete")
        auto.wait(0.2)
        uni = "Türkçe ıİşŞğĞ + 日本語"
        auto.type(uni)
        auto.wait(0.3)
        auto.press("ctrl", "a")
        clip2 = auto.copy().strip()
        print(f"   unicode sent    {uni!r}")
        print(f"   unicode read    {clip2!r}")
        check("unicode survives layout translation", clip2 == uni, f"{clip2!r}")

        # humanized typing, measured
        auto.press("ctrl", "a")
        auto.tap("delete")
        auto.wait(0.2)
        auto.humanize(CAREFUL)
        t0 = time.perf_counter()
        auto.type("organic")
        slow_ms = (time.perf_counter() - t0) * 1000
        auto.humanize(False)
        auto.wait(0.3)
        auto.press("ctrl", "a")
        clip3 = auto.copy().strip()
        print(f"   humanized typing  {slow_ms:.1f} ms for 7 chars "
              f"({slow_ms/7:.1f} ms/char)  vs instant {fast_ms/len(probe):.3f} ms/char")
        check("humanized text is still correct", clip3 == "organic", f"{clip3!r}")
        check("humanized typing is measurably slower", slow_ms / 7 > fast_ms / len(probe) * 10)

        # reversal: leave nothing behind
        auto.press("ctrl", "a")
        auto.tap("delete")
        auto.wait(0.2)
        from basira import clipboard
        clipboard.clear()
        auto.press("ctrl", "a")
        clip4 = auto.copy().strip()
        check("test content cleaned up", clip4 == "", f"{clip4!r}")
finally:
    try:
        nw = auto.find(class_name="Notepad")
        if nw:
            nw.close()
            time.sleep(0.6)
    except Exception:
        pass
    if proc.poll() is None:
        proc.kill()

# --------------------------------------------------------------------------- #
head("9. MOUSE POSITION ROUNDTRIP (absolute coordinate math)")
origin = auto.cursor
errors = []
for tx, ty in [(100, 100), (1500, 800), (3000, 1200), (2, 2)]:
    auto.mouse.move(tx, ty)
    time.sleep(0.02)
    got = auto.cursor
    errors.append((abs(got.x - tx), abs(got.y - ty)))
    print(f"   requested ({tx:>5},{ty:>5})  ->  got ({got.x:>5},{got.y:>5})  "
          f"err=({got.x-tx:+d},{got.y-ty:+d})")
auto.mouse.move(origin.x, origin.y)
check("absolute positioning accurate to <=1 px",
      all(dx <= 1 and dy <= 1 for dx, dy in errors), str(errors))
check("cursor restored to where the user left it",
      abs(auto.cursor.x - origin.x) <= 1 and abs(auto.cursor.y - origin.y) <= 1)

# --------------------------------------------------------------------------- #
head("RESULT")
print(f"   passed {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("   FAILURES:")
    for f_ in FAIL:
        print(f"      - {f_}")
print()
print(f"   {auto.describe_environment()['stats']}")
sys.exit(1 if FAIL else 0)
