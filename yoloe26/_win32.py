"""Raw Win32 bindings. One place, so every hot path above it stays allocation-free.

Measured on this host (RTX 3060 Ti, 3440x1440 @144Hz, py3.11):
    SendInput 1 event .................   26 us
    SendInput batch of 8 ..............  5.9 us/event   <-- sweet spot
    GetForegroundWindow ...............  0.1 us
    EnumWindows (264 windows) .........   46 us
    screen-DC BitBlt (any size) ....... 6.94 ms  == DwmFlush() == 1/144s
    window-DC BitBlt (cached DC) ...... 0.018 ms  but BLACK on composited windows
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    dwmapi = ctypes.WinDLL("dwmapi")
except OSError:  # pragma: no cover - DWM missing only on ancient/headless Windows
    dwmapi = None

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


# --------------------------------------------------------------------------- #
# DPI awareness. Must happen before any coordinate is read, or every pixel
# coordinate silently becomes a virtualized lie on a scaled display.
# --------------------------------------------------------------------------- #
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)


def enable_dpi_awareness() -> str:
    """Opt into real pixels. Returns the mode actually obtained."""
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
        if user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
            return "per-monitor-v2"
    except AttributeError:
        pass
    try:
        shcore = ctypes.WinDLL("shcore")
        if shcore.SetProcessDpiAwareness(2) == 0:  # PROCESS_PER_MONITOR_DPI_AWARE
            return "per-monitor"
    except Exception:
        pass
    try:
        if user32.SetProcessDPIAware():
            return "system"
    except Exception:
        pass
    return "unaware"


# --------------------------------------------------------------------------- #
# Input structures
# --------------------------------------------------------------------------- #
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD),
        ("dwFlags", w.DWORD), ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD),
        ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", w.DWORD), ("wParamL", w.WORD), ("wParamH", w.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", w.DWORD), ("u", _INPUTUNION)]


INPUT_SIZE = ctypes.sizeof(INPUT)

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

user32.SendInput.argtypes = (w.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = w.UINT


# --------------------------------------------------------------------------- #
# GDI structures
# --------------------------------------------------------------------------- #
class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG),
        ("biPlanes", w.WORD), ("biBitCount", w.WORD), ("biCompression", w.DWORD),
        ("biSizeImage", w.DWORD), ("biXPelsPerMeter", w.LONG),
        ("biYPelsPerMeter", w.LONG), ("biClrUsed", w.DWORD), ("biClrImportant", w.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", w.DWORD * 3)]


SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
DIB_RGB_COLORS = 0

SM_CXSCREEN, SM_CYSCREEN = 0, 1
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79

PW_CLIENTONLY = 1
PW_RENDERFULLCONTENT = 2

user32.GetDC.argtypes = (w.HWND,)
user32.GetDC.restype = w.HDC
user32.GetWindowDC.argtypes = (w.HWND,)
user32.GetWindowDC.restype = w.HDC
user32.ReleaseDC.argtypes = (w.HWND, w.HDC)
user32.ReleaseDC.restype = ctypes.c_int
user32.GetForegroundWindow.restype = w.HWND
user32.FindWindowW.restype = w.HWND
user32.WindowFromPoint.argtypes = (w.POINT,)
user32.WindowFromPoint.restype = w.HWND

# Explicit GDI signatures. Without argtypes, ctypes narrows a 64-bit HDC/HBITMAP
# to a C int and either overflows or silently truncates the handle - which is
# the failure mode that makes a GDI wrapper work on one machine and not another.
gdi32.CreateCompatibleDC.argtypes = (w.HDC,)
gdi32.CreateCompatibleDC.restype = w.HDC
gdi32.CreateCompatibleBitmap.argtypes = (w.HDC, ctypes.c_int, ctypes.c_int)
gdi32.CreateCompatibleBitmap.restype = w.HBITMAP
gdi32.SelectObject.argtypes = (w.HDC, w.HGDIOBJ)
gdi32.SelectObject.restype = w.HGDIOBJ
gdi32.DeleteDC.argtypes = (w.HDC,)
gdi32.DeleteDC.restype = w.BOOL
gdi32.DeleteObject.argtypes = (w.HGDIOBJ,)
gdi32.DeleteObject.restype = w.BOOL
gdi32.BitBlt.argtypes = (w.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, w.HDC, ctypes.c_int, ctypes.c_int, w.DWORD)
gdi32.BitBlt.restype = w.BOOL
gdi32.GetDIBits.argtypes = (w.HDC, w.HBITMAP, w.UINT, w.UINT, ctypes.c_void_p,
                            ctypes.c_void_p, w.UINT)
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.GetPixel.argtypes = (w.HDC, ctypes.c_int, ctypes.c_int)
gdi32.GetPixel.restype = w.COLORREF
user32.GetPixel = gdi32.GetPixel
try:
    user32.PrintWindow.argtypes = (w.HWND, w.HDC, w.UINT)
    user32.PrintWindow.restype = w.BOOL
except AttributeError:  # pragma: no cover
    pass

WNDENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

# Window API signatures, for the same reason as the GDI block above: an HWND
# passed without argtypes is truncated to int, and the call then fails on a
# window whose handle happens to exceed 32 bits.
for _name, _args, _ret in [
    ("IsWindow", (w.HWND,), w.BOOL),
    ("IsWindowVisible", (w.HWND,), w.BOOL),
    ("IsWindowEnabled", (w.HWND,), w.BOOL),
    ("IsIconic", (w.HWND,), w.BOOL),
    ("IsZoomed", (w.HWND,), w.BOOL),
    ("GetWindowTextLengthW", (w.HWND,), ctypes.c_int),
    ("GetWindowTextW", (w.HWND, w.LPWSTR, ctypes.c_int), ctypes.c_int),
    ("GetClassNameW", (w.HWND, w.LPWSTR, ctypes.c_int), ctypes.c_int),
    ("GetWindowRect", (w.HWND, ctypes.c_void_p), w.BOOL),
    ("GetClientRect", (w.HWND, ctypes.c_void_p), w.BOOL),
    ("ClientToScreen", (w.HWND, ctypes.c_void_p), w.BOOL),
    ("ScreenToClient", (w.HWND, ctypes.c_void_p), w.BOOL),
    ("ShowWindow", (w.HWND, ctypes.c_int), w.BOOL),
    ("MoveWindow", (w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                    ctypes.c_int, w.BOOL), w.BOOL),
    ("SetWindowPos", (w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                      ctypes.c_int, w.UINT), w.BOOL),
    ("SetForegroundWindow", (w.HWND,), w.BOOL),
    ("BringWindowToTop", (w.HWND,), w.BOOL),
    ("PostMessageW", (w.HWND, w.UINT, w.WPARAM, w.LPARAM), w.BOOL),
    ("GetWindowThreadProcessId", (w.HWND, ctypes.c_void_p), w.DWORD),
    ("GetWindowLongW", (w.HWND, ctypes.c_int), ctypes.c_long),
    ("EnumChildWindows", (w.HWND, WNDENUMPROC, w.LPARAM), w.BOOL),
    ("EnumWindows", (WNDENUMPROC, w.LPARAM), w.BOOL),
    ("GetAncestor", (w.HWND, w.UINT), w.HWND),
    ("GetCursorPos", (ctypes.c_void_p,), w.BOOL),
    ("GetAsyncKeyState", (ctypes.c_int,), ctypes.c_short),
    ("GetKeyState", (ctypes.c_int,), ctypes.c_short),
    ("GetKeyboardLayout", (w.DWORD,), ctypes.c_void_p),
    ("AttachThreadInput", (w.DWORD, w.DWORD, w.BOOL), w.BOOL),
    ("GetSystemMetrics", (ctypes.c_int,), ctypes.c_int),
    ("EmptyClipboard", (), w.BOOL),
]:
    try:
        _fn = getattr(user32, _name)
        _fn.argtypes = _args
        _fn.restype = _ret
    except AttributeError:  # pragma: no cover
        pass

kernel32.OpenProcess.argtypes = (w.DWORD, w.BOOL, w.DWORD)
kernel32.OpenProcess.restype = w.HANDLE
kernel32.CloseHandle.argtypes = (w.HANDLE,)
kernel32.CloseHandle.restype = w.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = (
    w.HANDLE, w.DWORD, w.LPWSTR, ctypes.c_void_p)
kernel32.QueryFullProcessImageNameW.restype = w.BOOL
kernel32.GetCurrentThreadId.restype = w.DWORD


def virtual_screen_rect() -> tuple[int, int, int, int]:
    """(x, y, width, height) spanning every monitor."""
    return (
        user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CYVIRTUALSCREEN),
    )


def refresh_interval_ms() -> float:
    """The compositor frame time. This is the hard floor of every screen read."""
    class DEVMODE(ctypes.Structure):
        _fields_ = [
            ("dmDeviceName", w.WCHAR * 32), ("dmSpecVersion", w.WORD),
            ("dmDriverVersion", w.WORD), ("dmSize", w.WORD), ("dmDriverExtra", w.WORD),
            ("dmFields", w.DWORD), ("dmPositionX", w.LONG), ("dmPositionY", w.LONG),
            ("dmDisplayOrientation", w.DWORD), ("dmDisplayFixedOutput", w.DWORD),
            ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short),
            ("dmYResolution", ctypes.c_short), ("dmTTOption", ctypes.c_short),
            ("dmCollate", ctypes.c_short), ("dmFormName", w.WCHAR * 32),
            ("dmLogPixels", w.WORD), ("dmBitsPerPel", w.DWORD),
            ("dmPelsWidth", w.DWORD), ("dmPelsHeight", w.DWORD),
            ("dmDisplayFlags", w.DWORD), ("dmDisplayFrequency", w.DWORD),
            ("dmICMMethod", w.DWORD), ("dmICMIntent", w.DWORD),
            ("dmMediaType", w.DWORD), ("dmDitherType", w.DWORD),
            ("dmReserved1", w.DWORD), ("dmReserved2", w.DWORD),
            ("dmPanningWidth", w.DWORD), ("dmPanningHeight", w.DWORD),
        ]

    dm = DEVMODE()
    dm.dmSize = ctypes.sizeof(DEVMODE)
    if user32.EnumDisplaySettingsW(None, -1, ctypes.byref(dm)) and dm.dmDisplayFrequency > 1:
        return 1000.0 / dm.dmDisplayFrequency
    return 1000.0 / 60.0
