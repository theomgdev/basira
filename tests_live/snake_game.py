"""A deliberately minimal Snake, built as a test target for Basira.

Design constraints that make it a useful target rather than just a game:
  * Flat, saturated, unique colours per entity, so the pixel layer can find
    things with tolerance=0 and no ambiguity.
  * A fixed grid whose geometry the bot can derive from the window rect alone.
  * Score rendered as a colour-coded bar AND a window title, so a bot can read
    progress two independent ways and we can check they agree.
  * Arrow-key control only - the bot must drive it through real SendInput.

Run standalone to play it yourself:  python snake_game.py
"""
from __future__ import annotations

import random
import sys
import tkinter as tk

CELL = 24
COLS, ROWS = 20, 16
TICK_MS = int(__import__("os").environ.get("SNAKE_TICK_MS", 110))

BG = "#101014"
SNAKE_HEAD = "#00ff00"   # pure green, nothing else on screen is this
SNAKE_BODY = "#008f26"
FOOD = "#ff0000"         # pure red
WALL = "#3c3c50"
BAR = "#00bfff"

TITLE = "basira-snake"


class Snake:
    def __init__(self, root: tk.Tk, *, seed: int | None = None):
        self.root = root
        self.rng = random.Random(seed)
        self.w = COLS * CELL
        self.h = ROWS * CELL
        self.canvas = tk.Canvas(root, width=self.w, height=self.h + 16,
                                bg=BG, highlightthickness=0, borderwidth=0)
        self.canvas.pack()
        self.reset()
        root.bind("<Key>", self.on_key)
        self.running = True
        self.root.after(TICK_MS, self.tick)

    # -- state -------------------------------------------------------------- #
    def reset(self):
        mid = (COLS // 2, ROWS // 2)
        self.body = [mid, (mid[0] - 1, mid[1]), (mid[0] - 2, mid[1])]
        self.dir = (1, 0)
        self.pending = (1, 0)
        self.score = 0
        self.alive = True
        self.ticks = 0
        self.place_food()

    def place_food(self):
        free = [(x, y) for x in range(COLS) for y in range(ROWS)
                if (x, y) not in self.body]
        self.food = self.rng.choice(free) if free else None

    # -- input -------------------------------------------------------------- #
    def on_key(self, ev):
        k = ev.keysym.lower()
        turn = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
        if k in turn:
            nd = turn[k]
            # reject a 180-degree reversal, as every snake game does
            if (nd[0] + self.dir[0], nd[1] + self.dir[1]) != (0, 0):
                self.pending = nd
        elif k == "r":
            self.reset()
        elif k == "q":
            self.running = False
            self.root.destroy()

    # -- loop --------------------------------------------------------------- #
    def tick(self):
        if not self.running:
            return
        if self.alive:
            self.step()
        self.draw()
        self.root.title(f"{TITLE} | score {self.score} | "
                        f"{'alive' if self.alive else 'dead'}")
        self.root.after(TICK_MS, self.tick)

    def step(self):
        self.ticks += 1
        self.dir = self.pending
        hx, hy = self.body[0]
        nx, ny = hx + self.dir[0], hy + self.dir[1]

        if not (0 <= nx < COLS and 0 <= ny < ROWS) or (nx, ny) in self.body[:-1]:
            self.alive = False
            return

        self.body.insert(0, (nx, ny))
        if self.food and (nx, ny) == self.food:
            self.score += 1
            self.place_food()
        else:
            self.body.pop()

    # -- render ------------------------------------------------------------- #
    def draw(self):
        c = self.canvas
        c.delete("all")
        for i, (x, y) in enumerate(self.body):
            colour = SNAKE_HEAD if i == 0 else SNAKE_BODY
            c.create_rectangle(x * CELL, y * CELL, x * CELL + CELL, y * CELL + CELL,
                               fill=colour, outline="")
        if self.food:
            fx, fy = self.food
            c.create_rectangle(fx * CELL + 3, fy * CELL + 3,
                               fx * CELL + CELL - 3, fy * CELL + CELL - 3,
                               fill=FOOD, outline="")
        # score bar: 8px blocks, wrapped at 50 per row so a high score does not
        # run off the 480px canvas (the bot hit 57 and clipped the old 13px bar)
        for i in range(self.score):
            row, col = divmod(i, 50)
            c.create_rectangle(col * 9, self.h + 2 + row * 7,
                               col * 9 + 7, self.h + 7 + row * 7,
                               fill=BAR, outline="")
        if not self.alive:
            c.create_rectangle(0, 0, self.w, self.h, fill="", outline=WALL, width=6)


def main(seed: int | None = None, topmost: bool = True):
    root = tk.Tk()
    root.title(TITLE)
    root.resizable(False, False)
    if topmost:
        root.attributes("-topmost", True)
    root.geometry(f"+120+120")
    Snake(root, seed=seed)
    root.mainloop()


if __name__ == "__main__":
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else None
    main(seed)
