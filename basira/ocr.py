"""Real-time multilingual screen OCR: find, click, hover, or select any text.

Backed by RapidOCR (PP-OCR DBNet text detection + SVTR text recognition),
running on ONNX Runtime or PyTorch without requiring PaddlePaddle.

Why a dedicated OCR layer alongside open-vocabulary vision (YOLOE):
  * YOLOE sees *objects* by appearance ("cat", "bus", "person", "icon").
  * OCR reads *glyphs* ("Submit", "İptal", "Отмена", "إلغاء", "確定", "A", "B").
  * Together they cover everything on a screen: objects by what they look like,
    controls and labels by what they say.

Three architectural rules keep this layer at hardware-floor latency:

1. **Skip orientation classification (`use_cls=False`) by default.** Desktop UI
   text is not upside-down. Dropping the 180-degree classifier cuts a 3-stage
   pipeline to 2 stages (DBNet + SVTR).
2. **Frame-coherent caching.** Reading a screen region once caches the full
   `TextSet` on `(frame.seq, region, lang)`. Asking for 'A', then 'B', then 'C'
   on the same frame runs OCR **once** and answers the remaining 25 letters from
   RAM in ~1 us each.
3. **Proportional sub-box slicing.** DBNet detects text *lines*, so a toolbar
   like `"File  Edit  View"` can come back as one box. Clicking the box centre
   when the user asked for `"View"` would click `"Edit"`. `TextHit.slice_for()`
   projects the matched character span onto the bounding box horizontally so
   single words—and single letters—inside a line still land accurately, and
   `select_text()` drags across just that span.
"""
from __future__ import annotations

import os
import time
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np

from .screen import Frame, Point, Region
from .vision import _encoder_cache_dir

# Languages routed to PP-OCRv5 mobile recognition (scripts outside PP-OCRv6's
# built-in 52-language Latin + CJK vocabulary).
_V5_SCRIPT_ROUTES: dict[str, str] = {
    # Arabic script
    "ar": "arabic", "arabic": "arabic", "fa": "arabic", "persian": "arabic",
    "ur": "arabic", "urdu": "arabic", "ug": "arabic",
    # Cyrillic script
    "ru": "cyrillic", "russian": "cyrillic", "cyrillic": "cyrillic",
    "uk": "eslav", "be": "eslav", "eslav": "eslav",
    "bg": "cyrillic", "mn": "cyrillic", "rs_cyrillic": "cyrillic",
    # Korean
    "ko": "korean", "kr": "korean", "korean": "korean",
    # Greek
    "el": "el", "gr": "el", "greek": "el",
    # Devanagari & Indic
    "hi": "devanagari", "mr": "devanagari", "ne": "devanagari",
    "sa": "devanagari", "devanagari": "devanagari",
    "ta": "ta", "tamil": "ta",
    "te": "te", "telugu": "te",
    # Thai
    "th": "th", "thai": "th",
}


def _fold(text: str, *, case: bool = False) -> str:
    """Normalise text for matching, handling the Turkish dotted/dotless-I trap.

    In standard Python `'İPTAL'.lower()` produces `'i\\u0307ptal'` (7 codepoints)
    which never matches `'iptal'` (5 codepoints). Folding `İ -> i` and `I -> ı`
    before casefolding makes Turkish, English and every other Latin script
    behave consistently.
    """
    s = unicodedata.normalize("NFKC", text)
    if case:
        return s
    s = s.replace("İ", "i").replace("I", "ı")
    s = s.casefold().replace("\u0307", "")
    return s


class OCRUnavailable(RuntimeError):
    """RapidOCR / ONNX Runtime is not installed."""


@dataclass(frozen=True)
class TextHit:
    """One recognised piece of text on screen."""

    text: str
    confidence: float
    box: Region
    """Bounding box in absolute screen coordinates."""
    polygon: tuple[Point, ...] = ()
    """Four corner points (top-left, top-right, bottom-right, bottom-left)."""

    @property
    def center(self) -> Point:
        return self.box.center

    @property
    def start(self) -> Point:
        """Left-middle edge of the text box: where a selection drag begins."""
        return Point(self.box.x + 2, self.box.y + self.box.height // 2)

    @property
    def end(self) -> Point:
        """Right-middle edge of the text box: where a selection drag ends."""
        return Point(self.box.x + max(2, self.box.width - 2),
                     self.box.y + self.box.height // 2)

    def slice_for(self, query: str, *, case: bool = False) -> "TextHit":
        """Narrow the bounding box to the character span matching `query`.

        When `self.text` equals `query` (or `query` spans the whole box), returns
        `self` unchanged. When `query` is a substring inside a longer line,
        interpolates the horizontal sub-box proportionally to character offsets.
        """
        if not query or not self.text:
            return self
        hay = _fold(self.text, case=case)
        needle = _fold(query, case=case)
        idx = hay.find(needle)
        if idx < 0 or (idx == 0 and len(needle) == len(hay)):
            return self
        total = max(1, len(hay))
        x0 = self.box.x + int(round(self.box.width * (idx / total)))
        x1 = self.box.x + int(round(self.box.width * ((idx + len(needle)) / total)))
        sub_reg = Region(x0, self.box.y, max(4, x1 - x0), self.box.height)
        raw_sub = self.text[idx:idx + len(query)] or query
        return TextHit(text=raw_sub, confidence=self.confidence,
                       box=sub_reg, polygon=self.polygon)

    def __repr__(self) -> str:
        return (f"<TextHit {self.text!r} {self.confidence:.2f} "
                f"@({self.box.x},{self.box.y}) {self.box.width}x{self.box.height}>")


class TextSet(Sequence[TextHit]):
    """Every text span found in one frame or region."""

    __slots__ = ("_items", "frame_seq", "lang", "elapsed_ms")

    def __init__(self, items: list[TextHit], frame_seq: int,
                 lang: str, elapsed_ms: float):
        self._items = items
        self.frame_seq = frame_seq
        self.lang = lang
        self.elapsed_ms = elapsed_ms

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, i):
        return self._items[i]

    def __repr__(self) -> str:
        preview = ", ".join(repr(h.text) for h in self._items[:5])
        more = "" if len(self._items) <= 5 else f", +{len(self._items) - 5} more"
        return f"<TextSet [{preview}{more}] ({len(self._items)} spans in {self.elapsed_ms:.1f}ms)>"

    # -- queries ------------------------------------------------------------ #
    def all(self, query: str | None = None, *, exact: bool = False,
            case: bool = False, min_confidence: float = 0.0,
            slice_substrings: bool = True) -> list[TextHit]:
        """Every span matching `query` (or all spans when `query=None`).

        With `exact=False` (default), a span containing `query` as a substring
        matches and is sliced to the matched character range so clicking lands
        on the requested word rather than the whole line's midpoint.
        """
        if query is None:
            return [h for h in self._items if h.confidence >= min_confidence]

        needle = _fold(query.strip(), case=case)
        if not needle:
            return []

        exact_hits: list[TextHit] = []
        sub_hits: list[TextHit] = []
        for h in self._items:
            if h.confidence < min_confidence:
                continue
            hay = _fold(h.text.strip(), case=case)
            if hay == needle:
                exact_hits.append(h)
            elif not exact and needle in hay:
                sub_hits.append(h.slice_for(query.strip(), case=case)
                                if slice_substrings else h)
        # Prefer exact span matches over substring slices, then sort by score
        exact_hits.sort(key=lambda h: h.confidence, reverse=True)
        sub_hits.sort(key=lambda h: h.confidence, reverse=True)
        return exact_hits + sub_hits

    def best(self, query: str | None = None, *, exact: bool = False,
             case: bool = False, min_confidence: float = 0.0) -> TextHit | None:
        r = self.all(query, exact=exact, case=case, min_confidence=min_confidence)
        return r[0] if r else None

    def reading_order(self, *, line_tolerance_px: int = 14) -> list[TextHit]:
        """Top-to-bottom, left-to-right natural reading order."""
        return sorted(
            self._items,
            key=lambda h: (h.box.y // max(1, line_tolerance_px), h.box.x),
        )

    def alphabetical(self, *, single_chars_only: bool = False,
                     case: bool = False) -> list[TextHit]:
        """Spans sorted alphabetically by their text (A -> Z).

        `single_chars_only=True` filters to single-character targets first,
        which is the exact helper for 'click appearing letters in alphabetical
        order'. If DBNet merged two neighbouring spaced letters into one box
        (e.g. 'A B'), splits them proportionally into per-letter sub-boxes so
        neither letter is lost.
        """
        items: list[TextHit] = []
        for h in self._items:
            raw = h.text.strip()
            if not raw:
                continue
            if single_chars_only:
                if len(raw) == 1 and raw.isalnum():
                    items.append(h)
                else:
                    tokens = raw.split()
                    if len(tokens) > 1 and all(len(t) == 1 and t.isalnum() for t in tokens):
                        for t in tokens:
                            items.append(h.slice_for(t, case=True))
            else:
                items.append(h)
        return sorted(items, key=lambda h: _fold(h.text.strip(), case=case))

    def texts(self) -> list[str]:
        return [h.text for h in self.reading_order()]

    def full_text(self, sep: str = "\n") -> str:
        return sep.join(self.texts())

    def inside(self, region: Region | tuple) -> "TextSet":
        r = Region(*region)
        keep = [h for h in self._items
                if r.x <= h.center.x < r.x + r.width
                and r.y <= h.center.y < r.y + r.height]
        return TextSet(keep, self.frame_seq, self.lang, self.elapsed_ms)


class OCR:
    """Lazy, frame-coherent RapidOCR wrapper supporting 80+ languages.

    Measured pipeline architecture on RTX 3060 Ti (360x140 region):
      * Default RapidOCR (ORT CPU, limit_type='min' 736): 186.8 ms
      * Pure TORCH-CUDA (limit_type='min' 736):            55.4 ms
      * HYBRID (Det=ORT-tiny + limit_type='max' 640,
                Rec=TORCH-CUDA tiny/small):             10.9-15.1 ms (17x faster)

    Why the hybrid beats both pure engines:
      1. RapidOCR's default `Det.limit_type='min'` with `limit_side_len=736`
         upscales small UI crops by up to 19x before running DBNet, burning ~47 ms
         and blurring adjacent letters together. Switching to `limit_type='max'`
         stops upscaling and drops DBNet to 4.5 ms.
      2. DBNet postprocessing (`cv2.findContours` + polygon unclipping) runs on
         CPU anyway; running the tiny 1.7 MB detection graph in ONNX Runtime on
         CPU avoids the PCIe roundtrip and finishes in 4.5-5.3 ms.
      3. SVTR recognition runs over a batch of cropped word boxes (`[N,3,48,320]`).
         Batch cost grows linearly on CPU (40 ms) but runs in parallel on CUDA
         in 5.8 ms (tiny) to 11.0 ms (small, 52 languages incl. Turkish & CJK).
    """

    def __init__(self, *, lang: str = "multi", tier: str = "tiny",
                 confidence: float = 0.3, use_cls: bool = False,
                 engine: str = "auto", limit_side_len: int = 960):
        self.lang = lang
        self.tier = tier
        self.confidence = confidence
        self.use_cls = use_cls
        self.engine_name = engine
        self.limit_side_len = limit_side_len

        self._engines: dict[tuple[str, str, str], object] = {}
        self._load_ms: float | None = None
        self._cache: dict[tuple, TextSet] = {}
        self._calls = 0
        self._total_ms = 0.0

    @staticmethod
    def available() -> bool:
        try:
            import importlib.util
            return importlib.util.find_spec("rapidocr") is not None
        except Exception:
            return False

    @staticmethod
    def _cuda_available() -> bool:
        try:
            import torch
            return bool(torch.cuda.is_available())
        except Exception:
            return False

    def _get_engine(self, lang: str, tier: str):
        key = (lang.lower().strip(), tier.lower().strip(), self.engine_name.lower())
        eng = self._engines.get(key)
        if eng is not None:
            return eng

        try:
            import logging
            from rapidocr import EngineType, ModelType, OCRVersion, RapidOCR
            logging.getLogger("RapidOCR").setLevel(logging.WARNING)
        except ImportError as e:
            raise OCRUnavailable(
                "Screen OCR needs RapidOCR + ONNX Runtime:\n"
                "    pip install 'basira[ocr]'\n"
                "or  pip install 'rapidocr>=3.0.0' onnxruntime"
            ) from e

        lang_key, tier_key, eng_key = key
        use_cuda = (eng_key in ("auto", "cuda", "torch")) and self._cuda_available()

        params: dict[str, object] = {
            "Global.use_cls": bool(self.use_cls),
            "Global.text_score": float(self.confidence),
            "Det.limit_type": "max",
            "Det.limit_side_len": int(self.limit_side_len),
            # Isolated single-character targets (e.g. a lone 'L' or 'T' button)
            # score 0.22-0.28 in DBNet because DBNet is trained on multi-char
            # text lines. Setting box_thresh=0.20 lets single glyphs pass to
            # SVTR, where recognition confidence filters actual noise.
            "Det.thresh": 0.15,
            "Det.box_thresh": 0.20,
            "Det.unclip_ratio": 1.8,
        }

        if lang_key in _V5_SCRIPT_ROUTES:
            # Non-Latin/non-CJK scripts (Arabic, Cyrillic, Korean, Greek,
            # Devanagari, Thai) route to PP-OCRv5 mobile ONNX models.
            rec_lang = _V5_SCRIPT_ROUTES[lang_key]
            params.update({
                "Det.engine_type": EngineType.ONNXRUNTIME,
                "Det.ocr_version": OCRVersion.PPOCRV5,
                "Det.model_type": ModelType.MOBILE,
                "Rec.engine_type": EngineType.ONNXRUNTIME,
                "Rec.ocr_version": OCRVersion.PPOCRV5,
                "Rec.model_type": ModelType.MOBILE,
                "Rec.lang_type": rec_lang,
            })
        else:
            # PP-OCRv6 unified multilingual (52 languages incl. tr, en, ch, japan)
            rec_mtype = {
                "tiny": ModelType.TINY,
                "small": ModelType.SMALL,
                "medium": ModelType.MEDIUM,
                "mobile": ModelType.SMALL,
            }.get(tier_key, ModelType.TINY)
            # 'tiny' has no Japanese kana in its dictionary; upgrade to 'small'
            if rec_mtype == ModelType.TINY and lang_key in ("ja", "jp", "japan"):
                rec_mtype = ModelType.SMALL

            det_eng = (EngineType.TORCH if eng_key == "torch"
                       else EngineType.ONNXRUNTIME)
            rec_eng = (EngineType.TORCH if use_cuda
                       else EngineType.ONNXRUNTIME)

            params.update({
                "Det.engine_type": det_eng,
                "Det.ocr_version": OCRVersion.PPOCRV6,
                "Det.model_type": ModelType.TINY,
                "Rec.engine_type": rec_eng,
                "Rec.ocr_version": OCRVersion.PPOCRV6,
                "Rec.model_type": rec_mtype,
            })
            if use_cuda:
                params["EngineConfig.torch.use_cuda"] = True

        t0 = time.perf_counter()
        cache = _encoder_cache_dir()
        prev_cwd = os.getcwd()
        try:
            os.chdir(cache)
            eng = RapidOCR(params=params)
        finally:
            os.chdir(prev_cwd)
        self._load_ms = (time.perf_counter() - t0) * 1000.0
        self._engines[key] = eng
        return eng

    def status(self) -> dict:
        return {
            "available": self.available(),
            "loaded_engines": [f"{l}/{t}/{e}" for (l, t, e) in self._engines],
            "load_ms": None if self._load_ms is None else round(self._load_ms, 1),
            "lang": self.lang,
            "tier": self.tier,
            "engine": self.engine_name,
            "use_cls": self.use_cls,
            "confidence": self.confidence,
            "calls": self._calls,
            "mean_ms": (round(self._total_ms / self._calls, 1)
                        if self._calls else None),
            "cached_frames": len(self._cache),
        }

    # -- execution ---------------------------------------------------------- #
    def read(self, frame: Frame, *, region: Region | tuple | None = None,
             lang: str | None = None, tier: str | None = None,
             confidence: float | None = None) -> TextSet:
        """Run OCR over `frame` (or a sub-region) and return a cached `TextSet`."""
        use_lang = (lang or self.lang).lower().strip()
        use_tier = (tier or self.tier).lower().strip()
        conf = self.confidence if confidence is None else confidence
        sub = None if region is None else Region(*region)

        ckey = (frame.seq, frame.source, sub, use_lang, use_tier)
        hit = self._cache.get(ckey)
        if hit is not None:
            return hit

        engine = self._get_engine(use_lang, use_tier)
        target = frame if sub is None else frame.crop(sub)
        # RapidOCR accepts an HxWx3 BGR numpy array directly (OpenCV convention),
        # which is a contiguous slice of our BGRA buffer.
        bgr = np.ascontiguousarray(target.bgra[:, :, :3])

        t0 = time.perf_counter()
        out = engine(bgr, use_det=True, use_cls=self.use_cls, use_rec=True)
        elapsed = (time.perf_counter() - t0) * 1000.0
        self._calls += 1
        self._total_ms += elapsed

        items = self._parse(out, target.origin, conf)
        res = TextSet(items, frame.seq, use_lang, elapsed)
        self._cache[ckey] = res
        if len(self._cache) > 16:
            for k in list(self._cache)[:-8]:
                del self._cache[k]
        return res

    @staticmethod
    def _parse(out, origin: Point, min_conf: float) -> list[TextHit]:
        items: list[TextHit] = []
        if out is None:
            return items
        boxes = getattr(out, "boxes", None)
        txts = getattr(out, "txts", None)
        scores = getattr(out, "scores", None)
        if boxes is None or txts is None:
            return items

        for i, txt in enumerate(txts):
            if not txt:
                continue
            sc = float(scores[i]) if scores is not None and i < len(scores) else 1.0
            if sc < min_conf:
                continue
            pts_arr = boxes[i]
            xs = [int(round(float(p[0]))) + origin.x for p in pts_arr]
            ys = [int(round(float(p[1]))) + origin.y for p in pts_arr]
            x0, x1 = min(xs), max(xs)
            y0, y1 = min(ys), max(ys)
            poly = tuple(Point(x, y) for x, y in zip(xs, ys))
            items.append(TextHit(
                text=str(txt),
                confidence=sc,
                box=Region(x0, y0, max(1, x1 - x0), max(1, y1 - y0)),
                polygon=poly,
            ))
        return items
