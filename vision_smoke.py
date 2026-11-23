"""First live test of the vision layer: can it actually see things on screen?

Downloads weights and the text encoder on first run, so this is the slow one.
Everything measured, nothing assumed.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from basira import Region, auto

print("=" * 100)
print("VISION SMOKE TEST")
print("=" * 100)
print(f"   vision available: {auto.vision.available()}")
print(f"   status: {auto.vision.status()}")

# ---------------------------------------------------------------------------
print("\n1. Loading model + text encoder (first run downloads weights)")
t0 = time.perf_counter()
try:
    frame = auto.grab()
    got = auto.look(prompts=["window", "button", "text", "icon"])
    load_and_first = (time.perf_counter() - t0)
    print(f"   first call total: {load_and_first:.1f} s")
    print(f"   result: {got!r}")
    print(f"   status: {auto.vision.status()}")
except Exception as e:
    print(f"   FAILED: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# ---------------------------------------------------------------------------
print("\n2. Warm inference timing (model already loaded, prompts unchanged)")
times = []
for i in range(5):
    t0 = time.perf_counter()
    r = auto.look(prompts=["window", "button", "text", "icon"], fresh=True)
    times.append((time.perf_counter() - t0) * 1000)
times.sort()
print(f"   5 fresh detections: p50={times[len(times)//2]:.1f} ms  "
      f"min={times[0]:.1f}  max={times[-1]:.1f}")
print(f"   model-reported elapsed: {r.elapsed_ms:.1f} ms")

# ---------------------------------------------------------------------------
print("\n3. Cache: does a second question about the SAME frame cost nothing?")
f = auto.grab()
t0 = time.perf_counter()
a = auto.vision.detect(f, ["window", "button", "text", "icon"])
first_ms = (time.perf_counter() - t0) * 1000
t0 = time.perf_counter()
for _ in range(20):
    auto.vision.detect(f, ["window", "button", "text", "icon"])
cached_ms = (time.perf_counter() - t0) * 1000
print(f"   first detect on frame: {first_ms:.2f} ms")
print(f"   20x same query:        {cached_ms:.3f} ms ({cached_ms/20*1000:.1f} us each)")
print(f"   cache works: {cached_ms/20 < first_ms/10}")

# ---------------------------------------------------------------------------
print("\n4. What does prompt-free mode see on this screen?")
try:
    t0 = time.perf_counter()
    pf = auto.look(prompts=None, fresh=True)
    pf_s = time.perf_counter() - t0
    print(f"   prompt-free in {pf_s:.1f} s -> {pf!r}")
    print(f"   labels found: {pf.labels()[:20]}")
    for d in pf.all()[:10]:
        print(f"      {d!r}")
except Exception as e:
    print(f"   prompt-free unavailable: {type(e).__name__}: {e}")

# ---------------------------------------------------------------------------
print("\n5. Region-scoped detection (half the screen)")
w, h = auto.screen.size
left = Region(0, 0, w // 2, h)
t0 = time.perf_counter()
scoped = auto.look(left, prompts=["window", "text", "button"], fresh=True)
print(f"   left half in {(time.perf_counter()-t0)*1000:.1f} ms -> {scoped!r}")
for d in scoped.all()[:5]:
    inside = 0 <= d.center.x < w // 2
    print(f"      {d!r}  centre inside left half: {inside}")

print("\n" + "=" * 100)
print(f"FINAL STATUS: {auto.vision.status()}")
