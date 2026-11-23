"""Open-vocabulary screen understanding: name a thing, get its location.

This is the layer that replaces coordinates with words. Instead of

    auto.click(847, 392)          # what is at 847,392? nobody knows six months later

you write

    auto.click(auto.see("cat"))   # finds a cat, anywhere on screen

Backed by Ultralytics YOLOE (Real-Time Seeing Anything), which takes the
category list at *inference* time as a text prompt, so there is no training
step and no fixed class list. A screen is not COCO, so the words that work are
everyday object names; see `Vision.suggest_vocabulary()` for what the built-in
prompt-free vocabulary already knows.

ARCHITECTURE: the same frame-coherence rule that governs `screen.py` governs
this file, for a stronger reason. A screen read costs one compositor frame
(6.94 ms at 144 Hz on the dev host); a YOLOE forward pass costs the same order
of magnitude on GPU and far more on CPU. So a naive

    auto.click(auto.see("cat"))
    auto.click(auto.see("dog"))

must NOT run the model twice. `Vision.detections()` caches the full detection
result per (frame.seq, prompt-set) and every query reads that cache. Changing
the prompt set is what forces new work, not asking another question.

COST HONESTY: the model is lazy-loaded on first use. First call pays weight
download (tens to hundreds of MB) plus a text-encoder fetch; the text encoder
also writes a .ts file into the current working directory. Nothing here
pretends that is free - `Vision.status()` reports exactly what is loaded, what
it cost, and where it came from.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

from .screen import Frame, Point, Region

# Model families, smallest first. 'n' runs on CPU at reduced resolution; 'x'
# wants a real GPU. Defaulting to 's' is the measured compromise between
# "works on a laptop" and "actually finds things on a cluttered desktop".
SCALES = ("n", "s", "m", "l", "x")
DEFAULT_SCALE = "s"


def _encoder_cache_dir() -> Path:
    """Where the CLIP text encoder lives.

    Ultralytics writes it to the working directory, so without a fixed home
    the same 242 MB downloads again every time a script runs from a new
    folder. BASIRA_CACHE overrides; otherwise it sits beside the Ultralytics
    weights cache so a user cleaning one finds the other.
    """
    env = os.environ.get("BASIRA_CACHE")
    if env:
        p = Path(env)
    else:
        base = (os.environ.get("LOCALAPPDATA")
                or os.environ.get("XDG_CACHE_HOME")
                or str(Path.home() / ".cache"))
        p = Path(base) / "basira" / "encoders"
    p.mkdir(parents=True, exist_ok=True)
    return p


class VisionUnavailable(RuntimeError):
    """Ultralytics is not installed, or the weights could not be obtained.

    Carries the install hint rather than a bare import error, because this is
    the one dependency of the library that is genuinely heavy and a user who
    only wants pixel automation should never be forced into it.
    """


@dataclass(frozen=True)
class Detection:
    """One recognised thing on screen."""

    label: str
    confidence: float
    box: Region
    """Bounding box in absolute screen coordinates."""
    mask: np.ndarray | None = field(default=None, repr=False)
    """Instance segmentation mask, frame-local, bool. None when the model ran
    in detect-only mode."""

    @property
    def center(self) -> Point:
        return self.box.center

    @property
    def area(self) -> int:
        return self.box.width * self.box.height

    def __repr__(self) -> str:
        return (f"<{self.label} {self.confidence:.2f} "
                f"@({self.box.x},{self.box.y}) {self.box.width}x{self.box.height}>")


class DetectionSet(Sequence[Detection]):
    """Everything found in one frame, with the query helpers a script needs.

    Behaves as a sequence so `len()`, iteration and indexing work, but the
    useful surface is the filtering: `.best('cat')`, `.all('button')`,
    `.inside(region)`, `.largest()`.
    """

    __slots__ = ("_items", "frame_seq", "prompts", "elapsed_ms")

    def __init__(self, items: list[Detection], frame_seq: int,
                 prompts: tuple[str, ...], elapsed_ms: float):
        self._items = items
        self.frame_seq = frame_seq
        self.prompts = prompts
        self.elapsed_ms = elapsed_ms

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, i):
        return self._items[i]

    def __repr__(self) -> str:
        counts: dict[str, int] = {}
        for d in self._items:
            counts[d.label] = counts.get(d.label, 0) + 1
        inner = ", ".join(f"{k}x{v}" for k, v in sorted(counts.items())) or "nothing"
        return f"<DetectionSet {inner} in {self.elapsed_ms:.1f}ms>"

    # -- filtering ---------------------------------------------------------- #
    def all(self, label: str | None = None, *, min_confidence: float = 0.0
            ) -> list[Detection]:
        """Every match, highest confidence first.

        Label matching is case-insensitive and substring-based: asking for
        'button' finds 'submit button'. That is the behaviour a beginner
        expects, and an exact match is still available via `label.lower() ==`
        on the result.
        """
        key = None if label is None else label.casefold().strip()
        out = [d for d in self._items
               if d.confidence >= min_confidence
               and (key is None or key in d.label.casefold())]
        out.sort(key=lambda d: d.confidence, reverse=True)
        return out

    def best(self, label: str | None = None, *, min_confidence: float = 0.0
             ) -> Detection | None:
        """Highest-confidence match, or None. The workhorse."""
        r = self.all(label, min_confidence=min_confidence)
        return r[0] if r else None

    def largest(self, label: str | None = None) -> Detection | None:
        """Biggest match by area - what you want when confidence ties and size
        disambiguates (the main window vs its thumbnail)."""
        r = self.all(label)
        return max(r, key=lambda d: d.area) if r else None

    def inside(self, region: Region | tuple) -> "DetectionSet":
        """Only detections whose centre falls within a region. The cheap way to
        say 'the cat in the left panel, not the one in the sidebar'."""
        r = Region(*region)
        keep = [d for d in self._items
                if r.x <= d.center.x < r.x + r.width
                and r.y <= d.center.y < r.y + r.height]
        return DetectionSet(keep, self.frame_seq, self.prompts, self.elapsed_ms)

    def labels(self) -> list[str]:
        """Distinct labels found, most frequent first - useful for exploring
        what prompt-free mode actually sees on a screen."""
        counts: dict[str, int] = {}
        for d in self._items:
            counts[d.label] = counts.get(d.label, 0) + 1
        return [k for k, _ in sorted(counts.items(), key=lambda kv: -kv[1])]

    def count(self, label: str | None = None) -> int:
        return len(self.all(label))


class Vision:
    """Lazy YOLOE wrapper with per-frame detection caching.

    Construction is free and imports nothing; the model loads on the first
    detection call. That matters because `basira` must stay usable as a pure
    Win32 automation library on a machine with no torch installed.
    """

    def __init__(self, *, scale: str = DEFAULT_SCALE, device: str | None = None,
                 confidence: float = 0.25, segment: bool = True,
                 imgsz: int = 640):
        if scale not in SCALES:
            raise ValueError(f"scale must be one of {SCALES}, got {scale!r}")
        self.scale = scale
        self.device = device
        self.confidence = confidence
        self.segment = segment
        self.imgsz = imgsz

        self._model = None
        self._model_name: str | None = None
        self._prompts: tuple[str, ...] = ()
        self._prompt_free = False
        self._load_ms: float | None = None
        self._cache: dict[tuple, DetectionSet] = {}
        self._infer_count = 0
        self._infer_total_ms = 0.0

    # -- model lifecycle ---------------------------------------------------- #
    def _checkpoint(self, prompt_free: bool) -> str:
        suffix = "-seg" if self.segment else ""
        pf = "-pf" if prompt_free else ""
        return f"yoloe-26{self.scale}{suffix}{pf}.pt"

    def _ensure_model(self, prompt_free: bool):
        """Load (or reload) the checkpoint for this prompting mode.

        Text/visual checkpoints and prompt-free checkpoints are different files
        and are not interchangeable - a prompt-free model raises on
        `set_classes`. Switching modes therefore swaps the model.
        """
        want = self._checkpoint(prompt_free)
        if self._model is not None and self._model_name == want:
            return self._model

        try:
            from ultralytics import YOLOE
        except ImportError as e:
            raise VisionUnavailable(
                "Open-vocabulary vision needs Ultralytics:\n"
                "    pip install 'basira[vision]'\n"
                "or  pip install 'ultralytics>=8.4.0'\n"
                "The rest of basira (pixels, input, windows) works without it."
            ) from e

        t0 = time.perf_counter()
        cache = _encoder_cache_dir()
        prev_cwd = os.getcwd()
        try:
            # Same reason as in _ensure_prompts: Ultralytics resolves a bare
            # checkpoint name against the working directory and downloads it
            # there, so pinning the cwd keeps one copy per machine instead of
            # one per folder a script is launched from.
            os.chdir(cache)
            self._model = YOLOE(want)
        except Exception as e:
            raise VisionUnavailable(
                f"could not load {want}: {e}. The first run downloads weights, "
                "so this usually means no network access or no disk space."
            ) from e
        finally:
            os.chdir(prev_cwd)
        self._load_ms = (time.perf_counter() - t0) * 1000.0
        self._model_name = want
        self._prompt_free = prompt_free
        self._prompts = ()
        self._cache.clear()
        return self._model

    def _ensure_prompts(self, prompts: tuple[str, ...]):
        """Set the class list, skipping the work when it has not changed.

        `set_classes` runs the text encoder and is the expensive part of
        prompting (and on first ever call, downloads it). Re-setting an
        identical prompt tuple would pay that for nothing.

        The text encoder is fetched into the PROCESS WORKING DIRECTORY by
        Ultralytics, which means a user who runs the same script from a
        different folder re-downloads ~242 MB. We pin it to a stable cache
        directory and chdir only for the duration of the call, so the download
        happens once per machine instead of once per working directory.
        """
        if prompts == self._prompts:
            return
        model = self._ensure_model(prompt_free=False)

        cache = _encoder_cache_dir()
        prev_cwd = os.getcwd()
        try:
            os.chdir(cache)
            model.set_classes(list(prompts))
        finally:
            os.chdir(prev_cwd)
        self._prompts = prompts
        self._cache.clear()

    def status(self) -> dict:
        """What is actually loaded and what it has cost so far."""
        return {
            "available": self.available(),
            "model": self._model_name,
            "loaded": self._model is not None,
            "load_ms": None if self._load_ms is None else round(self._load_ms, 1),
            "mode": "prompt-free" if self._prompt_free else "text-prompt",
            "prompts": list(self._prompts),
            "scale": self.scale,
            "segment": self.segment,
            "imgsz": self.imgsz,
            "device": self.device or "auto",
            "inferences": self._infer_count,
            "mean_infer_ms": (round(self._infer_total_ms / self._infer_count, 1)
                              if self._infer_count else None),
            "cached_frames": len(self._cache),
        }

    @staticmethod
    def available() -> bool:
        """Can vision run at all? Cheap probe, no model load."""
        try:
            import importlib.util
            return importlib.util.find_spec("ultralytics") is not None
        except Exception:
            return False

    # -- detection ---------------------------------------------------------- #
    def detect(self, frame: Frame, prompts: Iterable[str] | None = None,
               *, confidence: float | None = None,
               region: Region | tuple | None = None) -> DetectionSet:
        """Run detection over a frame (or a sub-region of it).

        Cached on (frame.seq, region, prompt-set, confidence): asking about a
        second object in the same frame costs nothing. Passing `prompts=None`
        uses the prompt-free checkpoint and its built-in 4,585-name vocabulary,
        which is how you explore a screen you have no words for yet.
        """
        conf = self.confidence if confidence is None else confidence
        sub = None if region is None else Region(*region)
        pkey = None if prompts is None else tuple(
            dict.fromkeys(p.strip() for p in prompts if p and p.strip()))
        if pkey is not None and not pkey:
            raise ValueError("prompts was empty; pass None for prompt-free mode")

        ckey = (frame.seq, frame.source, sub, pkey, round(conf, 3))
        hit = self._cache.get(ckey)
        if hit is not None:
            return hit

        if pkey is None:
            model = self._ensure_model(prompt_free=True)
        else:
            self._ensure_prompts(pkey)
            model = self._model

        target = frame if sub is None else frame.crop(sub)
        # Ultralytics wants HxWx3; the frame holds BGRA, and `rgb` is a view -
        # ascontiguousarray is what makes it a real buffer the model can read.
        rgb = np.ascontiguousarray(target.rgb)

        t0 = time.perf_counter()
        kw = dict(conf=conf, imgsz=self.imgsz, verbose=False)
        if self.device:
            kw["device"] = self.device
        results = model.predict(rgb, **kw)
        elapsed = (time.perf_counter() - t0) * 1000.0
        self._infer_count += 1
        self._infer_total_ms += elapsed

        out = self._parse(results, target.origin, elapsed, frame.seq, pkey or ())
        self._cache[ckey] = out
        # Keep the cache bounded; frames are large and a long-running script
        # would otherwise accumulate masks forever.
        if len(self._cache) > 16:
            for k in list(self._cache)[:-8]:
                del self._cache[k]
        return out

    def _parse(self, results, origin: Point, elapsed_ms: float,
               frame_seq: int, prompts: tuple[str, ...]) -> DetectionSet:
        items: list[Detection] = []
        if not results:
            return DetectionSet(items, frame_seq, prompts, elapsed_ms)

        r = results[0]
        names = getattr(r, "names", {}) or {}
        boxes = getattr(r, "boxes", None)
        if boxes is None or len(boxes) == 0:
            return DetectionSet(items, frame_seq, prompts, elapsed_ms)

        xyxy = boxes.xyxy.cpu().numpy()
        confs = boxes.conf.cpu().numpy()
        clses = boxes.cls.cpu().numpy().astype(int)

        masks = None
        mdata = getattr(r, "masks", None)
        if mdata is not None and getattr(mdata, "data", None) is not None:
            masks = mdata.data.cpu().numpy() > 0.5

        for i in range(len(xyxy)):
            x0, y0, x1, y1 = (float(v) for v in xyxy[i])
            label = str(names.get(int(clses[i]), clses[i]))
            items.append(Detection(
                label=label,
                confidence=float(confs[i]),
                box=Region(int(round(x0)) + origin.x, int(round(y0)) + origin.y,
                           max(1, int(round(x1 - x0))), max(1, int(round(y1 - y0)))),
                mask=None if masks is None or i >= len(masks) else masks[i],
            ))
        items.sort(key=lambda d: d.confidence, reverse=True)
        return DetectionSet(items, frame_seq, prompts, elapsed_ms)

    # -- vocabulary --------------------------------------------------------- #
    def vocabulary(self) -> list[str]:
        """The prompt-free checkpoint's built-in vocabulary.

        Worth printing before guessing prompts: it tells you which words the
        model already has embeddings for, instead of discovering by trial that
        your chosen noun lands nowhere.
        """
        model = self._ensure_model(prompt_free=True)
        names = getattr(model, "names", None)
        if isinstance(names, dict):
            return [str(v) for v in names.values()]
        return [str(v) for v in (names or [])]
