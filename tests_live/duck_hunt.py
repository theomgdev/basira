"""Basira Duck Hunt & Multi-Modal Shooting Gallery.

A 5-stage live shooting gallery that exercises every perception + action mode
in Basira inside a single game window:

  Stage 1 (RED_SQUARES) : red squares (#ff1a1a) spawn across the arena -> click
                          each one (pixel layer).
  Stage 2 (YOLO_DUCKS)  : a real mallard duck photo teleports across the arena
                          -> find and shoot it with open-vocabulary YOLOE
                          (`auto.click("duck", within=arena)`).
  Stage 3 (ALPHA_CLICK) : scrambled letters spawn simultaneously -> must be
                          clicked in strict ALPHABETICAL order (A -> Z). Clicking
                          out of order increments `order_errors`.
  Stage 4 (HOVER_KEY)   : letters spawn across the arena -> clicking does
                          nothing; you must HOVER the mouse cursor over the
                          letter's box and PRESS that letter on the keyboard.
  Stage 5 (TEXT_OPS)    : an editable text box holds a secret token alongside a
                          Turkish confirmation button ('Onayla') -> select & copy
                          the token via OCR drag-selection, then click 'Onayla'.

When all stages complete (or on `Escape`), writes a ground-truth JSON scorecard
if `--report <path>` was passed.
"""
from __future__ import annotations

import json
import os
import random
import sys
import tkinter as tk

W, H = 820, 520
BG = "#141820"
RED_TARGET = "#ff1a1a"
CARD_BG = "#232a36"
CARD_BORDER = "#3d4a5d"


class DuckHuntGallery:
    def __init__(self, root: tk.Tk, duck_img_path: str,
                 report_path: str | None = None, seed: int = 42):
        self.root = root
        self.rng = random.Random(seed)
        self.report_path = report_path
        self.duck_photo = tk.PhotoImage(file=duck_img_path)
        self.dw = self.duck_photo.width()
        self.dh = self.duck_photo.height()

        self.canvas = tk.Canvas(root, width=W, height=H, bg=BG,
                                highlightthickness=0, borderwidth=0)
        self.canvas.pack()

        self.stage = 1
        self.mouse_xy = (0, 0)
        self.targets: list[dict] = []
        self.entry_widget: tk.Entry | None = None
        self.entry_win_id: int | None = None

        self.scorecard = {
            "stage1_red_hits": 0,
            "stage1_red_total": 6,
            "stage2_duck_hits": 0,
            "stage2_duck_total": 4,
            "stage3_alpha_hits": 0,
            "stage3_alpha_total": 6,
            "stage3_order_errors": 0,
            "stage3_click_sequence": [],
            "stage4_hover_hits": 0,
            "stage4_hover_total": 5,
            "stage4_wrong_keys": 0,
            "stage4_key_sequence": [],
            "stage5_button_clicked": False,
            "completed": False,
        }

        self.canvas.bind("<Button-1>", self.on_click)
        self.canvas.bind("<Motion>", self.on_motion)
        root.bind("<Motion>", self.on_root_motion)
        root.bind("<Key>", self.on_key)
        root.protocol("WM_DELETE_WINDOW", self.finish)

        self.start_stage(1)

    # -- stage setup -------------------------------------------------------- #
    def start_stage(self, n: int):
        self.stage = n
        self.targets.clear()
        self.canvas.delete("all")
        if self.entry_widget is not None:
            self.entry_widget.destroy()
            self.entry_widget = None

        if n == 1:
            self._spawn_next_red()
        elif n == 2:
            self._spawn_next_duck()
        elif n == 3:
            self._spawn_alpha_board()
        elif n == 4:
            self._spawn_hover_board()
        elif n == 5:
            self._spawn_text_ops()
        else:
            self.scorecard["completed"] = True
            self.update_title("DONE")
            self.root.after(150, self.finish)
            return
        self.update_title()

    def update_title(self, extra: str = ""):
        t = (f"basira-duck-hunt | stage={self.stage} "
             f"s1={self.scorecard['stage1_red_hits']} "
             f"s2={self.scorecard['stage2_duck_hits']} "
             f"s3={self.scorecard['stage3_alpha_hits']} "
             f"s4={self.scorecard['stage4_hover_hits']} "
             f"s5={int(self.scorecard['stage5_button_clicked'])} {extra}").strip()
        self.root.title(t)

    # -- stage 1: red squares ----------------------------------------------- #
    def _spawn_next_red(self):
        self.canvas.delete("all")
        self.targets.clear()
        idx = self.scorecard["stage1_red_hits"]
        positions = [(110, 95), (590, 120), (340, 290),
                     (150, 370), (640, 350), (410, 140)]
        x, y = positions[idx % len(positions)]
        sz = 46
        self.canvas.create_rectangle(x, y, x + sz, y + sz,
                                     fill=RED_TARGET, outline="")
        self.targets.append({"x0": x, "y0": y, "x1": x + sz, "y1": y + sz})

    # -- stage 2: real duck sprite (YOLOE) ---------------------------------- #
    def _spawn_next_duck(self):
        self.canvas.delete("all")
        self.targets.clear()
        idx = self.scorecard["stage2_duck_hits"]
        positions = [(80, 70), (510, 80), (120, 290), (490, 280)]
        x, y = positions[idx % len(positions)]
        self.canvas.create_image(x, y, image=self.duck_photo, anchor="nw")
        self.targets.append({"x0": x, "y0": y,
                             "x1": x + self.dw, "y1": y + self.dh})

    # -- stage 3: alphabetical letters -------------------------------------- #
    def _spawn_alpha_board(self):
        self.canvas.delete("all")
        self.targets.clear()
        # Six distinct letters placed at well-separated coordinates
        spec = [
            ("M", 120, 110),
            ("B", 410, 110),
            ("T", 680, 110),
            ("A", 160, 340),
            ("K", 430, 340),
            ("E", 660, 340),
        ]
        for ch, cx, cy in spec:
            bw, bh = 74, 74
            x0, y0, x1, y1 = cx - bw // 2, cy - bh // 2, cx + bw // 2, cy + bh // 2
            rect_id = self.canvas.create_rectangle(
                x0, y0, x1, y1, fill=CARD_BG, outline="")
            txt_id = self.canvas.create_text(
                cx, cy, text=ch, fill="#ffffff", font=("Arial", 32, "bold"))
            self.targets.append({
                "char": ch, "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                "ids": (rect_id, txt_id),
            })

    # -- stage 4: hover + keypress (letters appear one by one!) ------------- #
    def _spawn_hover_board(self):
        self.canvas.delete("all")
        self.targets.clear()
        spec = [
            ("R", 140, 130),
            ("N", 650, 140),
            ("L", 390, 260),
            ("S", 180, 390),
            ("H", 620, 380),
        ]
        idx = self.scorecard["stage4_hover_hits"]
        ch, cx, cy = spec[idx % len(spec)]
        bw, bh = 82, 82
        x0, y0, x1, y1 = cx - bw // 2, cy - bh // 2, cx + bw // 2, cy + bh // 2
        rect_id = self.canvas.create_rectangle(
            x0, y0, x1, y1, fill="#1f2d24", outline="")
        txt_id = self.canvas.create_text(
            cx, cy, text=ch, fill="#80ffaa", font=("Arial", 34, "bold"))
        self.targets.append({
            "char": ch, "x0": x0, "y0": y0, "x1": x1, "y1": y1,
            "ids": (rect_id, txt_id),
        })

    # -- stage 5: text select/copy + multilingual button -------------------- #
    def _spawn_text_ops(self):
        self.canvas.delete("all")
        self.targets.clear()

        self.entry_widget = tk.Entry(
            self.root, font=("Consolas", 22, "bold"),
            fg="#111111", bg="#f5f5f5", width=14, justify="center",
            borderwidth=2, relief="flat"
        )
        self.entry_widget.insert(0, "TOKEN-7391")
        self.entry_win_id = self.canvas.create_window(
            260, 230, window=self.entry_widget, width=240, height=48)

        # Turkish confirmation button ('Onayla')
        bx0, by0, bx1, by1 = 480, 200, 660, 264
        self.canvas.create_rectangle(bx0, by0, bx1, by1,
                                     fill="#1e5cb3", outline="#4a8df0", width=2)
        self.canvas.create_text((bx0 + bx1) // 2, (by0 + by1) // 2,
                                text="Onayla", fill="#ffffff",
                                font=("Arial", 22, "bold"))
        self.targets.append({"label": "Onayla", "x0": bx0, "y0": by0,
                             "x1": bx1, "y1": by1})

    # -- event handlers ----------------------------------------------------- #
    def on_motion(self, ev):
        self.mouse_xy = (ev.x, ev.y)

    def on_root_motion(self, ev):
        # keep canvas-local mouse position up to date even when moving fast
        cx = self.root.winfo_pointerx() - self.canvas.winfo_rootx()
        cy = self.root.winfo_pointery() - self.canvas.winfo_rooty()
        self.mouse_xy = (cx, cy)

    def on_click(self, ev):
        x, y = ev.x, ev.y
        if self.stage == 1 and self.targets:
            t = self.targets[0]
            if t["x0"] <= x <= t["x1"] and t["y0"] <= y <= t["y1"]:
                self.scorecard["stage1_red_hits"] += 1
                if self.scorecard["stage1_red_hits"] >= self.scorecard["stage1_red_total"]:
                    self.start_stage(2)
                else:
                    self._spawn_next_red()
                    self.update_title()

        elif self.stage == 2 and self.targets:
            t = self.targets[0]
            if t["x0"] <= x <= t["x1"] and t["y0"] <= y <= t["y1"]:
                self.scorecard["stage2_duck_hits"] += 1
                if self.scorecard["stage2_duck_hits"] >= self.scorecard["stage2_duck_total"]:
                    self.start_stage(3)
                else:
                    self._spawn_next_duck()
                    self.update_title()

        elif self.stage == 3 and self.targets:
            expected = min(t["char"] for t in self.targets)
            for t in list(self.targets):
                if t["x0"] <= x <= t["x1"] and t["y0"] <= y <= t["y1"]:
                    if t["char"] == expected:
                        for cid in t["ids"]:
                            self.canvas.delete(cid)
                        self.targets.remove(t)
                        self.scorecard["stage3_alpha_hits"] += 1
                        self.scorecard["stage3_click_sequence"].append(t["char"])
                        if not self.targets:
                            self.start_stage(4)
                        else:
                            self.update_title()
                    else:
                        self.scorecard["stage3_order_errors"] += 1
                    break

        elif self.stage == 5 and self.targets:
            t = self.targets[0]
            if t["x0"] <= x <= t["x1"] and t["y0"] <= y <= t["y1"]:
                self.scorecard["stage5_button_clicked"] = True
                self.start_stage(6)

    def on_key(self, ev):
        ks = ev.keysym.lower()
        if ks == "escape":
            self.finish()
            return
        if self.stage == 4 and self.targets and len(ks) == 1:
            # Query live OS pointer position directly so synthetic SendInput
            # moves that did not trigger a Tk <Motion> event still register.
            mx = self.root.winfo_pointerx() - self.canvas.winfo_rootx()
            my = self.root.winfo_pointery() - self.canvas.winfo_rooty()
            matched = False
            for t in list(self.targets):
                if t["x0"] <= mx <= t["x1"] and t["y0"] <= my <= t["y1"]:
                    if t["char"].lower() == ks:
                        self.scorecard["stage4_hover_hits"] += 1
                        self.scorecard["stage4_key_sequence"].append(t["char"])
                        matched = True
                        if self.scorecard["stage4_hover_hits"] >= self.scorecard["stage4_hover_total"]:
                            self.start_stage(5)
                        else:
                            self._spawn_hover_board()
                            self.update_title()
                    break
            if not matched:
                self.scorecard["stage4_wrong_keys"] += 1

    def finish(self):
        if self.report_path:
            with open(self.report_path, "w", encoding="utf-8") as f:
                json.dump(self.scorecard, f, indent=2)
        self.root.destroy()


def main():
    duck_img = sys.argv[1]
    report = sys.argv[2] if len(sys.argv) > 2 else None
    root = tk.Tk()
    root.title("basira-duck-hunt | stage=1")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    root.geometry(f"{W}x{H}+110+110")
    DuckHuntGallery(root, duck_img, report)
    root.mainloop()


if __name__ == "__main__":
    main()
