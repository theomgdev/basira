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
- **Rule**: Minimum delay between consecutive Unicode characters must be $\mathbf{\ge 4-5\text{ ms}}$. At 5 ms, typing throughput is 200 characters per second with 100% fidelity across all Unicode planes.
