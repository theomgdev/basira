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

- **Observation**: Emitting multiple `KEYEVENTF_UNICODE` characters with zero delay ($<2\text{ ms}$) into modern Windows 11 controls (XAML Notepad, Electron, RichEdit) causes character dropping, character merging, and tail repetition (e.g. `'YOLOE26 ok'` becoming `'YOLOE26 kk'`).
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
