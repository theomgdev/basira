# Windows Desktop Automation Latency & Evasion Invariants

Author: Cahit Karahan (theomgdev), Azade (Hermes Agent)  
Host: Windows 11 (AMD/RTX 3060 Ti, 3440x1440 @ 144 Hz, Python 3.11)

---

## 1. The Screen Acquisition Floor is Compositor-Quantized

- **Observation**: Calling GDI `BitBlt` against the desktop DC (`GetDC(0)`) costs exactly **6.94 ms** for a 64x64 region, 320x240 region, and 1280x720 region.
- **Root Cause**: `DwmFlush()` on a 144 Hz display blocks for **6.933 ms** ($1 / 144 = 0.006944\text{ s}$). Desktop DC blits synchronize with the DWM compositor swap-chain. Screen acquisition latency is therefore quantized to the monitor's refresh interval, not pixel bandwidth.
- **Architectural Consequence**: Per-query pixel inspection (`PixelGetColor` model) is fundamentally anti-pattern on composited operating systems. The unit of screen acquisition must be an immutable **Frame** cached across the refresh horizon ($\sim 6.94\text{ ms}$). Vectorized in-memory searches over a captured frame take $\sim 0.44\text{ }\mu\text{s}$ per query.

## 2. The Window-DC Seduction

- **Observation**: Calling `BitBlt` against a specific window DC (`GetWindowDC(hwnd)`) costs **0.018 ms** (385x faster than desktop DC).
- **The Defect**: Live testing across active top-level windows revealed that **7 out of 8 windows return an all-black (mean=0, std=0, unique_colors=1) buffer**.
- **Root Cause**: Modern UI toolkits (Chromium, CEF, DirectComposition, XAML, WPF) render into GPU swapchains handled directly by DWM. Their GDI window DC contains no pixel data unless `PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT)` is invoked (which takes the standard 6.95 ms compositor frame). Window DC blits must be probed for color variance before being trusted.

## 3. The SendInput Batching Sweet Spot

- **Observation**:
  - 1 mouse event via `SendInput`: **26 $\mu$s**
  - 8 mouse events in 1 `SendInput` call: **47 $\mu$s total** ($\mathbf{5.87\text{ }\mu\text{s/event}}$)
  - 32 mouse events: $423\text{ }\mu\text{s}$ ($13.2\text{ }\mu\text{s/event}$)
  - 128 mouse events: $2042\text{ }\mu\text{s}$ ($16.0\text{ }\mu\text{s/event}$)
- **Root Cause**: Kernel transition overhead is amortized up to the internal driver queue chunk limit ($\sim 8-16$ events). Beyond that, the driver thread synchronizes and blocks.
- **Consequence for Humanized Motion**: Emitting an 80-point biological trajectory in chunks of 8 events spaced by USB polling intervals (1-7 ms) is **cheaper and faster** than emitting individual events in a loop.

## 4. Text Services Framework (TSF) Ingestion Limits

- **Observation**: Emitting multiple `KEYEVENTF_UNICODE` characters with zero delay ($<2\text{ ms}$) into modern Windows 11 controls (XAML Notepad, Electron, RichEdit) causes character dropping, character merging, and tail repetition (e.g. `'Basira ok'` becoming `'Basira kk'`).
- **Root Cause**: Windows TSF dispatches `VK_PACKET` events into `WM_IME_COMPOSITION` / `WM_CHAR` streams. If down/up events land within the same message queue slice, the composition buffer fails to commit previous codepoints before the next arrives.
- **Rule**: Minimum delay between consecutive Unicode characters must be $\mathbf{\ge 6\text{ ms}}$. The theoretical floor is $\sim 4\text{ ms}$, and $5\text{ ms}$ passed on an idle machine, but under load `'日本語'` collapsed to `'本本語'` at $5\text{ ms}$ while $6\text{ ms}$ held across repeated runs. At 6 ms, typing throughput is 166 characters per second with 100% fidelity across all Unicode planes.

## 5. WM_MOUSEMOVE Coalescing During Drag

- **Observation**: `SendInput` of 8 `MOUSEEVENTF_MOVE` events in one call while the left button was held delivered **one** `WM_MOUSEMOVE` to the target window (verified by a Tk canvas binding `<B1-Motion>` and counting the callbacks: 9 total instead of the expected 20+).
- **Root Cause**: Windows coalesces pending `WM_MOUSEMOVE` messages in the thread queue (`QS_MOUSEMOVE`) between message-pump iterations. Button-down/up and keyboard events are NOT coalesced, which is why batching is free for clicks and keystrokes but destructive for drag strokes.
- **Rule**: While a mouse button is held, each waypoint must be followed by a brief yield ($\sim 1.5\text{ ms}$) so the target thread pumps the message before the next arrives. When the button is up, batching at 8 events remains the measured optimum because nobody is listening to the intermediate points.

## 5. 64-bit Ctypes Handle Truncation

- **Observation**: `CreateCompatibleDC(screen_dc)` raised `OverflowError: int too long to convert`; a later `GlobalLock` returned the pointer `0xFFFFFFFFC469B7E0`.
- **Root Cause**: Without explicit `argtypes`/`restype`, ctypes marshals handles as 32-bit signed `int`. A handle whose value exceeds 32 bits either overflows on the way in, or comes back sign-extended on the way out — and the sign-extended pointer is the worse case, because it fails later and elsewhere (an access violation inside `wstring_at`, far from the call that produced it).
- **Rule**: Bind `argtypes` and `restype` on **every** Win32 function the library calls, using `w.HWND`, `w.HDC`, `w.HBITMAP`, `w.HANDLE` or `ctypes.c_void_p`. This is not defensive style; an unbound signature is a latent crash whose probability depends on how long the machine has been running.

## 6. Open-Vocabulary Detection Inherits the Frame-Coherence Rule

- **Observation**: a YOLOE-26s forward pass sits in the same order of magnitude as a compositor frame on GPU, and far above it on CPU.
- **Architectural Consequence**: the naive shape
  ```python
  auto.click(auto.see("cat"))
  auto.click(auto.see("dog"))
  ```
  would run the model twice over what is conceptually one glance. Detection results must therefore be cached on `(frame.seq, region, prompt-set, confidence)`, exactly as pixel queries are cached on the frame. **Changing the prompt set is what forces new work; asking another question is not.**
- **Secondary cost**: `set_classes()` runs the CLIP text encoder, so re-setting an identical prompt tuple pays the encoder for nothing. Compare prompt tuples before calling it.
- **Multi-detection consistency**: when clicking every instance of a thing, all boxes must come from ONE frame. A detect-click-detect loop chases a layout that the first click already changed.

## 7. Ultralytics Resolves Assets Against the Working Directory

- **Observation**: the first `set_classes()` call downloads `mobileclip2_b.ts` (242 MB) into `os.getcwd()`, and a bare checkpoint name resolves the same way.
- **Consequence**: a user running the same script from a different folder re-downloads the entire text encoder. On a 10 Mbit line that is ~7 minutes, silently, per folder.
- **Rule**: `chdir` to a stable cache directory for the duration of the load/`set_classes` call and restore the previous cwd in a `finally`. Pinning the cache turns a per-folder cost into a per-machine cost.

## 8. Real-Time Screen OCR: The 17x Hybrid Pipeline & DBNet Upscale Trap

- **Observation**: Out-of-the-box RapidOCR (`PP-OCRv6 small`, ONNX Runtime) took **186.8 ms** on a 360x140 UI region. Switching to pure `TORCH-CUDA` dropped it to **55.4 ms**, still 4x slower than expected (`det = 46.9 ms`, `rec = 7.7 ms`).
- **Root Cause 1 (The `limit_type='min'` Upscale Trap)**: RapidOCR defaults to `Det.limit_type = 'min'` with `Det.limit_side_len = 736`. A 360x140 crop has its short side scaled to 736 px (producing an 1892x736 = 1.4 MPx tensor — a **19x pixel expansion**!) before DBNet runs. Setting `Det.limit_type = 'max'` (never upscale, only downscale large inputs) dropped DBNet from **46.9 ms to 4.5–9.6 ms** and prevented adjacent letters from blurring into a single box.

- **Root Cause 2 (Stage Asymmetry — CPU Det + GPU Rec Hybrid)**:
  - **DBNet (Detection)** outputs a probability map followed immediately by CPU contour extraction (`cv2.findContours` + polygon unclipping). On small/medium crops, running `PP-OCRv6_det_tiny` in **ONNX Runtime on CPU** takes **4.5 ms** — faster than GPU (**9.6 ms**) because it avoids the PCIe host-to-device-to-host roundtrip.
  - **SVTR (Recognition)** runs over a batch of $N$ cropped word boxes (`[N, 3, 48, 320]`). On CPU, batch cost scales linearly (**40.0 ms**); on **PyTorch CUDA**, the batch executes in parallel in **5.8 ms** (`tiny`) to **11.0 ms** (`small`).
  - Combining **`Det = ONNXRUNTIME` + `Rec = TORCH-CUDA`** achieved **13.1 ms p50 (10.9 ms min)** on the 360x140 test card and **23.5 ms p50 (21.3 ms min)** on a live 620x300 multilingual window (**17x faster** than default).
- **Root Cause 3 (Single-Character DBNet Thresholds)**: DBNet is trained on multi-character text lines, so an isolated single-character button or target (e.g. a lone `'L'` or `'T'`) produces a mean box score of $0.22–0.28$ and gets discarded by the default `box_thresh = 0.5`. Setting `Det.thresh = 0.15` and `Det.box_thresh = 0.20` lets isolated single glyphs pass to SVTR, which recognizes them at $>0.98$ confidence.

## 9. Click-Through HUD Overlays: Six Physical Traps

Drawing boxes over the desktop is how an open-vocabulary automation becomes
debuggable at all — without it, every diagnosis needs captured frames replayed
offline. Six Win32 facts decide whether such an overlay works:

1. **`WS_EX_TRANSPARENT` is input-load-bearing.** A layered window without it intercepts every mouse message inside its rectangle, so a debug overlay would break the automation it exists to show.
2. **`WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW` protect fullscreen state.** Taking focus minimises fullscreen-exclusive 3D games — exactly the case most worth debugging.
3. **`UpdateLayeredWindow(ULW_ALPHA)` expects premultiplied BGRA.** Straight alpha produces bright haloes. GDI `TextOutW` also never writes the alpha channel, so glyphs drawn onto transparent glass are invisible; filling an opaque label chip first makes premultiplication an identity operation and the text comes out solid.
4. **Hiding a layered window from a worker thread does not work through `ShowWindow`.** Synchronous `ShowWindow(SW_HIDE)` blocks on `WM_SHOWWINDOW` and deadlocked the caller for 180+ s when the owning thread had no message pump. `ShowWindowAsync` avoids the deadlock but merely posts to that same unpumped queue, so the overlay stayed painted on screen indefinitely — measured live: 10056 lime px drawn, 10056 still there 400 ms after `hide()`. Blanking the content instead (all-zero premultiplied bitmap through `UpdateLayeredWindow`, a direct call that never touches the message queue) clears the glass from any thread in **1.3 ms**. A fully transparent layered window is visually identical to a hidden one and stays click-through either way.
5. **Assert on screen pixels, not on an internal `visible` flag.** The first version of this overlay's test suite checked its own bookkeeping and passed 4/4 while the boxes were frozen on screen in a live game. A test that reads back the pixels catches it immediately; a test that trusts the object's self-report cannot. Also measure a baseline before drawing — without one, unrelated on-screen content of the same colour is indistinguishable from the overlay's own output.
6. **DIB section updates scale with area, not draw complexity.** Clearing a full 3440x1440 32-bpp top-down DIB, drawing boxes, labels and a crosshair, and presenting via `UpdateLayeredWindow` costs **1.45 ms p50** — safe inside a 30–60 Hz tracking loop.

## 10. Watched-Region Size is a Measured Tradeoff, Not a Free Optimisation

- **Observation** (3440x1440, YOLOE-26s, fixed prompt list):
  - centred 55% crop (1892x792): grab **13.0 ms** + detect **14.5 ms** = **36.3 Hz**
  - full screen (3440x1440): grab **24.3 ms** + detect **24.3 ms** = **20.6 Hz**
- **Root Cause**: both halves scale with area — the DIB copy because it is a memcpy, and the detector because its letterboxed input grows with the source.
- **Consequence**: a crop buys loop rate but makes everything outside it invisible. On an ultrawide that is a large blind region, and a target entering from the screen edge simply never exists as far as the automation is concerned. Expose the fraction as a parameter rather than hard-coding a number that silently encodes one host's aspect ratio and one task's priorities.
