# YOLOE-26

**Fast, organic, and ultra-simple desktop automation for Python — inspired by AutoIt.**

YOLOE-26 is built on a single premise: **automation code should read like plain English, execute with hardware-floor latency, and look completely human on wire whenever you toggle it.**

Zero heavyweight dependencies (only standard library `ctypes` + `numpy`). Zero guesswork.

---

## Quickstart

```python
from yoloe26 import auto

# 1. Ultra-simple window discovery & control
notepad = auto.wait_window("Notepad")
notepad.activate()

# 2. Instant or layout-independent typing
auto.type("Hello world!\nTürkçe karakterler: ığüşöç İĞÜŞÖÇ\nUnicode: 日本語 🚀")

# 3. Screen inspection (Frame-coherent, 0.44 us per query)
pos = auto.locate("#ff0000")  # find red block centroid
if pos:
    auto.click(pos)

# 4. Turn on organic motion with one call (anti-bot evasion)
auto.humanize()  # or auto.humanize('careful')
auto.click(500, 300)  # travels along a biological wrist-arc curve with micro-tremor
```

---

## Measured Real-World Performance

All measurements taken live on **Windows 11 (AMD/RTX 3060 Ti, 3440x1440 @ 144Hz, Python 3.11)**:

| Operation | Cost | Mechanism / Invariant |
|---|---|---|
| `GetForegroundWindow` | **0.1 us** | Win32 direct call |
| Window sweep (`EnumWindows`, 264 windows) | **46 us** | Direct Win32 callback |
| SendInput mouse move (cached) | **26 us** | Win32 `SendInput` single event |
| SendInput batched (8 events) | **5.9 us/ev** | Win32 batch queue sweet spot |
| Sub-millisecond sleep (`auto.wait(0.001)`) | **1.000 ms** | `timeBeginPeriod(1)` + spin tail |
| Frame query from RAM (`pixel()`, `find()`) | **0.44 us** | Vectorized uint32 NumPy view |
| Screen acquisition (small ROI) | **6.94 ms** | Hardware DWM compositor vsync lock ($1/144\text{s}$) |
| Full screen grab (3440x1440, 5 MPx) | **29-34 ms** | $\max(\text{vsync}, \text{5 MPx DIB memory copy})$ |

### The "Fast-but-Black" Window DC Trap

A raw window DC BitBlt takes only **0.018 ms** (385x faster than screen BitBlt). However, live measurements across real running applications prove that **7 out of 8 windows return a pitch-black flat field** because modern applications (Chrome, Electron, UWP, WPF) are GPU-composited directly by DWM.

YOLOE-26 includes `probe_window_dc(hwnd)` to verify whether a window can actually provide live pixels before ever attempting to read from it. For all other surfaces, `Screen.grab()` uses compositor-coherent sampling.

---

## Frame-Coherent Architecture

In libraries like AutoIt (`PixelGetColor`) or PyAutoGUI (`pixel()`), querying 100 pixels invokes 100 individual screen reads — taking **694 ms** and returning pixels from 100 different moments in time.

In YOLOE-26:
- `auto.grab()` captures a frame once (costing 1 compositor frame).
- `auto.frame()` reuses the existing frame if it is younger than the monitor's refresh period ($6.94\text{ ms}$ at 144Hz).
- 1,000 subsequent pixel queries on the frame take only **0.45 ms** in RAM.

---

## Humanize / Anti-Bot Detection

When `auto.humanize()` is enabled:
1. **Biological Paths**: Moves follow a cubic Bézier trajectory with perpendicular offsets mimicking the human wrist rotation arc, minimum-jerk velocity time-warping (slow start, fast flight, soft landing), and high-frequency neuromuscular tremor.
2. **Batched Injection**: Trajectory waypoints are sent in 8-event HID report bursts matching physical mouse polling intervals (1-7 ms), making organic curves **1.5x faster** than naive per-event automation.
3. **Paced Typing**: Unicode codepoints are injected with variable hold/gap delays matching human typing cadences, with optional typo correction.

Presets: `INSTANT` (default), `FAST`, `NATURAL`, `CAREFUL`.

---

## Verification Suite

Run the full 55-point live verification suite on any Windows machine:

```bash
python verify.py
```

Validates DPI awareness, timer precision, GDI frame caching, synthetic color matching, path geometry, batching economics, live Notepad input roundtrip via Win32 clipboard, and cursor restoration.

---

## License

MIT © [Cahit Karahan](https://github.com/theomgdev)
