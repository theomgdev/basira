"""Does the vision layer actually find a named object on the real screen?

The honest test: display an image whose contents we KNOW, on screen, in a
window we own, then ask Basira to find the thing by name and verify the hit
lands inside the image. Asking the model about desktop chrome proves nothing -
nobody can say whether a given box "should" have been found.

Images come from Ultralytics' own test assets (a bus photo with people, and a
cat), so the ground truth is not our opinion.

Nothing is clicked on the user's windows: the viewer is a Tk window this script
creates, and it is destroyed at the end.
"""
from __future__ import annotations

import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from basira import Region, auto
from basira.vision import _encoder_cache_dir

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"   [{'PASS' if ok else 'FAIL'}] {name:<50} {detail}")


def head(t):
    print("\n" + "=" * 104)
    print(t)
    print("=" * 104)


ASSETS = {
    "bus.jpg": "https://ultralytics.com/images/bus.jpg",
    "zidane.jpg": "https://ultralytics.com/images/zidane.jpg",
}


def fetch(name: str, url: str) -> str:
    cache = _encoder_cache_dir().parent / "testdata"
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / name
    if not path.exists():
        print(f"   downloading {name} ...")
        urllib.request.urlretrieve(url, path)
    return str(path)


# --------------------------------------------------------------------------- #
head("0. VISION STATUS")
st = auto.vision.status()
for k, v in st.items():
    print(f"   {k:<16} {v}")
check("ultralytics available", st["available"])

# --------------------------------------------------------------------------- #
head("1. GROUND TRUTH: find named objects in a known image shown on screen")

try:
    import tkinter as tk
except ImportError:
    print("   tkinter unavailable; cannot display a known image")
    sys.exit(1)

bus = fetch("bus.jpg", ASSETS["bus.jpg"])
zid = fetch("zidane.jpg", ASSETS["zidane.jpg"])

# Tk needs the image in a format it reads natively; PNG via PIL keeps it simple.
from PIL import Image

def to_png(src: str) -> tuple[str, int, int]:
    im = Image.open(src).convert("RGB")
    # scale so the whole thing fits comfortably on screen
    sw, sh = auto.screen.size
    max_w, max_h = int(sw * 0.45), int(sh * 0.75)
    im.thumbnail((max_w, max_h))
    out = os.path.splitext(src)[0] + "_view.png"
    im.save(out)
    return out, im.width, im.height


bus_png, bw, bh = to_png(bus)
zid_png, zw, zh = to_png(zid)
print(f"   bus view    {bw}x{bh}")
print(f"   zidane view {zw}x{zh}")

root = tk.Tk()
root.title("basira-vision-test")
root.attributes("-topmost", True)
root.geometry(f"{bw}x{bh}+80+80")
img = tk.PhotoImage(file=bus_png)
label = tk.Label(root, image=img, borderwidth=0, highlightthickness=0)
label.pack()
clicks_received: list[tuple[int, int]] = []
label.bind("<Button-1>", lambda e: clicks_received.append((e.x_root, e.y_root)))
root.update()
time.sleep(0.8)
root.update()

try:
    win = auto.find("basira-vision-test")
    check("viewer window found", win is not None, repr(win))
    area = win.client_rect
    print(f"   image area on screen: {tuple(area)}")

    # --- bus.jpg: contains a bus and several people --------------------- #
    t0 = time.perf_counter()
    got = auto.look(area, prompts=["bus", "person"], fresh=True)
    el = (time.perf_counter() - t0) * 1000
    print(f"\n   detect in {el:.0f} ms -> {got!r}")
    for d in got.all()[:8]:
        print(f"      {d!r}")

    b = got.best("bus")
    check("found a bus in bus.jpg", b is not None,
          f"{b.confidence:.2f}" if b else "nothing")
    if b:
        inside = (area.x <= b.center.x < area.x + area.width
                  and area.y <= b.center.y < area.y + area.height)
        check("bus centre lands inside the image", inside, str(b.center))
        big = b.area > (area.width * area.height) * 0.10
        check("bus box is a substantial part of the frame", big,
              f"{b.area/(area.width*area.height)*100:.0f}% of view")

    people = got.all("person")
    check("found people in bus.jpg", len(people) >= 1, f"{len(people)} found")

    # --- a word that is NOT there: no false positive -------------------- #
    absent = auto.look(area, prompts=["elephant"], fresh=True)
    check("does NOT hallucinate an elephant", absent.best("elephant") is None,
          repr(absent.best("elephant")))

    # --- the headline API: see() + region() ----------------------------- #
    d = auto.see("bus", area)
    check("auto.see('bus') resolves", d is not None, repr(d))
    r = auto.region("bus", area)
    check("auto.region('bus') returns a box", isinstance(r, Region), str(r))

    # --- resolving a word to a click point AND actually clicking it ------ #
    pt = auto._as_point("bus", within=area)
    check("word resolves to a screen point", pt == d.center, f"{pt}")

    origin_cursor = auto.cursor
    win.activate()
    root.update()
    auto.click("bus", within=area)
    for _ in range(10):
        root.update()
        time.sleep(0.02)
    auto.mouse.move(origin_cursor.x, origin_cursor.y)
    check("auto.click('bus') delivered a click to the window",
          len(clicks_received) >= 1, str(clicks_received))
    if clicks_received and r:
        cx, cy = clicks_received[-1]
        in_bus = (r.x <= cx <= r.x + r.width and r.y <= cy <= r.y + r.height)
        check("received click landed inside the bus bounding box", in_bus,
              f"click=({cx},{cy}) box={tuple(r)}")

    # --- NotFound is raised, not silently mis-clicked ------------------- #
    from basira import NotFound
    try:
        auto._as_point("elephant", within=area)
        check("absent word raises NotFound", False, "no exception")
    except NotFound as e:
        check("absent word raises NotFound", True, str(e)[:52] + "...")

    # --- swap the image, confirm detection follows the content ---------- #
    img2 = tk.PhotoImage(file=zid_png)
    label.configure(image=img2)
    label.image = img2
    root.geometry(f"{zw}x{zh}+80+80")
    root.update()
    time.sleep(1.0)
    root.update()
    area2 = auto.find("basira-vision-test").client_rect

    got2 = auto.look(area2, prompts=["person", "bus"], fresh=True)
    print(f"\n   after swapping to zidane.jpg -> {got2!r}")
    for d2 in got2.all()[:6]:
        print(f"      {d2!r}")
    check("finds people in zidane.jpg", got2.count("person") >= 1,
          f"{got2.count('person')} people")
    check("no bus in zidane.jpg", got2.best("bus") is None,
          repr(got2.best("bus")))

    # --- cache: second question on the same frame is free --------------- #
    f = auto.grab(area2)
    t0 = time.perf_counter()
    auto.vision.detect(f, ["person"])
    first = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    for _ in range(20):
        auto.vision.detect(f, ["person"])
    cached = (time.perf_counter() - t0) * 1000
    print(f"\n   first inference on frame: {first:7.1f} ms")
    print(f"   20x same query:           {cached:7.3f} ms ({cached/20*1000:.1f} us each)")
    check("per-frame cache eliminates repeat inference", cached / 20 < first / 20,
          f"{first/max(cached/20, 0.001):.0f}x cheaper")

    # --- warm inference timing ------------------------------------------ #
    times = []
    for _ in range(5):
        t0 = time.perf_counter()
        auto.look(area2, prompts=["person"], fresh=True)
        times.append((time.perf_counter() - t0) * 1000)
    times.sort()
    print(f"   5 fresh detections: p50={times[len(times)//2]:.0f} ms "
          f"min={times[0]:.0f} max={times[-1]:.0f}")
    check("warm detection under 500 ms", times[len(times) // 2] < 500,
          f"{times[len(times)//2]:.0f} ms")

finally:
    root.destroy()

# --------------------------------------------------------------------------- #
head("RESULT")
print(f"   passed {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("   FAILURES:")
    for f_ in FAIL:
        print(f"      - {f_}")
print(f"\n   vision status: {auto.vision.status()}")
sys.exit(1 if FAIL else 0)
