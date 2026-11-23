"""Keyboard injection.

The correctness problem this file exists to solve: virtual-key codes are
layout-dependent. `VkKeyScan('@')` returns a different key+modifier
combination on a Turkish Q layout than on US English, and a script that hard-codes
VK codes types garbage the moment the user's layout differs from the author's.

So text goes out as `KEYEVENTF_UNICODE` scan values, which bypass layout
translation entirely and deliver the exact codepoint - including characters that
have no key at all on the active layout (emoji, Turkish dotless i on a US
layout, CJK). Named keys (enter, f5, ctrl) still go out as real VKs, because
that is what applications listen for in their accelerator tables.
"""
from __future__ import annotations

import ctypes

from . import _win32 as _w
from . import humanize as _h
from .mouse import _clock, _key_event, _send

# Named keys: VK code, and whether it needs the extended-key flag.
# The extended flag matters more than it looks - without it, the numpad Enter
# and the main Enter are indistinguishable, and arrow keys can land as numpad
# digits when NumLock is on.
KEYS: dict[str, tuple[int, bool]] = {
    # control
    "backspace": (0x08, False), "tab": (0x09, False), "clear": (0x0C, False),
    "enter": (0x0D, False), "return": (0x0D, False), "shift": (0x10, False),
    "ctrl": (0x11, False), "control": (0x11, False), "alt": (0x12, False),
    "pause": (0x13, False), "capslock": (0x14, False), "esc": (0x1B, False),
    "escape": (0x1B, False), "space": (0x20, False),
    # navigation (all extended)
    "pageup": (0x21, True), "pagedown": (0x22, True), "end": (0x23, True),
    "home": (0x24, True), "left": (0x25, True), "up": (0x26, True),
    "right": (0x27, True), "down": (0x28, True),
    "insert": (0x2D, True), "delete": (0x2E, True), "del": (0x2E, True),
    # modifiers, sided
    "lshift": (0xA0, False), "rshift": (0xA1, True),
    "lctrl": (0xA2, False), "rctrl": (0xA3, True),
    "lalt": (0xA4, False), "ralt": (0xA5, True),
    "win": (0x5B, True), "lwin": (0x5B, True), "rwin": (0x5C, True),
    "apps": (0x5D, True), "menu": (0x5D, True),
    # system
    "printscreen": (0x2C, True), "snapshot": (0x2C, True),
    "scrolllock": (0x91, False), "numlock": (0x90, True),
    # numpad
    "num0": (0x60, False), "num1": (0x61, False), "num2": (0x62, False),
    "num3": (0x63, False), "num4": (0x64, False), "num5": (0x65, False),
    "num6": (0x66, False), "num7": (0x67, False), "num8": (0x68, False),
    "num9": (0x69, False), "nummul": (0x6A, False), "numadd": (0x6B, False),
    "numsub": (0x6D, False), "numdec": (0x6E, False), "numdiv": (0x6F, True),
    # media
    "volmute": (0xAD, True), "voldown": (0xAE, True), "volup": (0xAF, True),
    "nexttrack": (0xB0, True), "prevtrack": (0xB1, True),
    "stop": (0xB2, True), "playpause": (0xB3, True),
}
for _i in range(1, 25):
    KEYS[f"f{_i}"] = (0x6F + _i, False)
for _c in "abcdefghijklmnopqrstuvwxyz":
    KEYS[_c] = (ord(_c.upper()), False)
for _c in "0123456789":
    KEYS[_c] = (ord(_c), False)

ALIASES = {"cmd": "win", "super": "win", "ret": "enter", "pgup": "pageup",
           "pgdn": "pagedown", "pgdown": "pagedown", "esc": "escape"}

_MODIFIER_NAMES = {"ctrl", "control", "alt", "shift", "win", "cmd", "super",
                   "lctrl", "rctrl", "lalt", "ralt", "lshift", "rshift",
                   "lwin", "rwin"}


def _resolve_key(name: str) -> tuple[int, bool]:
    key = ALIASES.get(name.lower().strip(), name.lower().strip())
    if key not in KEYS:
        raise ValueError(
            f"unknown key {name!r}. Known names include: enter, tab, esc, "
            f"f1-f24, ctrl, alt, shift, win, arrows, home/end, pageup/pagedown, "
            f"num0-num9, and single characters. For arbitrary text use type()."
        )
    return KEYS[key]


class Keyboard:
    def __init__(self, profile: _h.Profile | None = None):
        self.profile = profile or _h.INSTANT
        self._actions = 0

    # -- raw key handling --------------------------------------------------- #
    def down(self, key: str):
        vk, ext = _resolve_key(key)
        flags = _w.KEYEVENTF_EXTENDEDKEY if ext else 0
        _send([_key_event(vk, flags)])

    def up(self, key: str):
        vk, ext = _resolve_key(key)
        flags = _w.KEYEVENTF_KEYUP | (_w.KEYEVENTF_EXTENDEDKEY if ext else 0)
        _send([_key_event(vk, flags)])

    def press(self, *keys: str, humanize=None):
        """Press keys as a chord: `press('ctrl', 'shift', 'n')`.

        Modifiers are held while the final key taps, then released in reverse -
        the order a real hand uses, and the order Windows accelerator handling
        expects.
        """
        p = self.profile if humanize is None else _h.resolve(humanize)
        if not keys:
            return
        *mods, final = keys
        evs_down = []
        for m in mods:
            vk, ext = _resolve_key(m)
            evs_down.append(_key_event(vk, _w.KEYEVENTF_EXTENDEDKEY if ext else 0))
        fvk, fext = _resolve_key(final)
        fflag = _w.KEYEVENTF_EXTENDEDKEY if fext else 0

        if p is _h.INSTANT:
            evs = list(evs_down)
            evs.append(_key_event(fvk, fflag))
            evs.append(_key_event(fvk, fflag | _w.KEYEVENTF_KEYUP))
            for m in reversed(mods):
                vk, ext = _resolve_key(m)
                evs.append(_key_event(vk, (_w.KEYEVENTF_EXTENDEDKEY if ext else 0)
                                      | _w.KEYEVENTF_KEYUP))
            _send(evs)
            return

        for ev in evs_down:
            _send([ev])
            _clock.sleep_ms(p._span((18.0, 45.0), self._actions))
        _send([_key_event(fvk, fflag)])
        _clock.sleep_ms(p._span(p.key_hold_ms, self._actions))
        _send([_key_event(fvk, fflag | _w.KEYEVENTF_KEYUP)])
        for m in reversed(mods):
            _clock.sleep_ms(p._span((15.0, 40.0), self._actions))
            vk, ext = _resolve_key(m)
            _send([_key_event(vk, (_w.KEYEVENTF_EXTENDEDKEY if ext else 0)
                              | _w.KEYEVENTF_KEYUP)])
        self._actions += 1

    def tap(self, key: str, *, count: int = 1, humanize=None):
        p = self.profile if humanize is None else _h.resolve(humanize)
        vk, ext = _resolve_key(key)
        flag = _w.KEYEVENTF_EXTENDEDKEY if ext else 0
        if p is _h.INSTANT:
            _send([ev for _ in range(count)
                   for ev in (_key_event(vk, flag),
                              _key_event(vk, flag | _w.KEYEVENTF_KEYUP))])
        else:
            for i in range(count):
                _send([_key_event(vk, flag)])
                _clock.sleep_ms(p._span(p.key_hold_ms, self._actions))
                _send([_key_event(vk, flag | _w.KEYEVENTF_KEYUP)])
                if i < count - 1:
                    _clock.sleep_ms(p._span(p.key_gap_ms, self._actions))
            self._actions += 1

    # -- text --------------------------------------------------------------- #
    @staticmethod
    def _unicode_events(ch: str) -> tuple[list[_w.INPUT], list[_w.INPUT]]:
        """Layout-independent character delivery.

        Codepoints outside the BMP (emoji, rare CJK) have no single UTF-16
        unit, so they go as surrogate pairs - two downs, then two ups.
        Returns (downs, ups) so down->hold->up pacing stays correct regardless
        of codepoint length.
        """
        downs, ups = [], []
        units = ch.encode("utf-16-le")
        for i in range(0, len(units), 2):
            code = units[i] | (units[i + 1] << 8)
            downs.append(_key_event(0, _w.KEYEVENTF_UNICODE, code))
            ups.append(_key_event(0, _w.KEYEVENTF_UNICODE | _w.KEYEVENTF_KEYUP, code))
        return downs, ups

    def type(self, text: str, *, humanize=None):
        """Type literal text. Layout-independent: a Turkish Q layout, a US
        layout and a Dvorak layout all receive exactly these characters.

        Measured constraint: sending multiple distinct Unicode characters in
        ONE SendInput batch causes the Windows Text Services Framework (TSF)
        to collapse or duplicate events on modern WinUI/XAML surfaces (observed
        on Windows 11 Notepad). Characters must therefore be dispatched in
        per-character SendInput calls. Even unbatched, SendInput takes ~26 us
        per call, which is ~38,000 characters per second - still sub-millisecond
        for any humanly perceptible string.
        """
        p = self.profile if humanize is None else _h.resolve(humanize)

        if p is _h.INSTANT:
            for ch in text:
                if ch == "\n":
                    vk, _ = KEYS["enter"]
                    _send([_key_event(vk, 0), _key_event(vk, _w.KEYEVENTF_KEYUP)])
                else:
                    downs, ups = self._unicode_events(ch)
                    _send(downs + ups)
                # Measured 5ms gap: Windows Text Services Framework (TSF) requires
                # >=4ms to reliably pump VK_PACKET Unicode characters without
                # event merging or dropping on modern WinUI/XAML surfaces.
                # At 5ms, speed is still 200 chars/second (sub-human latency).
                _clock.sleep_ms(5.0)
            return

        rng = p.rng
        for ch in text:
            if p.typo_rate and ch.isalnum() and rng.random() < p.typo_rate:
                wrong = rng.choice("abcdefghijklmnopqrstuvwxyz")
                w_down, w_up = self._unicode_events(wrong)
                _send(w_down + w_up)
                _clock.sleep_ms(p._span(p.key_gap_ms, self._actions))
                self.tap("backspace", humanize=p)
                _clock.sleep_ms(p._span((90.0, 220.0), self._actions))

            if ch == "\n":
                self.tap("enter", humanize=p)
            else:
                downs, ups = self._unicode_events(ch)
                _send(downs)
                _clock.sleep_ms(p._span(p.key_hold_ms, self._actions))
                _send(ups)
            gap = p._span(p.key_gap_ms, self._actions)
            if ch == " ":
                gap *= rng.uniform(1.1, 1.8)
            _clock.sleep_ms(gap)
        self._actions += 1

    # -- state -------------------------------------------------------------- #
    @staticmethod
    def is_down(key: str) -> bool:
        vk, _ = _resolve_key(key)
        return bool(_w.user32.GetAsyncKeyState(vk) & 0x8000)

    @staticmethod
    def is_toggled(key: str) -> bool:
        """For CapsLock / NumLock / ScrollLock, whose light is on or off."""
        vk, _ = _resolve_key(key)
        return bool(_w.user32.GetKeyState(vk) & 0x0001)

    @staticmethod
    def active_layout() -> str:
        """The foreground window's keyboard layout, as a hex language id.
        Worth surfacing: it is the single most common cause of 'my script types
        the wrong characters' in every other automation library."""
        hwnd = _w.user32.GetForegroundWindow()
        tid = _w.user32.GetWindowThreadProcessId(hwnd, None)
        hkl = _w.user32.GetKeyboardLayout(tid)
        return f"0x{hkl & 0xFFFF:04x}"
