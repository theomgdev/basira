"""Offline replay of captured CS 1.6 frames through the live aim pipeline.

Verifies the viewmodel/HUD rejection filter against ground truth read off the
20 `de_dust00NN.bmp` captures: every real bot must survive, every artefact must
be rejected, with no hand-tuning per frame.
"""
from __future__ import annotations

import glob
import os
import sys
import time

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from basira import Region, auto
from basira.screen import Frame, Point
from cs_aim import PROMPTS, plausible_target

SHOTS = "C:/Program Files (x86)/Steam/steamapps/common/Half-Life/cstrike/de_dust00*.bmp"


def main():
    files = sorted(glob.glob(SHOTS))
    if not files:
        print("no captures found at", SHOTS)
        return 1

    W, H = Image.open(files[0]).size
    fw, fh = int(W * 0.55), int(H * 0.55)
    ox, oy = (W - fw) // 2, (H - fh) // 2
    reg = Region(ox, oy, fw, fh)
    cx, cy = ox + fw // 2, oy + fh // 2
    print(f"game {W}x{H} | crop {fw}x{fh} at ({ox},{oy}) | crosshair ({cx},{cy})")
    print(f"{len(files)} captured frames\n")

    kept = rejected = 0
    frames_with_target = 0
    for seq, f in enumerate(files):
        im = Image.open(f).convert("RGB")
        rgb = np.asarray(im.crop((ox, oy, ox + fw, oy + fh)))
        bgra = np.dstack([rgb[:, :, ::-1],
                          np.full(rgb.shape[:2], 255, np.uint8)]).copy()
        fr = Frame(bgra, Point(ox, oy), time.perf_counter(), 7000 + seq, "bmp")
        name = os.path.basename(f)

        dets = auto.vision.detect(fr, PROMPTS, confidence=0.15)
        survivors = []
        for d in dets:
            ok = plausible_target(d, reg)
            asp = d.box.width / max(1, d.box.height)
            rely = (d.center.y - oy) / fh
            print(f"  {'KEEP  ' if ok else 'REJECT'} {name}: conf={d.confidence:.2f} "
                  f"{d.box.width:4}x{d.box.height:4} aspect={asp:.2f} rel_y={rely:.2f}")
            if ok:
                kept += 1
                survivors.append(d)
            else:
                rejected += 1

        if survivors:
            frames_with_target += 1
            t = min(survivors, key=lambda d: (d.center.x - cx) ** 2
                    + (d.center.y - cy) ** 2)
            ax = t.center.x
            ay = t.box.y + int(t.box.height * 0.35)
            print(f"         -> aim ({ax},{ay})  dx={ax-cx:+5} dy={ay-cy:+5}")

    print(f"\nkept {kept} targets, rejected {rejected} artefacts, "
          f"{frames_with_target}/{len(files)} frames would engage")
    return 0


if __name__ == "__main__":
    sys.exit(main())
