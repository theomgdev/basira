"""Basira CS 1.6 live test: hold CTRL (duck) and the aim closes itself.

The whole bot is a closed loop, which is why it needs no sensitivity
calibration at all:

    read frame -> find people -> pick the one nearest screen centre
                -> auto.move(target) -> screen rotates a bit -> repeat

CS 1.6 consumes the pointer motion to rotate the view *before* it re-centres
the OS cursor, so one absolute `auto.move` toward a target produces a partial
turn. Because the loop re-measures the error every frame, those partial turns
accumulate and converge on the target — the same self-correcting structure as
the Snake bot, with no pixel-to-degree constant to get wrong.

Usage (run from the repo root with the game already open):

    python tests_live/cs_aim.py probe     # diagnose: is the screen readable?
    python tests_live/cs_aim.py aim       # the real thing: hold CTRL to aim+fire

While `aim` runs: hold CTRL to acquire and fire, release to stop, press END to
quit the script.
"""
from __future__ import annotations

import ctypes
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from basira import INSTANT, NATURAL, Region, auto
from basira._win32 import user32

VK_CONTROL = 0x11
VK_END = 0x23

# Closed-loop aim tuning.
#   The loop corrects `gain` of the remaining screen-space error each frame, so
#   the aim decays onto the target instead of being teleported there. Below ~0.5
#   that decay is visible as a wrist settling; at 1.0 the correction is applied
#   whole, which converges fastest and — with the game's own sensitivity lowered
#   to compensate — was what felt best in live play. The ringing that full gain
#   can cause comes from loop dead time (the correction lands after the camera
#   has already turned), so if the aim ever flickers left-right, lower this
#   rather than raising it.
AIM_GAIN = 1
DEAD_ZONE_PX = 3      # stop nudging once this close; prevents idle jitter

# Trigger discipline. A real player does not hold the mouse down forever: with
# a pistol they tap, with a rifle they fire a short burst and let the spray
# reset. Both are modelled as a down/up cycle with randomised lengths, because
# a fixed cadence is itself a detectable signature.
#
# Halved from the first live session: the original 40-90/110-230 ms pistol
# cadence felt sluggish in play, so every burst and pause below is half of
# what it was while keeping the randomised spread.
FIRE_MODES = {
    # name:      (burst_ms_range,  pause_ms_range)
    "tap":       ((20, 45),        (55, 115)),    # pistol: single shots
    "burst":     ((55, 120),       (75, 160)),    # rifle: 3-6 round bursts
    "hold":      ((200, 450),      (30, 70)),     # spray: long trigger pull
}
DEFAULT_FIRE_MODE = "tap"

# Human reaction before the first shot of an engagement. Also halved.
REACTION_S = (0.005, 0.011)

# How close the aim must be before the trigger is allowed, as a fraction of the
# target's own box height — so a distant 35px-tall bot and a close 500px-tall
# one get proportionate windows instead of one absolute pixel count.
#
# Measured over a live session with a fixed 70px window: engagements starting
# at 95-156px of error fired ZERO bursts across 7, 2 and 31 tracking frames,
# because the player never happened to be looking at the target already. Real
# CS players shoot while turning, so the window is now generous by default and
# `fire_window=0` disables the check entirely.
FIRE_WINDOW_RATIO = 3.0
FIRE_WINDOW_MIN_PX = 120

# CS 1.6 player models are low-poly and dark; these prompts all describe one.
PROMPTS = ["person", "soldier", "man", "police", "shield", "human", "human siluette"]

# A standing human is taller than wide on screen. Measured on 20 captured
# de_dust frames: every true bot detection had aspect (w/h) between 0.29 and
# 0.87, while the two false positives (the first-person viewmodel hands/weapon
# and HUD furniture at the bottom of the screen) were 2.38 and 3.35 wide — and
# both scored only 0.26-0.29 against 0.52-0.87 for real players.
MAX_ASPECT = 1.1
MIN_CONF = 0.25
# The viewmodel occupies the bottom strip of the screen permanently; nothing
# there is ever a target worth turning towards.
VIEWMODEL_BAND = 0.78

# Target selection. Scoring purely by "distance from box centre to crosshair"
# systematically prefers whoever is standing right next to you — usually a
# teammate — for two compounding reasons:
#   1. A body a metre away fills much of the frame, and a box that overflows
#      the watched region gets CLIPPED to it, which drags its computed centre
#      toward the region centre, i.e. toward the crosshair. The bigger and
#      closer the body, the more its centre is pulled onto your aim point.
#   2. Even unclipped, a large box's centre sweeps a wide area, so it wins the
#      nearest-to-centre comparison against a small distant silhouette that is
#      actually the one you are trying to shoot.
# So the score penalises apparent size, and bodies large enough to be touching
# you (or clipped by the region edge) are dropped outright.
SIZE_PENALTY_REF = 200.0   # px of box height that doubles the effective distance
MAX_HEIGHT_FRAC = 0.80     # drop bodies taller than this fraction of the region
EDGE_CLIP_PX = 3           # box within this many px of a region edge = clipped

# Uprightness preference. A player standing or running is tall and narrow; one
# that is dying, ragdolling or already dead collapses toward horizontal. Aspect
# (w/h) measured across the captured frames separates them cleanly:
#     0.30-0.57  upright, confident detections (conf 0.66-0.87)
#     0.73-0.87  crouching, leaning, or very close
#     2.38-3.35  flat on the ground (conf 0.26-0.29)
# MAX_ASPECT already rejects the flat ones outright; this factor additionally
# *prefers* the most upright candidate among those that pass, so the aim moves
# on to a live threat instead of emptying a magazine into a falling body.
UPRIGHT_REF = 0.55         # aspect treated as "fully upright", no penalty
UPRIGHT_WEIGHT = 7.0       # multiplier applied per unit of aspect beyond that
UPRIGHT_EXP = 2.0          # superlinear: collapsing costs far more than leaning

# Fraction of the screen watched each frame. 1.0 = the whole screen, which is
# what live play settled on: a crop is faster per frame but blind to anything
# outside it, and on an ultrawide that blind band is where flankers appear.
# See center_region() for the measured cost of each choice.
REGION_FRACTION = 1


def plausible_target(d, reg: Region) -> bool:
    """Reject the player's own gun/hands and HUD furniture.

    Three independent signals, each measured rather than assumed:
      * aspect: a standing player is taller than wide (<= 1.1)
      * confidence: real bots scored >= 0.52, artefacts <= 0.29
      * position: the lower ~22% of the frame is permanently viewmodel
    """
    if d.confidence < MIN_CONF:
        return False
    if d.box.height <= 0 or d.box.width / d.box.height > MAX_ASPECT:
        return False
    rel_y = (d.center.y - reg.y) / reg.height
    if rel_y > VIEWMODEL_BAND:
        return False
    # A body this large is a metre away — in a bot match that is nearly always
    # a teammate brushing past, and its clipped box would hijack the aim.
    if d.box.height > reg.height * MAX_HEIGHT_FRAC:
        return False
    # A box touching a region edge is clipped, so its centre is an artefact
    # pulled toward the crosshair rather than a real aim point.
    if (d.box.x <= reg.x + EDGE_CLIP_PX
            or d.box.y <= reg.y + EDGE_CLIP_PX
            or d.box.x + d.box.width >= reg.x + reg.width - EDGE_CLIP_PX
            or d.box.y + d.box.height >= reg.y + reg.height - EDGE_CLIP_PX):
        return False
    return True


def target_score(d, cx: int, cy: int) -> float:
    """Lower is better. Distance to the crosshair, penalised by apparent size
    and by how far the body has collapsed from upright.

    Dividing by a size factor would reward big targets; multiplying by it makes
    a near, frame-filling body have to be genuinely much closer to the aim than
    a distant silhouette before it wins — which is what "prefer the far one"
    means in practice, without hard-coding a distance estimate.

    The uprightness factor then breaks ties toward whoever is still standing,
    so a dying body sliding to the floor stops attracting fire while a live
    enemy is on screen.
    """
    ax = d.center.x
    ay = d.box.y + int(d.box.height * 0.35)
    dist = ((ax - cx) ** 2 + (ay - cy) ** 2) ** 0.5
    size_penalty = 1.0 + d.box.height / SIZE_PENALTY_REF
    aspect = d.box.width / max(1, d.box.height)
    collapse = max(0.0, aspect - UPRIGHT_REF)
    upright_penalty = 1.0 + UPRIGHT_WEIGHT * collapse ** UPRIGHT_EXP
    return dist * size_penalty * upright_penalty


def key_down(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def center_region(fraction: float = REGION_FRACTION) -> Region:
    """A centred box covering `fraction` of the screen, or all of it at 1.0.

    This crop is a latency tradeoff, measured on this 3440x1440 host:

        fraction 0.55 (1892x792)  -> grab 13.0 ms + detect 14.5 ms = 36.3 Hz
        fraction 1.00 (3440x1440) -> grab 24.3 ms + detect 24.3 ms = 20.6 Hz

    Both halves scale with area: the DIB copy and YOLOE's letterbox both grow,
    so full screen costs a little under half the loop rate. It is a real cost
    but not a prohibitive one -- enemies entering from the far edges of an
    ultrawide are invisible to a crop, and on an ultrawide that is a lot of
    screen to be blind to. Hence tunable rather than fixed.
    """
    sw, sh = auto.screen.size
    if fraction >= 1.0:
        return Region(0, 0, sw, sh)
    w, h = int(sw * fraction), int(sh * fraction)
    return Region((sw - w) // 2, (sh - h) // 2, w, h)


def probe():
    print("=" * 78)
    print("PROBE: is a fullscreen-exclusive CS 1.6 frame readable at all?")
    print("=" * 78)

    sw, sh = auto.screen.size
    print(f"   screen            : {sw}x{sh}")
    win = auto.find("Counter-Strike") or auto.active
    print(f"   foreground window : {win!r}")

    reg = center_region()
    times = []
    frame = None
    for _ in range(8):
        t0 = time.perf_counter()
        frame = auto.grab(reg)
        times.append((time.perf_counter() - t0) * 1000)
    times.sort()

    px = frame.bgra[:, :, :3]
    mean = float(px.mean())
    std = float(px.std())
    uniq = int(np.unique(px[::16, ::16].reshape(-1, 3), axis=0).shape[0])

    print(f"   grab {reg.width}x{reg.height}   : p50={times[4]:.1f} ms")
    print(f"   pixel mean/std    : {mean:.1f} / {std:.1f}")
    print(f"   distinct colours  : {uniq} (sampled)")

    if mean < 1.0 and std < 1.0:
        print("\n   >>> BLACK FRAME. Exclusive fullscreen is bypassing the")
        print("   >>> compositor. Switch CS to windowed mode:")
        print("   >>>   launch options:  -window -noborder")
        print("   >>>   or in-game:      Options > Video > Windowed")
        return False

    print("\n   >>> Frame is readable. Running detection on it...")
    # Keep the prompt list identical across calls: Vision caches `set_classes`
    # by prompt tuple, so a changing list re-runs the CLIP text encoder every
    # time (250-300 ms) instead of the cached 12-15 ms path.
    t0 = time.perf_counter()
    dets = auto.vision.detect(frame, PROMPTS, confidence=0.15)
    ms = (time.perf_counter() - t0) * 1000
    boxes = [(d.label, round(d.confidence, 2), d.center) for d in dets]
    print(f"   prompts {PROMPTS!r:<28} {ms:6.1f} ms  ->  {boxes if boxes else 'nothing'}")
    t0 = time.perf_counter()
    dets = auto.vision.detect(frame, PROMPTS, confidence=0.15)
    ms = (time.perf_counter() - t0) * 1000
    print(f"   same prompts, 2nd call      {ms:6.1f} ms  (should be ~12-15 ms, proves the cache)")
    return True


def aim(gain: float = AIM_GAIN, fire_mode: str = DEFAULT_FIRE_MODE,
        show_overlay: bool = False, fraction: float = REGION_FRACTION):
    print("=" * 78)
    print("AIM: hold CTRL to lock on and fire. Press END to quit.")
    print("=" * 78)

    burst_ms, pause_ms = FIRE_MODES[fire_mode]
    reg = center_region(fraction)
    cx, cy = reg.x + reg.width // 2, reg.y + reg.height // 2
    print(f"   watching {reg.width}x{reg.height} around ({cx}, {cy})")
    print(f"   gain={gain}  dead zone={DEAD_ZONE_PX}px  "
          f"fire window={'off (always)' if FIRE_WINDOW_RATIO <= 0 else f'{FIRE_WINDOW_RATIO}x box height, min {FIRE_WINDOW_MIN_PX}px'}")
    print(f"   fire mode={fire_mode!r}: burst {burst_ms[0]}-{burst_ms[1]} ms, "
          f"pause {pause_ms[0]}-{pause_ms[1]} ms")
    print(f"   overlay={'ON (live debug bounding boxes)' if show_overlay else 'off (pass --overlay to enable)'}")

    # Warm the model so the first CTRL press is not a 4-second stall.
    auto.vision.detect(auto.grab(reg), PROMPTS[:1])
    print("   model warm. go.\n")

    firing = False
    engaged = False
    engagements = 0
    bursts = 0
    loop_ms: list[float] = []
    frames = 0
    phase_until = 0.0

    from basira.overlay import overlay as _get_overlay
    ov = _get_overlay() if show_overlay else None

    def release():
        nonlocal firing
        if firing:
            auto.mouse.up("left")
            firing = False

    try:
        while not key_down(VK_END):
            if not key_down(VK_CONTROL):
                release()
                if ov and ov._visible:
                    ov.hide()
                if engaged:
                    print(f"   release -> ceasefire after {frames} frames, "
                          f"{bursts} bursts")
                    frames = bursts = 0
                    engaged = False
                time.sleep(0.004)
                continue

            t0 = time.perf_counter()
            frame = auto.grab(reg)
            raw_dets = auto.vision.detect(frame, PROMPTS, confidence=0.05)
            dets = [d for d in raw_dets if plausible_target(d, reg)]

            # Live screen overlay: draw what the bot sees directly on screen.
            # 1.45 ms draw cost, completely click-through, auto-hides on release.
            if ov is not None:
                ov.clear()
                # Cyan outline for the watched region itself
                ov.rect(reg, color="cyan", width=1, fill_alpha=0)
                # Red boxes for rejected candidates, with the rejection reason
                for d in raw_dets:
                    if d not in dets:
                        reason = ("conf" if d.confidence < MIN_CONF else
                                  "aspect" if d.box.width / max(1, d.box.height) > MAX_ASPECT else
                                  "viewmodel" if (d.center.y - reg.y) / reg.height > VIEWMODEL_BAND else
                                  "too-close" if d.box.height > reg.height * MAX_HEIGHT_FRAC else
                                  "clipped")
                        ov.rect(d.box, color="red", width=1)
                        from basira.screen import Point
                        ov.label(Point(d.box.x, d.box.y), f"{d.label} [{reason}]",
                                 color="red", size=13)
                # Lime boxes for surviving candidates
                for d in dets:
                    a = d.box.width / max(1, d.box.height)
                    ov.rect(d.box, color="lime", width=2)
                    from basira.screen import Point
                    ov.label(Point(d.box.x, d.box.y),
                             f"{d.label} {d.confidence:.2f} a={a:.2f}",
                             color="lime", size=14)

            if not dets:
                if ov is not None:
                    ov.present()
                release()
                continue

            target = min(dets, key=lambda d: target_score(d, cx, cy))
            ax = target.center.x
            ay = target.box.y + int(target.box.height * 0.35)
            err = ((ax - cx) ** 2 + (ay - cy) ** 2) ** 0.5

            if ov is not None:
                # Mark the chosen target's aim point with a red crosshair
                from basira.screen import Point
                ov.crosshair(Point(ax, ay), color="red", size=16, width=2)
                ov.present()

            if not engaged:
                # A human does not react the instant a shape enters the frame.
                time.sleep(random.uniform(*REACTION_S))
                engagements += 1
                engaged = True
                print(f"   engage #{engagements}: {target.label} "
                      f"{target.confidence:.2f} at ({ax},{ay}) err={err:.0f}px")

            # Proportional correction instead of snapping the whole distance:
            # each frame removes `gain` of the remaining error, so the aim decays
            # exponentially onto the target the way a wrist settles, and cannot
            # oscillate the way a unity-gain loop does. INSTANT because the
            # smoothing now lives in the loop — a Bezier curve here would add
            # camera rotation along the curve's arc rather than toward the target.
            if err > DEAD_ZONE_PX:
                tx = cx + (ax - cx) * gain + random.uniform(-1.5, 1.5)
                ty = cy + (ay - cy) * gain + random.uniform(-1.5, 1.5)
                auto.move(int(tx), int(ty), humanize=INSTANT)

            # Trigger discipline: cycle down/up on randomised deadlines instead
            # of holding the button, so a pistol actually taps and a rifle fires
            # short bursts. The deadline is checked rather than slept on, so the
            # aim loop keeps tracking at full rate through the whole burst.
            now = time.perf_counter()
            # Window scales with the target's on-screen size; ratio 0 = always
            # allowed to shoot, which is closest to how people actually play.
            if FIRE_WINDOW_RATIO <= 0:
                on_target = True
            else:
                window = max(FIRE_WINDOW_MIN_PX,
                             target.box.height * FIRE_WINDOW_RATIO)
                on_target = err < window
            if not on_target:
                release()
            elif firing:
                if now >= phase_until:
                    release()
                    phase_until = now + random.uniform(*pause_ms) / 1000.0
            elif now >= phase_until:
                auto.mouse.down("left")
                firing = True
                bursts += 1
                phase_until = now + random.uniform(*burst_ms) / 1000.0

            frames += 1
            loop_ms.append((time.perf_counter() - t0) * 1000)
    finally:
        release()
        if ov is not None:
            ov.close()

    if loop_ms:
        loop_ms.sort()
        print(f"\n   {engagements} engagements, {len(loop_ms)} tracking frames")
        print(f"   loop latency p50={loop_ms[len(loop_ms)//2]:.1f} ms "
              f"min={loop_ms[0]:.1f} ms  (~{1000/loop_ms[len(loop_ms)//2]:.0f} Hz)")
    print("   done.")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "probe"
    if mode == "probe":
        sys.exit(0 if probe() else 1)
    elif mode == "aim":
        g = AIM_GAIN
        fm = DEFAULT_FIRE_MODE
        frac = REGION_FRACTION
        overlay_flag = False
        for a in sys.argv[2:]:
            if a in ("--overlay", "-o", "overlay"):
                overlay_flag = True
            elif a in ("full", "fullscreen", "--full"):
                frac = 1.0
            elif a.startswith("region="):
                frac = float(a.split("=", 1)[1])
            elif a.startswith("gain="):
                g = float(a.split("=", 1)[1])
            elif a in FIRE_MODES:
                fm = a
            else:
                try:
                    g = float(a)
                except ValueError:
                    print(f"unknown argument {a!r}; fire modes: "
                          f"{', '.join(FIRE_MODES)}; flags: --overlay, full, "
                          f"region=<0-1>, gain=<n>")
                    sys.exit(2)
        aim(gain=g, fire_mode=fm, show_overlay=overlay_flag, fraction=frac)
    else:
        print(__doc__)
        sys.exit(2)
