"""Optional organic-motion profiles.

Off by default: a library that silently adds 200 ms of jitter to every click is
lying about its own latency. On request, it shapes the *trajectory and timing*
of real input events rather than wrapping them in sleeps.

Three measured facts drive the design:

1. `SendInput` costs 26 us for one event but 5.9 us/event in a batch of 8. A
   60-point human-like curve pushed as batches is CHEAPER than 60 separate
   calls (0.35 ms vs 1.56 ms), so organic motion is not a latency tax - naive
   per-event sending is.

2. Batching stops paying above ~8 events/call: 32 events measured 13.2 us/event
   and 128 measured 16.0 us/event. The queue drains at a bounded rate, so an
   oversized batch just blocks in the syscall. Chunk at 8.

3. Events inside one `SendInput` call carry no inter-event delay - the whole
   batch lands in the same input tick. Timing realism therefore comes from the
   gaps BETWEEN batches, which is exactly what a real mouse's USB polling
   interval looks like (1-8 ms).
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field


@dataclass
class Profile:
    """How organic should it look? Every field is independently tunable, and
    the ceiling warnings live in the docs, not in clamps - a user who wants
    `jitter=50` has a reason (recording a demo, stress-testing a detector)."""

    name: str = "natural"

    # -- pointer path -------------------------------------------------------
    curve: float = 0.18
    """Lateral bow of the travel arc, as a fraction of distance. 0 = straight
    line (what every naive bot emits). Humans overshoot the chord because a
    wrist rotates around a joint."""

    overshoot: float = 0.08
    """Chance-weighted fraction of distance travelled past the target before
    correcting back. Fitts' law behaviour: fast long moves overshoot, slow
    short ones do not."""

    jitter: float = 1.2
    """Per-sample pixel noise. Real hands tremor ~1 px at 100+ Hz."""

    steps_per_100px: float = 9.0
    """Path resolution. Humans produce roughly this sample density at typical
    speeds; too many samples is as unnatural as too few."""

    min_steps: int = 6
    max_steps: int = 140

    # -- timing -------------------------------------------------------------
    poll_interval_ms: tuple[float, float] = (1.0, 7.0)
    """Gap between batches. Matches real USB HID polling spread (125-1000 Hz)."""

    click_hold_ms: tuple[float, float] = (38.0, 95.0)
    """Button down->up dwell. Measured human range; a 0 ms hold is the single
    most obvious tell in naive automation."""

    key_hold_ms: tuple[float, float] = (28.0, 72.0)
    key_gap_ms: tuple[float, float] = (45.0, 135.0)

    typo_rate: float = 0.0
    """Fraction of characters typed wrong then corrected with backspace. Off by
    default: it changes what the script actually types, which is a semantic
    change, not a cosmetic one."""

    # -- fatigue ------------------------------------------------------------
    fatigue: float = 0.0
    """Progressive slow-down factor across a long session. 0.15 means timings
    drift ~15% slower over `fatigue_horizon` actions."""

    fatigue_horizon: int = 500

    rng: random.Random = field(default_factory=random.Random, repr=False)

    # -- derived ------------------------------------------------------------
    def seeded(self, seed: int) -> "Profile":
        """A reproducible clone - organic motion you can actually unit-test."""
        import copy
        p = copy.copy(self)
        p.rng = random.Random(seed)
        return p

    def _span(self, lo_hi: tuple[float, float], actions_done: int = 0) -> float:
        lo, hi = lo_hi
        v = self.rng.uniform(lo, hi)
        if self.fatigue:
            v *= 1.0 + self.fatigue * min(1.0, actions_done / max(1, self.fatigue_horizon))
        return v

    # -- path generation ----------------------------------------------------
    def path(self, x0: int, y0: int, x1: int, y1: int) -> list[tuple[int, int]]:
        """A plausible pointer trajectory from start to target.

        Shape: cubic Bezier with perpendicular-offset control points (the wrist
        arc), minimum-jerk time warping (fast middle, soft ends - the velocity
        profile human reaching actually follows), optional overshoot-and-settle,
        and per-sample tremor. The last sample is always exactly the target, so
        organic motion never costs accuracy.
        """
        rng = self.rng
        dx, dy = x1 - x0, y1 - y0
        dist = math.hypot(dx, dy)
        if dist < 1.5:
            return [(x1, y1)]

        n = int(min(self.max_steps, max(self.min_steps, dist / 100.0 * self.steps_per_100px)))

        # control points offset perpendicular to the chord
        px, py = -dy / dist, dx / dist
        bow = dist * self.curve * rng.uniform(-1.0, 1.0)
        c1 = (x0 + dx * 0.30 + px * bow, y0 + dy * 0.30 + py * bow)
        c2 = (x0 + dx * 0.70 + px * bow * rng.uniform(0.3, 1.0),
              y0 + dy * 0.70 + py * bow * rng.uniform(0.3, 1.0))

        # overshoot grows with distance: a 40 px nudge does not overshoot
        ox, oy = x1, y1
        do_overshoot = self.overshoot > 0 and dist > 120 and rng.random() < 0.55
        if do_overshoot:
            amt = dist * self.overshoot * rng.uniform(0.5, 1.0)
            ox = x1 + dx / dist * amt
            oy = y1 + dy / dist * amt

        pts: list[tuple[int, int]] = []
        for i in range(1, n + 1):
            t = i / n
            # minimum-jerk warp: slow start, fast middle, slow arrival
            tw = t * t * t * (10 - 15 * t + 6 * t * t)
            mt = 1 - tw
            bx = (mt ** 3 * x0 + 3 * mt * mt * tw * c1[0]
                  + 3 * mt * tw * tw * c2[0] + tw ** 3 * ox)
            by = (mt ** 3 * y0 + 3 * mt * mt * tw * c1[1]
                  + 3 * mt * tw * tw * c2[1] + tw ** 3 * oy)
            if self.jitter:
                # tremor fades as the hand closes in on the target
                amp = self.jitter * (1.0 - 0.75 * tw)
                bx += rng.gauss(0, amp)
                by += rng.gauss(0, amp)
            pts.append((int(round(bx)), int(round(by))))

        if do_overshoot:
            # settle back: 2-4 decaying corrective samples
            for k in range(rng.randint(2, 4)):
                f = 0.45 ** (k + 1)
                pts.append((int(round(x1 + (ox - x1) * f)), int(round(y1 + (oy - y1) * f))))

        pts.append((x1, y1))  # land exactly, always
        return pts


# Presets. `instant` is the default everywhere; the rest are opt-in.
INSTANT = Profile(name="instant", curve=0.0, overshoot=0.0, jitter=0.0,
                  steps_per_100px=0.0, min_steps=0, max_steps=0,
                  poll_interval_ms=(0.0, 0.0), click_hold_ms=(0.0, 0.0),
                  key_hold_ms=(0.0, 0.0), key_gap_ms=(0.0, 0.0))

FAST = Profile(name="fast", curve=0.08, overshoot=0.0, jitter=0.5,
               steps_per_100px=4.0, poll_interval_ms=(0.3, 1.5),
               click_hold_ms=(12.0, 30.0), key_hold_ms=(10.0, 25.0),
               key_gap_ms=(12.0, 40.0))

NATURAL = Profile(name="natural")

CAREFUL = Profile(name="careful", curve=0.26, overshoot=0.12, jitter=1.8,
                  steps_per_100px=14.0, poll_interval_ms=(2.0, 11.0),
                  click_hold_ms=(55.0, 140.0), key_hold_ms=(40.0, 95.0),
                  key_gap_ms=(80.0, 260.0), fatigue=0.12)

PRESETS = {p.name: p for p in (INSTANT, FAST, NATURAL, CAREFUL)}


def resolve(spec) -> Profile:
    """Accept None / False / True / 'natural' / Profile - every shape a user
    reaches for when they just want it to look human."""
    if spec is None or spec is False:
        return INSTANT
    if spec is True:
        return NATURAL
    if isinstance(spec, Profile):
        return spec
    if isinstance(spec, str):
        key = spec.lower().strip()
        if key in PRESETS:
            return PRESETS[key]
        raise ValueError(f"unknown profile {spec!r}; known: {sorted(PRESETS)}")
    raise TypeError(f"cannot read a humanize profile from {type(spec).__name__}")
