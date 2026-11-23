"""Live multi-modal Duck Hunt bot: pixels + YOLOE vision + real-time OCR.

Launches `duck_hunt.py` and clears all 5 stages without a single hardcoded
target coordinate:

  Stage 1 : Pixel layer (`auto.locate('#ff1a1a', arena)`) shoots 6 red squares.
  Stage 2 : YOLOE vision (`auto.click('duck', within=arena)`) shoots 4 ducks.
  Stage 3 : OCR (`auto.read(arena).alphabetical(single_chars_only=True)`) reads
            all scrambled letters in ONE pass and clicks them A -> Z.
  Stage 4 : OCR locates each letter, moves the mouse over its box (`auto.move`),
            and presses that letter on the keyboard (`auto.tap`).
  Stage 5 : OCR drag-selects & copies `'TOKEN-7391'` from a live Entry widget
            (`auto.select_text(..., action='copy')`) and clicks the Turkish
            `'Onayla'` button (`auto.click_text('Onayla')`).

Followed by a live multilingual OCR verification card (Turkish, English,
Chinese/Japanese, Cyrillic) rendered on screen and read back in real time.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from basira import Region, auto, clipboard
from basira.vision import _encoder_cache_dir

GAME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "duck_hunt.py")
RED = "#ff1a1a"

PASS, FAIL = [], []


def chk(name: str, ok: bool, detail: str = ""):
    (PASS if ok else FAIL).append(name)
    print(f"   [{'PASS' if ok else 'FAIL'}] {name:<54} {detail}")


def head(title: str):
    print("\n" + "=" * 106)
    print(title)
    print("=" * 106)


def ensure_duck_sprite() -> str:
    path = _encoder_cache_dir().parent / "testdata" / "duck_target.png"
    if path.exists():
        return str(path)
    import urllib.request
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    url = "https://upload.wikimedia.org/wikipedia/commons/b/bf/Bucephala-albeola-010.jpg"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        path.write_bytes(r.read())
    im = Image.open(path).convert("RGB")
    im.thumbnail((220, 165))
    im.save(path)
    return str(path)


def run_gallery():
    head("1. MULTI-MODAL DUCK HUNT GALLERY (5 stages: Pixel -> YOLOE -> OCR Alpha -> OCR Hover+Key -> OCR Select)")
    duck_png = ensure_duck_sprite()
    fd, report_path = tempfile.mkstemp(suffix=".json", prefix="basira_duck_")
    os.close(fd)

    # Warm up YOLOE and OCR before starting the game clock so model load time is
    # reported separately from per-target reaction latency.
    t0 = time.perf_counter()
    f0 = auto.grab((0, 0, 320, 200))
    auto.vision.detect(f0, ["duck"])
    auto.ocr.read(f0)
    print(f"   models warmed up in {(time.perf_counter() - t0)*1000:.0f} ms")

    origin_cursor = auto.cursor
    proc = subprocess.Popen([sys.executable, GAME, duck_png, report_path])
    copied_token = ""

    try:
        win = auto.wait_window("basira-duck-hunt", timeout=12)
        chk("gallery window launched", win is not None, repr(win))
        if not win:
            return
        win.activate(timeout=3)
        auto.wait(0.35)
        arena = win.client_rect

        # ------------------------------------------------------------------ #
        # STAGE 1: 6 Red Squares via Pixel Layer
        # ------------------------------------------------------------------ #
        s1_times = []
        for i in range(6):
            t0 = time.perf_counter()
            pt = auto.wait_for_color(RED, arena, timeout=2.0)
            if pt is None:
                break
            auto.click(pt)
            s1_times.append((time.perf_counter() - t0) * 1000)
            auto.wait(0.05)
        s1_times.sort()
        print(f"   Stage 1 (Red Squares) : {len(s1_times)}/6 shot | "
              f"p50={s1_times[len(s1_times)//2]:.1f} ms")

        # ------------------------------------------------------------------ #
        # STAGE 2: 4 Real Ducks via YOLOE Open-Vocabulary Vision
        # ------------------------------------------------------------------ #
        auto.wait(0.15)
        s2_times = []
        for i in range(4):
            t0 = time.perf_counter()
            det = auto.see("duck", arena, confidence=0.2, fresh=True)
            if det is None:
                det = auto.see("bird", arena, confidence=0.2, fresh=True)
            if det is None:
                break
            auto.click(det)
            s2_times.append((time.perf_counter() - t0) * 1000)
            auto.wait(0.12)
        s2_times.sort()
        print(f"   Stage 2 (YOLOE Ducks) : {len(s2_times)}/4 shot | "
              f"p50={s2_times[len(s2_times)//2]:.1f} ms")

        # ------------------------------------------------------------------ #
        # STAGE 3: Scrambled Letters Clicked in Alphabetical Order (A -> Z)
        # ------------------------------------------------------------------ #
        auto.wait(0.2)
        t0 = time.perf_counter()
        spans = auto.read(arena, fresh=True)
        ocr_s3_ms = (time.perf_counter() - t0) * 1000
        ordered = spans.alphabetical(single_chars_only=True)
        letters_seen = [h.text.strip().upper() for h in ordered]
        print(f"   Stage 3 (OCR Alpha)   : read {letters_seen} in ONE {ocr_s3_ms:.1f} ms pass")
        for h in ordered:
            auto.click(h)
            auto.wait(0.06)

        # ------------------------------------------------------------------ #
        # STAGE 4: As Each Letter Appears, Hover Over It + Press Its Key
        # ------------------------------------------------------------------ #
        auto.wait(0.15)
        win.activate()
        s4_times = []
        s4_letters = []
        for i in range(5):
            t0 = time.perf_counter()
            spans4 = auto.read(arena, fresh=True)
            targets4 = spans4.alphabetical(single_chars_only=True)
            if not targets4:
                break
            h = targets4[0]
            ch = h.text.strip()
            auto.move(h.center)
            auto.wait(0.04)
            auto.tap(ch.lower())
            s4_times.append((time.perf_counter() - t0) * 1000)
            s4_letters.append(ch.upper())
            auto.wait(0.08)
        s4_times.sort()
        print(f"   Stage 4 (Hover + Key) : {len(s4_letters)}/5 hit {s4_letters} | "
              f"p50={s4_times[len(s4_times)//2] if s4_times else 0:.1f} ms")

        # ------------------------------------------------------------------ #
        # STAGE 5: Select & Copy Token via OCR Drag + Click Turkish 'Onayla'
        # ------------------------------------------------------------------ #
        auto.wait(0.25)
        clipboard.clear()
        copied_token = str(auto.select_text("TOKEN-7391", arena,
                                            action="copy", fresh=True)).strip()
        print(f"   Stage 5 (Select+Copy) : copied={copied_token!r}")
        auto.wait(0.1)
        hit_btn = auto.click_text("Onayla", arena, fresh=True)
        print(f"   Stage 5 (Click Text)  : clicked {hit_btn!r}")

        proc.wait(timeout=5)
    finally:
        auto.mouse.move(origin_cursor.x, origin_cursor.y)
        if proc.poll() is None:
            proc.kill()

    with open(report_path, encoding="utf-8") as f:
        sc = json.load(f)
    os.remove(report_path)

    print(f"\n   game scorecard: {sc}")
    chk("Stage 1: all 6 red squares shot via pixel layer",
        sc["stage1_red_hits"] == 6, f"{sc['stage1_red_hits']}/6")
    chk("Stage 2: all 4 ducks shot via YOLOE open-vocabulary",
        sc["stage2_duck_hits"] == 4, f"{sc['stage2_duck_hits']}/4")
    chk("Stage 3: all 6 letters clicked in alphabetical order",
        sc["stage3_alpha_hits"] == 6
        and sc["stage3_click_sequence"] == ["A", "B", "E", "K", "M", "T"],
        f"seq={sc['stage3_click_sequence']}")
    chk("Stage 3: zero alphabetical order errors",
        sc["stage3_order_errors"] == 0, f"errors={sc['stage3_order_errors']}")
    chk("Stage 4: all 5 letters eliminated via hover + keypress",
        sc["stage4_hover_hits"] == 5,
        f"seq={sc['stage4_key_sequence']} wrong={sc['stage4_wrong_keys']}")
    chk("Stage 5: OCR drag-selected and copied TOKEN-7391",
        "TOKEN-7391" in copied_token, repr(copied_token))
    chk("Stage 5: OCR clicked Turkish 'Onayla' button & finished",
        sc["stage5_button_clicked"] and sc["completed"])


def run_multilingual_benchmark():
    head("2. MULTILINGUAL OCR & LATENCY BENCHMARK (Turkish, English, CJK, Substring Slicing)")
    import tkinter as tk

    root = tk.Tk()
    root.title("basira-multilingual-ocr")
    root.attributes("-topmost", True)
    root.geometry("620x300+140+140")
    c = tk.Canvas(root, width=620, height=300, bg="#161a22", highlightthickness=0)
    c.pack()

    # Render Turkish with dotted/dotless I, English toolbar, and Chinese/Japanese
    c.create_text(310, 55, text="İptal   Kaydet   Çağrı",
                  fill="#ffffff", font=("Arial", 24, "bold"))
    c.create_text(310, 140, text="File   Edit   View   Help",
                  fill="#70e0a0", font=("Arial", 22, "bold"))
    c.create_text(310, 225, text="確定   取消   東京",
                  fill="#ffd27f", font=("Microsoft YaHei", 24, "bold"))
    root.update()
    time.sleep(0.35)
    root.update()

    try:
        win = auto.find("basira-multilingual-ocr")
        area = win.client_rect

        # Measure warm latency on the live window
        times = []
        for _ in range(10):
            f = auto.grab(area)
            t0 = time.perf_counter()
            ts = auto.ocr.read(f)
            times.append((time.perf_counter() - t0) * 1000)
        times.sort()
        print(f"   10 live window OCR passes : p50={times[5]:.1f} ms  "
              f"min={times[0]:.1f} ms  max={times[-1]:.1f} ms")
        print(f"   recognised lines          : {ts.texts()}")

        # Cache test: 26 queries on the same frame
        f = auto.grab(area)
        auto.ocr.read(f)
        t0 = time.perf_counter()
        for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            auto.ocr.read(f).best(ch)
        cache_26_ms = (time.perf_counter() - t0) * 1000
        print(f"   26 letter queries (cached): {cache_26_ms:.3f} ms total "
              f"({cache_26_ms/26*1000:.1f} us/query)")

        chk("live OCR latency under 25 ms", times[5] < 25.0,
            f"p50={times[5]:.1f} ms (min {times[0]:.1f} ms)")
        chk("26 cached queries under 1 ms total", cache_26_ms < 1.0,
            f"{cache_26_ms:.3f} ms")
        chk("Turkish dotted-I fold: 'iptal' matches 'İptal'",
            ts.best("iptal") is not None, repr(ts.best("iptal")))
        chk("Turkish diacritics: 'çağrı' matches 'Çağrı'",
            ts.best("çağrı") is not None, repr(ts.best("çağrı")))
        chk("CJK recognition: finds '確定'",
            ts.best("確定") is not None, repr(ts.best("確定")))

        # Substring slicing check: 'File' and 'Help' inside 'File Edit View Help'
        # must resolve to distinct horizontal coordinates (left vs right)
        h_file = ts.best("File")
        h_help = ts.best("Help")
        chk("proportional substring slicing separates words in a line",
            h_file is not None and h_help is not None
            and h_file.center.x + 100 < h_help.center.x,
            f"File.x={h_file.center.x if h_file else None} "
            f"Help.x={h_help.center.x if h_help else None}")
    finally:
        root.destroy()


def main():
    run_gallery()
    run_multilingual_benchmark()

    head("FINAL SUMMARY")
    print(f"   passed {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("   FAILURES:", FAIL)
    print(f"   ocr status   : {auto.ocr.status()}")
    print(f"   vision status: {auto.vision.status()}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
