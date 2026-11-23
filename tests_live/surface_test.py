"""Interactive surface torture test.

Spawns a Tk window that logs EVERY raw input event it receives (mouse buttons,
drag motion, wheel delta, modifier state, geometry changes), then drives it
through Basira and checks the receiver's log against what we sent.

Why this matters: `SendInput` returning 1 only proves the OS accepted the event
into the input queue. It does NOT prove the target application saw a double
click rather than two single clicks, or a drag rather than a teleport, or a
shift-click with Shift actually held at the moment of the button-down. Only the
receiving window can testify to that.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


HARNESS_CODE = r'''
import json, sys, tkinter as tk

log_path = sys.argv[1]
events = []

root = tk.Tk()
root.title("basira-surface")
root.attributes("-topmost", True)
root.geometry("520x420+140+140")
root.resizable(True, True)

canvas = tk.Canvas(root, width=500, height=340, bg="#181820",
                   highlightthickness=0)
canvas.pack(padx=10, pady=10)

status = tk.Label(root, text="ready", fg="#e0e0e0", bg="#181820",
                  font=("Consolas", 10))
status.pack()

# A square button whose colour flips when clicked, so wait_until_still and
# wait_for_color have a real state transition to watch.
btn_box = canvas.create_rectangle(380, 20, 480, 90, fill="#2244aa", outline="")
drag_pts = []

def rec(kind, **d):
    events.append({"kind": kind, **d})
    status.config(text=f"{kind} {d}")

def mods(ev):
    s = []
    if ev.state & 0x0001: s.append("shift")
    if ev.state & 0x0004: s.append("ctrl")
    if ev.state & 0x20000: s.append("alt")
    return s

canvas.bind("<Button-1>", lambda e: (
    rec("left_down", x=e.x, y=e.y, mods=mods(e)),
    canvas.itemconfig(btn_box, fill="#00ee55")
    if 380 <= e.x <= 480 and 20 <= e.y <= 90 else None
))
canvas.bind("<ButtonRelease-1>", lambda e: rec("left_up", x=e.x, y=e.y,
                                               drag_samples=len(drag_pts)))
canvas.bind("<Double-Button-1>", lambda e: rec("double_left", x=e.x, y=e.y))
canvas.bind("<Button-2>", lambda e: rec("middle_down", x=e.x, y=e.y))
canvas.bind("<Button-3>", lambda e: rec("right_down", x=e.x, y=e.y))
canvas.bind("<B1-Motion>", lambda e: (
    drag_pts.append((e.x, e.y)),
    canvas.create_oval(e.x-2, e.y-2, e.x+2, e.y+2, fill="#ff8800", outline="")
))
canvas.bind("<MouseWheel>", lambda e: rec("wheel", delta=e.delta))

def dump_and_quit(*_):
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump({"events": events, "drag_pts": drag_pts}, f)
    root.destroy()

root.bind("<Key-q>", dump_and_quit)
root.protocol("WM_DELETE_WINDOW", dump_and_quit)
root.mainloop()
'''


def main():
    from basira import CAREFUL, FAST, NATURAL, Point, Region, auto

    PASS, FAIL = [], []
    def chk(name, ok, detail=""):
        (PASS if ok else FAIL).append(name)
        print(f"   [{'PASS' if ok else 'FAIL'}] {name:<50} {detail}")

    print("=" * 100)
    print("SURFACE TEST: does the receiving window see what we think we sent?")
    print("=" * 100)

    origin = auto.cursor
    fd, log_path = tempfile.mkstemp(suffix=".json", prefix="basira_surf_")
    os.close(fd)
    fd2, script_path = tempfile.mkstemp(suffix=".py", prefix="basira_surf_")
    with os.fdopen(fd2, "w", encoding="utf-8") as f:
        f.write(HARNESS_CODE)

    proc = subprocess.Popen([sys.executable, script_path, log_path])
    try:
        win = auto.wait_window("basira-surface", timeout=12)
        chk("harness window appeared", win is not None, repr(win))
        if not win:
            return 1
        win.activate(timeout=3)
        auto.wait(0.4)
        c = win.client_rect
        # canvas sits at (10,10) inside client
        cx, cy = c.x + 10, c.y + 10

        # 1. single left click
        auto.click(cx + 60, cy + 60)
        auto.wait(0.1)

        # 2. right click
        auto.right_click(cx + 120, cy + 60)
        auto.wait(0.1)

        # 3. middle click
        auto.click(cx + 180, cy + 60, button="middle")
        auto.wait(0.1)

        # 4. double click (instant) AND double click (humanized)
        auto.double_click(cx + 240, cy + 60)
        auto.wait(0.15)
        auto.double_click(cx + 300, cy + 60, humanize=FAST)
        auto.wait(0.15)

        # 5. modifier chord: Shift+Click and Ctrl+Click via auto.hold()
        with auto.hold("shift"):
            auto.click(cx + 60, cy + 130)
        auto.wait(0.1)
        with auto.hold("ctrl"):
            auto.click(cx + 120, cy + 130)
        auto.wait(0.1)

        # 6. drag in instant mode: must still deliver intermediate B1-Motion
        #    samples, or UIs that gate drag-start on motion ignore it
        auto.drag((cx + 40, cy + 200), (cx + 260, cy + 260))
        auto.wait(0.15)
        # Verify the orange trail (#ff8800) actually appeared on the canvas
        trail_frame = auto.grab(Region(cx, cy + 180, 300, 110))
        orange_px = trail_frame.count("#ff8800")
        chk("drag drew pixels on the receiving canvas", orange_px > 10,
            f"{orange_px} orange px")

        # 7. humanized drag: curved trajectory, more samples
        before_orange = orange_px
        auto.drag((cx + 40, cy + 280), (cx + 320, cy + 310), humanize=NATURAL)
        auto.wait(0.15)
        after_orange = auto.grab(Region(cx, cy + 180, 360, 150)).count("#ff8800")
        chk("humanized drag drew additional trail", after_orange > before_orange + 20,
            f"{before_orange} -> {after_orange} px")

        # 8. scroll wheel: +3 then -2
        auto.scroll(3, (cx + 200, cy + 150))
        auto.wait(0.1)
        auto.scroll(-2)
        auto.wait(0.1)

        # 9. click the blue button (#2244aa) and wait_for_color for it to turn
        #    green (#00ee55) - tests locate() + click() + wait_for_color()
        btn_region = Region(cx + 370, cy + 10, 120, 90)
        blue_pt = auto.locate("#2244aa", btn_region)
        chk("locate() found the blue button", blue_pt is not None, str(blue_pt))
        if blue_pt:
            auto.click(blue_pt)
            green_pt = auto.wait_for_color("#00ee55", btn_region, timeout=2.0)
            chk("button flipped to green after click", green_pt is not None,
                str(green_pt))

        # 10. window move + resize + restore
        r0 = win.rect
        win.move(x=r0.x + 30, y=r0.y + 20, width=r0.width + 40, height=r0.height)
        auto.wait(0.15)
        r1 = win.rect
        chk("window.move() relocated and resized",
            abs(r1.x - (r0.x + 30)) <= 2 and abs(r1.width - (r0.width + 40)) <= 2,
            f"{tuple(r0)} -> {tuple(r1)}")

        # 11. wait_until_still on a static region returns True promptly
        t0 = time.perf_counter()
        still = auto.wait_until_still(Region(cx, cy, 120, 120), timeout=2.0)
        still_ms = (time.perf_counter() - t0) * 1000
        chk("wait_until_still detects static region", still and still_ms < 500,
            f"{still} in {still_ms:.0f} ms")

        # Ask the harness to write its event log and exit cleanly
        win.activate()
        auto.wait(0.05)
        auto.tap("q")
        proc.wait(timeout=5)
    finally:
        auto.mouse.move(origin.x, origin.y)
        if proc.poll() is None:
            proc.kill()

    # --- now read what the window actually recorded ---------------------- #
    with open(log_path, encoding="utf-8") as f:
        data = json.load(f)
    evs = data["events"]
    drag_pts = data["drag_pts"]
    os.remove(log_path)
    os.remove(script_path)

    kinds = [e["kind"] for e in evs]
    print(f"\n   receiver logged {len(evs)} events, {len(drag_pts)} drag motion points")
    print(f"   event kinds: {kinds}")

    chk("receiver saw left_down + left_up",
        "left_down" in kinds and "left_up" in kinds)
    chk("receiver saw right_down", "right_down" in kinds)
    chk("receiver saw middle_down", "middle_down" in kinds)
    dbl = [e for e in evs if e["kind"] == "double_left"]
    chk("receiver recognised double-click (both modes)", len(dbl) >= 2,
        f"{len(dbl)} double-clicks")
    shift_clicks = [e for e in evs if e["kind"] == "left_down" and "shift" in e.get("mods", [])]
    ctrl_clicks = [e for e in evs if e["kind"] == "left_down" and "ctrl" in e.get("mods", [])]
    chk("hold('shift') held Shift during click", len(shift_clicks) >= 1, str(shift_clicks))
    chk("hold('ctrl') held Ctrl during click", len(ctrl_clicks) >= 1, str(ctrl_clicks))
    chk("drag delivered intermediate motion events", len(drag_pts) >= 12,
        f"{len(drag_pts)} B1-Motion points")
    wheels = [e["delta"] for e in evs if e["kind"] == "wheel"]
    chk("scroll delivered both + and - wheel deltas",
        any(d > 0 for d in wheels) and any(d < 0 for d in wheels),
        f"deltas={wheels}")

    print(f"\n   passed {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("   FAILURES:", FAIL)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
