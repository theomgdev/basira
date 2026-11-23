"""A Basira bot that plays Snake, as a closed-loop test of the whole library.

Why this is the useful test: every other check in this repo measures one
primitive in isolation. A game forces the real shape of automation - read the
screen, decide, press a key, and do it again before the world moves on. If the
frame cache, the pixel search, the key injection or the window geometry is
wrong by even a little, the snake dies and the score says so.

What it exercises end to end:
  * window discovery + readiness gating + activation
  * frame-coherent screen reads inside a tight loop
  * exact-colour pixel search (head, body, food) with tolerance=0
  * centroid vs find semantics on multi-pixel blocks
  * real arrow-key SendInput into a foreground window
  * sub-millisecond pacing against a 110 ms game tick

The pathfinding is deliberately simple (greedy with a safety check), because
the thing under test is the library, not the AI. The bot must not loop forever:
every run is bounded by a wall clock and a tick budget.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from basira import Point, Region, auto

CELL = 24
COLS, ROWS = 20, 16
HEAD = "#00ff00"
BODY = "#008f26"
FOOD = "#ff0000"
BAR = "#00bfff"

GAME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snake_game.py")


class SnakeBot:
    def __init__(self, *, seed: int = 7, budget_s: float = 75.0,
                 max_ticks: int = 900, verbose: bool = True):
        self.seed = seed
        self.budget_s = budget_s
        self.max_ticks = max_ticks
        self.verbose = verbose
        self.proc: subprocess.Popen | None = None
        self.win = None
        self.board: Region | None = None
        self.log: list[str] = []
        self.stats = {"frames": 0, "keys": 0, "reads_failed": 0,
                      "best_score": 0, "deaths": 0}

    # -- setup -------------------------------------------------------------- #
    def launch(self) -> bool:
        self.proc = subprocess.Popen([sys.executable, GAME, str(self.seed)])
        self.win = auto.wait_window("basira-snake", timeout=15)
        if self.win is None:
            return False
        if not self.win.wait_ready(timeout=8):
            return False
        self.win.activate(timeout=3)
        c = self.win.client_rect
        self.board = Region(c.x, c.y, COLS * CELL, ROWS * CELL)
        # Wait for the board to actually render its first green head pixel
        # rather than sleeping a guessed 600 ms (at 45 ms/tick the snake hits
        # the wall in 450 ms, so a fixed sleep kills it before tick 0).
        auto.wait_for_color(HEAD, self.board, timeout=3)
        if self.is_dead():
            auto.tap("r")
            auto.wait(0.05)
        self.say(f"window {self.win.hwnd} client={tuple(c)} board={tuple(self.board)}")
        return True

    def say(self, msg: str):
        self.log.append(msg)
        if self.verbose:
            print(f"   {msg}")

    # -- perception --------------------------------------------------------- #
    def to_cell(self, p: Point) -> tuple[int, int]:
        return ((p.x - self.board.x) // CELL, (p.y - self.board.y) // CELL)

    def read(self):
        """One glance: head, food, and every body cell, from ONE frame.

        All three queries hit the same frame, which is the only way they can be
        reasoned about together - a head read from frame N and food from frame
        N+1 describe different worlds and produce a bot that chases ghosts.
        """
        f = auto.grab(self.board)
        self.stats["frames"] += 1

        head_pt = f.centroid(HEAD)
        food_pt = f.centroid(FOOD)
        if head_pt is None:
            self.stats["reads_failed"] += 1
            return None

        occupied = set()
        for p in f.find_all(BODY):
            occupied.add(self.to_cell(p))
        head = self.to_cell(head_pt)
        occupied.discard(head)
        food = None if food_pt is None else self.to_cell(food_pt)
        return head, food, occupied, f

    def read_score_bar(self) -> int:
        """Score read from pixels: count the 7x5 blue blocks (50 per row)."""
        c = self.win.client_rect
        strip = Region(c.x, c.y + ROWS * CELL, COLS * CELL, 16)
        f = auto.grab(strip)
        pts = f.find_all(BAR)
        if not pts:
            return 0
        cells = {((p.y - strip.y - 2) // 7, (p.x - strip.x) // 9) for p in pts}
        return len(cells)

    def read_score_title(self) -> int:
        t = self.win.title
        if "score" in t:
            try:
                return int(t.split("score")[1].split("|")[0].strip())
            except (ValueError, IndexError):
                return -1
        return -1

    def is_dead(self) -> bool:
        return "dead" in self.win.title

    # -- decision ----------------------------------------------------------- #
    @staticmethod
    def neighbours(cell):
        x, y = cell
        return {"left": (x - 1, y), "right": (x + 1, y),
                "up": (x, y - 1), "down": (x, y + 1)}

    def safe(self, cell, occupied) -> bool:
        x, y = cell
        return 0 <= x < COLS and 0 <= y < ROWS and cell not in occupied

    def free_space(self, start, occupied, cap: int = 60) -> int:
        """Flood fill, capped. Used to avoid the classic greedy failure of
        sealing yourself into a pocket - bounded so one decision can never
        turn into an unbounded search."""
        seen = {start}
        stack = [start]
        n = 0
        while stack and n < cap:
            cur = stack.pop()
            n += 1
            for nb in self.neighbours(cur).values():
                if nb not in seen and self.safe(nb, occupied):
                    seen.add(nb)
                    stack.append(nb)
        return n

    def choose(self, head, food, occupied) -> str | None:
        """Greedy step toward the food, rejecting moves into tight pockets."""
        options = []
        for name, cell in self.neighbours(head).items():
            if not self.safe(cell, occupied):
                continue
            room = self.free_space(cell, occupied | {head})
            if food is None:
                dist = 0
            else:
                dist = abs(cell[0] - food[0]) + abs(cell[1] - food[1])
            options.append((room < 6, dist, -room, name))
        if not options:
            return None
        options.sort()
        return options[0][3]

    # -- loop --------------------------------------------------------------- #
    def play(self) -> dict:
        deadline = time.perf_counter() + self.budget_s
        ticks = 0
        last_head = None
        key_times = []
        read_times = []

        while time.perf_counter() < deadline and ticks < self.max_ticks:
            if self.is_dead():
                self.stats["deaths"] += 1
                score = self.read_score_title()
                self.say(f"died at score {score} after {ticks} ticks")
                break

            t0 = time.perf_counter()
            state = self.read()
            read_times.append((time.perf_counter() - t0) * 1000)
            if state is None:
                auto.wait(0.05)
                ticks += 1
                continue

            head, food, occupied, _ = state
            # Only act once per head position: the game folds all key presses
            # within one tick into `self.pending`, so deciding twice while the
            # snake is still on the same cell overwrites our own previous turn.
            if head == last_head:
                auto.wait(0.004)
                continue
            last_head = head

            move = self.choose(head, food, occupied)
            if move is None:
                self.say(f"boxed in at {head}, no safe move")
                auto.wait(0.02)
                ticks += 1
                continue

            t0 = time.perf_counter()
            auto.tap(move)
            key_times.append((time.perf_counter() - t0) * 1000)
            self.stats["keys"] += 1

            s = self.read_score_title()
            if s > self.stats["best_score"]:
                self.stats["best_score"] = s
                if s in (1, 3, 5, 8, 12, 18, 25):
                    self.say(f"score {s} (tick {ticks}, head {head}, food {food})")

            ticks += 1
            # Wait roughly half a game tick so we react once per tick without
            # busy-spinning. The tick is configurable via SNAKE_TICK_MS so the
            # same bot can be driven up to the library's real latency ceiling.
            tick_ms = int(os.environ.get("SNAKE_TICK_MS", 110))
            auto.wait(max(0.004, tick_ms / 2000.0))

        read_times.sort()
        key_times.sort()
        return {
            **self.stats,
            "ticks": ticks,
            "read_p50_ms": read_times[len(read_times) // 2] if read_times else None,
            "read_max_ms": read_times[-1] if read_times else None,
            "key_p50_ms": key_times[len(key_times) // 2] if key_times else None,
            "final_title_score": self.read_score_title(),
            "final_bar_score": self.read_score_bar(),
        }

    def close(self):
        try:
            if self.win and self.win.exists:
                self.win.close()
                time.sleep(0.4)
        except Exception:
            pass
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


def main():
    print("=" * 96)
    print("SNAKE: closed-loop live test of the whole library")
    print("=" * 96)
    bot = SnakeBot(seed=7, budget_s=75.0)
    try:
        if not bot.launch():
            print("   FAILED to launch or find the game window")
            return 1
        result = bot.play()
    finally:
        bot.close()

    print("\n   --- result ---")
    for k, v in result.items():
        print(f"   {k:<20} {v}")

    checks = []
    def chk(name, ok, detail=""):
        checks.append((name, ok))
        print(f"   [{'PASS' if ok else 'FAIL'}] {name:<46} {detail}")

    print("\n   --- assertions ---")
    chk("bot found and read the board", result["frames"] > 20, f"{result['frames']} frames")
    chk("pixel reads never failed", result["reads_failed"] == 0,
        f"{result['reads_failed']} failures")
    chk("keys were actually injected", result["keys"] > 5, f"{result['keys']} keys")
    chk("scored at least one point", result["best_score"] >= 1,
        f"best {result['best_score']}")
    chk("pixel score agrees with title score",
        result["final_bar_score"] == max(result["final_title_score"], 0)
        or result["final_title_score"] < 0,
        f"bar={result['final_bar_score']} title={result['final_title_score']}")
    if result["read_p50_ms"]:
        chk("board read keeps up with the game tick", result["read_p50_ms"] < 110,
            f"p50 {result['read_p50_ms']:.1f} ms vs 110 ms tick")
    if result["key_p50_ms"]:
        chk("key injection is sub-millisecond", result["key_p50_ms"] < 1.0,
            f"p50 {result['key_p50_ms']:.3f} ms")

    failed = [n for n, ok in checks if not ok]
    print(f"\n   passed {len(checks) - len(failed)} / {len(checks)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
