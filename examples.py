"""Basira by example.

Each block is a complete, runnable idea. Nothing here is pseudocode.
Run with:  python examples.py <number>
"""
from __future__ import annotations

import sys

from basira import CAREFUL, auto


def ex1_name_what_you_want():
    """The whole point: click a thing by name, not by coordinate."""
    auto.click("submit button")


def ex2_look_around_first():
    """You do not know the right words yet? Ask the screen what it contains.

    Prompt-free mode answers from a 4,585-name vocabulary, so this is how you
    discover which nouns actually land before writing the real script.
    """
    seen = auto.look()
    print("this screen contains:", seen.labels())
    for d in seen.all()[:10]:
        print("  ", d)


def ex3_scope_with_regions():
    """'The play button in the player, not the one in the sidebar.'

    Regions compose, which is how spatial meaning gets expressed without
    hardcoding a single coordinate.
    """
    player = auto.region("video player")
    if player:
        auto.click("play button", within=player)
    else:
        print("no player on screen")


def ex4_scope_to_a_window():
    """Window geometry is a Region, so it drops straight into `within`."""
    chrome = auto.find("Chrome")
    if not chrome:
        print("Chrome is not running")
        return
    chrome.activate()
    auto.wait(0.3)
    inside = chrome.client_rect
    print("buttons in Chrome:", auto.look(inside, prompts=["button"]).count())


def ex5_wait_then_act():
    """The most common bot step, in one line. Raises if it never appears -
    a step that silently skips itself is worse than one that stops."""
    auto.click_when_seen("accept button", timeout=30)


def ex6_wait_for_the_spinner_to_go():
    """The honest replacement for sleep(5) after triggering slow work."""
    auto.click("upload")
    auto.wait_to_vanish("loading spinner", timeout=120)
    print("upload finished")


def ex7_pixels_need_no_model():
    """Colour work stays on the fast path: no torch, no weights, 0.44 us/query.

    One frame, many questions - the frame is a moment of the screen, so every
    answer below describes the SAME instant.
    """
    frame = auto.grab()
    print("pixel at 100,100: 0x%06x" % frame.pixel(100, 100))
    print("red pixels:", frame.count("#ff0000"))
    print("red bounding box:", frame.bounds("#ff0000"))
    print("red centre:", frame.centroid("#ff0000"))


def ex8_wait_for_the_screen_to_settle():
    """No fixed sleep: watch until pixels stop moving."""
    auto.press("ctrl", "t")
    settled = auto.wait_until_still(timeout=5)
    print("screen settled:" if settled else "still animating after 5s:", settled)


def ex9_type_anything():
    """Layout-independent: the same call lands correctly on a Turkish Q,
    US English or Dvorak keyboard, including characters with no key at all."""
    note = auto.wait_window(class_name="Notepad")
    if not note:
        print("open Notepad first")
        return
    note.activate()
    auto.wait(0.3)
    auto.type("Türkçe ıİşŞğĞ + 日本語 + emoji 🚀\n")


def ex10_look_human():
    """Organic mode: biological pointer arcs, tremor, variable key cadence.

    Measured: batching those waypoints makes this CHEAPER than naive
    per-event automation, so looking human is not a latency tax.
    """
    auto.humanize(CAREFUL)
    try:
        auto.click("like button")
        auto.type("thanks!")
    finally:
        auto.humanize(False)


def ex11_count_things():
    """Vision as a sensor rather than a pointer - no clicking at all."""
    print("people on screen:", auto.look(prompts=["person"]).count("person"))
    print("windows open:", len(auto.windows()))


def ex12_click_every_one():
    """All detections come from ONE frame, so the coordinates agree with each
    other - unlike a re-detect-after-each-click loop chasing a moving layout."""
    n = auto.click_all("checkbox")
    print(f"clicked {n} checkboxes")


def ex13_know_your_machine():
    """Print this first when a script behaves differently on another host.
    DPI mode and refresh rate explain most surprises."""
    for k, v in auto.describe_environment().items():
        print(f"{k:<24} {v}")


EXAMPLES = [
    ex1_name_what_you_want,
    ex2_look_around_first,
    ex3_scope_with_regions,
    ex4_scope_to_a_window,
    ex5_wait_then_act,
    ex6_wait_for_the_spinner_to_go,
    ex7_pixels_need_no_model,
    ex8_wait_for_the_screen_to_settle,
    ex9_type_anything,
    ex10_look_human,
    ex11_count_things,
    ex12_click_every_one,
    ex13_know_your_machine,
]

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        for i, fn in enumerate(EXAMPLES, 1):
            first = (fn.__doc__ or "").strip().split("\n")[0]
            print(f"  {i:>2}. {fn.__name__:<34} {first}")
        print("\nSafe to run without touching anything: 2, 7, 11, 13")
        sys.exit(0)
    EXAMPLES[int(sys.argv[1]) - 1]()
