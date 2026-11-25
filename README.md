# BASIRA

**بصيرة** — *insight: seeing a thing and grasping what it is.*

Desktop automation where you **name what you want** instead of guessing where it is.

```python
from basira import auto

auto.click("cat")                    # finds a cat on screen, clicks it
auto.click("submit button")          # words, not coordinates
auto.drag("red card", "discard pile")
```

In classical usage `basar` is bare sight; `basira` is sight that *understands*. That is exactly the split in this library: the pixel layer sees colours, the vision layer sees a cat.

---

## Why

AutoIt got one thing profoundly right: automation should be writable by someone who is not a programmer. It also left one thing unsolved — scripts are written in coordinates and raw pixel colours, so they break the moment a window moves, a theme changes, or a display scales.

Basira keeps AutoIt's simplicity and replaces the brittle part. Under the hood: Win32 at its latency floor, [Ultralytics YOLOE](https://docs.ultralytics.com/models/yoloe) (*Real-Time Seeing Anything*) for open-vocabulary detection — the class list is a text prompt at inference time, so there is no training step and no fixed vocabulary.

```python
# The brittle way (every other library)
auto.click(847, 392)          # what was at 847,392? nobody knows in six months

# The Basira way
auto.click("play button")     # survives moves, themes, resolutions
```

---

## Install

```bash
pip install basira              # pixels, input, windows — pure ctypes, ~300 KB
pip install 'basira[vision]'    # + open-vocabulary object detection (YOLOE-26)
pip install 'basira[ocr]'       # + real-time 80+ language screen OCR (RapidOCR)
pip install 'basira[all]'       # everything
```

The vision layer is **optional on purpose**. Without it every pixel, input and window API still works; only the word-based calls raise, with the install hint attached.

---

## The API

### Seeing

```python
auto.see("cat")                     # -> Detection | None  (highest confidence)
auto.see_all("button")              # -> [Detection, ...]
auto.sees("error dialog")           # -> bool
auto.region("sidebar")              # -> Region | None  (bounding box)
auto.look()                         # -> DetectionSet: everything on screen
auto.look().labels()                # -> what IS this screen? explore mode
auto.wait_to_see("done", timeout=30)
auto.click_all("checkbox")          # clicks every one, from a single frame
```

Every target-taking call accepts a word, so `move`, `click`, `double_click`, `right_click`, `drag` and `scroll` all work the same way.

### Scoping with `within`

Regions compose, which is how you say *"the one in the left panel"*:

```python
player = auto.region("video player")
auto.click("play button", within=player)

chrome = auto.find("Chrome").client_rect
auto.click("close tab", within=chrome)

dialog = auto.region("dialog")
auto.look(dialog).labels()          # what's in the dialog?
```

### Reading & Acting on Text (Real-Time Multilingual OCR)

Backed by a hybrid **RapidOCR (PP-OCRv6/v5 DBNet + SVTR)** pipeline — **ONNX Runtime** for zero-copy CPU contour detection + **PyTorch CUDA** for parallel batch recognition (**10.9–15.1 ms** on a 360x140 region, **21–23 ms** on a 620x300 window, **80+ languages** including Turkish, English, CJK, Cyrillic, and Arabic):

```python
auto.click_text("Onayla")                          # click a button by its label
auto.click_text("Cancel", within=dialog)           # proportional sub-box slicing inside lines
auto.hover_text("Settings")                        # move pointer over text

# Drag-select text on screen and copy/cut/delete it in one call
token = auto.select_text("TOKEN-7391", action="copy")

# Read all text once, then act on every span from RAM (~6.4 us/query)
spans = auto.read(arena, fresh=True)
for hit in spans.alphabetical(single_chars_only=True):
    auto.click(hit)                                # click appearing letters A -> Z
```

### Pixels (no model required)

```python
auto.pixel(100, 200)                # -> 0xRRGGBB
auto.locate("#ff0000")              # -> Point: colour centroid
auto.locate_all("#ff0000")
auto.count_color("#ff0000")
auto.exists("#ff0000")              # colour -> pixel layer
auto.exists("cat")                  # word   -> vision layer
frame = auto.grab()                 # immutable moment of the screen
frame.bounds("#ff0000")             # -> Region
frame.changed_from(previous)
```

### Input

```python
auto.type("Türkçe ıİşŞğĞ + 日本語 works")   # layout-independent Unicode
auto.press("ctrl+s")                        # or press('ctrl', 's')
auto.tap("enter", count=3)
with auto.hold("shift"):
    auto.click("file")
auto.copy()                                 # -> clipboard text
auto.paste("text")
auto.wait(0.001)                            # real sub-millisecond sleep
```

### Windows

```python
w = auto.wait_window("Notepad")     # waits until actually usable, not just visible
w.activate(); w.maximize()
w.client_rect                       # drawable area, DWM border excluded
w.responsive()                      # is its message pump alive?
auto.windows()                      # every top-level window (46 us)
```

### Humanize

Off by default. One call turns it on:

```python
auto.humanize()              # natural
auto.humanize("careful")     # slower, more tremor, fatigue drift
auto.humanize(False)         # back to instant
auto.click("button", humanize="fast")   # or per-call
```

### Visual Debugging & HUD Overlay

When open-vocabulary vision or OCR misses, diagnosing it usually requires capturing screenshots and inspecting them offline. Basira includes an in-process, click-through (`WS_EX_TRANSPARENT`), per-pixel-alpha overlay that draws directly over running games and apps without stealing focus or intercepting clicks:

```python
# Show YOLOE detections for 2 seconds (default), color-coded per class
auto.show(auto.look())

# Show OCR text hits on screen for 5 seconds
auto.show(auto.read(), seconds=5.0)

# Highlight a watched ROI or point
auto.show(my_region, color="cyan", seconds=None)    # stays until next show/hide
auto.hide()                                        # clear overlay

# One-shot blocking flash for quick sanity inspection
auto.flash(auto.look(), seconds=1.5)
```

- **Zero input interference:** `WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW` ensures the overlay never eats a click or drops a game out of fullscreen.
- **Sub-2ms frame latency:** Premultiplied DIB section update (`1.45 ms p50` on 3440×1440), negligible overhead in 30–60 Hz automation loops.
- **Thread-safe clearing:** `hide()` blanks the glass through `UpdateLayeredWindow`, a direct call rather than a window message — so it works from an expiry timer or any worker thread even though an automation loop never pumps a message queue.

---

## Measured Performance

Live measurements on **Windows 11, RTX 3060 Ti, 3440x1440 @ 144 Hz, Python 3.11/3.13**:

| Operation | Cost | Why that number |
|---|---|---|
| `GetForegroundWindow` | **0.1 us** | direct Win32 call |
| Window sweep (264 windows) | **46 us** | `EnumWindows` callback — lookup is free, don't cache handles |
| `SendInput`, 1 event | **26 us** | one kernel transition |
| `SendInput`, batch of 8 | **5.9 us/event** | measured optimum; 32 decays to 13.2, 128 to 16.0 |
| `auto.wait(0.001)` | **1.000 ms** | `timeBeginPeriod(1)` + spin tail (default timer is 15.6 ms) |
| Pixel query from a frame | **0.44 us** | vectorised uint32 compare in RAM |
| Screen read, bounded region | **6.94 ms** | = `DwmFlush()` = 1/144 s — the compositor sets this, not us |
| Full-screen grab (5 MPx) | **29-34 ms** | max(vsync, 5 MB DIB copy) |
| Unicode typing | **6 ms/char** | TSF needs >=6 ms under load or CJK codepoints merge |
| Screen OCR (`PP-OCRv6` hybrid ORT+CUDA) | **10.9–23.5 ms** | 360x140 ROI in 13.1 ms p50 (10.9 ms min); **6.4 us** cached |
| Open-vocabulary detection (`yoloe-26s-seg`) | **35 ms p50** | warm GPU forward pass; **1.2 us** on a cached frame |

Three findings worth stating plainly, because they shaped the architecture:

**Screen reads are frame-quantized.** A 1-pixel read and a 1280x720 read both cost 6.94 ms, exactly matching `DwmFlush()` on a 144 Hz display. So a per-query API (`PixelGetColor`, `pyautogui.pixel()`) pays a full compositor frame per question — 100 questions cost 694 ms *and* return pixels from 100 different moments. Basira captures a `Frame` once and answers from RAM.

**The fast window-DC path is usually a lie.** Blitting a window DC takes 0.018 ms — 385x faster. But measured across live windows, **7 of 8 returned pitch black** (Chrome, Electron, UWP — all GPU-composited). `probe_window_dc()` checks colour variance before trusting that path, because fast-and-empty is worse than slow-and-correct.

**Organic motion is cheaper than naive automation.** An 89-point human-like trajectory sent in 8-event batches beats one-call-per-event by **1.4-1.7x** measured. Looking human is not a latency tax; sending events one at a time is.

---

## Verification

```bash
python verify.py          # 55 live checks: all pass on the dev host
python vision_smoke.py    # vision layer, timings and cache behaviour
```

`verify.py` drives the real OS: launches its own Notepad, types ASCII + Turkish + Japanese, reads it back through the Win32 clipboard, verifies organic path geometry lands pixel-exact, and restores the cursor to where you left it.

---

## Limits, stated honestly

- **Windows only.** The input and screen layers are Win32. A Linux/macOS backend is possible but does not exist.
- **Zero-shot detection is not trained-on-your-data detection.** YOLOE lands roughly in the 22-40 mAP band on LVIS. Reach for it to cover things you cannot train for.
- **A prompt describes appearance, not relationships.** "damaged", "left-most", "the one being carried" have no visual handle. Use `within=` for spatial constraints instead.
- **First vision call downloads weights** (~30 MB) plus a text encoder (~254 MB), and the encoder writes a `.ts` file into the working directory. `auto.vision.status()` reports what is loaded and what it measured.
- **Large prompt sets cost latency.** Prompt embeddings are compared against region features every forward pass; the full 4,585-name vocabulary is ~89% slower than 80 names, and FLOPs counters won't warn you.

---

## License

MIT © [Cahit Karahan](https://github.com/theomgdev)
