"""Hue-based shiny classifier.

The two reference crops (assets/normal.png, assets/shiny.png) differ mostly in
HUE -- pink/magenta vs orange -- while their brightness differs a lot because
they were captured under different lighting.  So the classifier works on a hue
histogram of "subject" pixels (saturated and bright enough to carry colour
information) and ignores value entirely.

Two refinements, both measured against the reference crops (see `hunt.py
selftest`):

* Colour cast removal.  A Shadow Pokemon's purple aura tints the whole crop and
  can drag orange hues far enough toward magenta to read as a normal Flaaffy --
  the one failure direction that actually costs you a shiny.  Subtracting the
  per-channel black level and then grey-world balancing removes that cast and
  classifies every aura-tinted test case correctly.
* Discriminative bin weighting.  Bins where the two references agree (shared
  background, dark outlines) carry no information, so each bin is weighted by
  how much the references disagree there.  The comparison focuses itself on the
  pink-vs-orange difference without any hardcoded hue ranges.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

BINS = 36  # 10 degrees per bin
WHITE_BALANCE_MODES = ("none", "grey", "black+grey")


def white_balance(image: Image.Image, mode: str = "black+grey") -> Image.Image:
    """Remove a colour cast so hue survives the Shadow aura and stage lighting."""
    if mode == "none":
        return image
    if mode not in WHITE_BALANCE_MODES:
        raise ValueError(f"unknown white_balance {mode!r}; use {WHITE_BALANCE_MODES}")
    arr = np.asarray(image.convert("RGB"), dtype=np.float32)
    flat = arr.reshape(-1, 3)
    if mode == "black+grey":
        # Additive tints (an aura glowing over the model) shift the black point;
        # a multiplicative correction alone cannot undo them.
        arr = np.clip(arr - np.percentile(flat, 5, axis=0), 0, None)
        flat = arr.reshape(-1, 3)
    means = flat.mean(axis=0)
    arr = arr / np.maximum(means, 1e-6) * means.mean()
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


def to_hsv(image: Image.Image) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (hue 0-360, saturation 0-1, value 0-1) arrays."""
    arr = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    mx, mn = arr.max(-1), arr.min(-1)
    delta = mx - mn
    value = mx
    sat = np.where(mx > 1e-6, delta / np.maximum(mx, 1e-6), 0.0)
    hue = np.zeros_like(mx)
    nz = delta > 1e-6
    sel = (mx == r) & nz
    hue[sel] = ((g - b)[sel] / delta[sel]) % 6
    sel = (mx == g) & nz
    hue[sel] = ((b - r)[sel] / delta[sel]) + 2
    sel = (mx == b) & nz
    hue[sel] = ((r - g)[sel] / delta[sel]) + 4
    return hue * 60.0, sat, value


def smooth_circular(hist: np.ndarray) -> np.ndarray:
    """Blur across the hue circle so a few degrees of drift don't matter."""
    return 0.25 * np.roll(hist, 1) + 0.5 * hist + 0.25 * np.roll(hist, -1)


def hue_histogram(image: Image.Image, sat_min: float, val_min: float,
                  bins: int = BINS) -> tuple[np.ndarray, int]:
    """Normalised hue histogram of subject pixels, plus the subject pixel count."""
    hue, sat, val = to_hsv(image)
    subject = (sat >= sat_min) & (val >= val_min)
    count = int(subject.sum())
    hist = smooth_circular(
        np.histogram(hue[subject], bins=bins, range=(0.0, 360.0))[0].astype(np.float64)
    )
    total = hist.sum()
    if total > 0:
        hist /= total
    return hist, count


@dataclass
class Verdict:
    label: str                      # shiny | normal | uncertain | no-subject
    margin: float                   # -1 (certainly normal) .. +1 (certainly shiny)
    scores: dict[str, float] = field(default_factory=dict)
    subject_px: int = 0

    @property
    def decisive(self) -> bool:
        return self.label in ("shiny", "normal")

    def __str__(self) -> str:
        parts = " ".join(f"{k}={v:.4f}" for k, v in sorted(self.scores.items()))
        return f"{self.label:<10} margin={self.margin:+.3f} px={self.subject_px:<6} {parts}"


class Classifier:
    """Decides shiny vs normal, and says so only when it is actually sure.

    Every ambiguous outcome becomes "uncertain" rather than "normal": a false
    stop costs you a glance at a screenshot, a false "normal" costs you the
    shiny for good.
    """

    def __init__(self, references: dict[str, str | Path], sat_min: float = 0.25,
                 val_min: float = 0.20, min_subject_px: int = 150,
                 margin: float = 0.15, min_score: float = 0.030,
                 white_balance_mode: str = "black+grey", bins: int = BINS):
        self.sat_min = sat_min
        self.val_min = val_min
        self.min_subject_px = min_subject_px
        self.margin = margin
        self.min_score = min_score
        self.white_balance_mode = white_balance_mode
        self.bins = bins

        self.refs: dict[str, np.ndarray] = {}
        for label, path in references.items():
            path = Path(path)
            if not path.is_file():
                raise SystemExit(f"reference image not found: {path}")
            hist, count = self._histogram(Image.open(path))
            if count < min_subject_px:
                raise SystemExit(
                    f"reference {path} has only {count} subject pixels; "
                    "lower detect.sat_min / detect.min_subject_px or re-crop it")
            self.refs[label] = hist
        if set(self.refs) != {"normal", "shiny"}:
            raise SystemExit("references must be exactly 'normal' and 'shiny'")

        self.weights = np.abs(self.refs["shiny"] - self.refs["normal"])
        if self.weights.sum() <= 0:
            raise SystemExit("the two references are identical in hue; re-crop them")
        self.weights /= self.weights.sum()

    def _histogram(self, image: Image.Image) -> tuple[np.ndarray, int]:
        image = white_balance(image, self.white_balance_mode)
        return hue_histogram(image, self.sat_min, self.val_min, self.bins)

    def classify(self, image: Image.Image) -> Verdict:
        hist, count = self._histogram(image)
        if count < self.min_subject_px:
            return Verdict("no-subject", 0.0, {}, count)
        scores = {
            label: float((self.weights * np.minimum(hist, ref)).sum())
            for label, ref in self.refs.items()
        }
        best = max(scores.values())
        total = scores["shiny"] + scores["normal"]
        margin = 0.0 if total <= 0 else (scores["shiny"] - scores["normal"]) / total

        if best < self.min_score:
            # Neither reference really matches -- wrong frame, wrong region, or
            # an effect we have never seen.  Do not guess.
            return Verdict("uncertain", margin, scores, count)
        if margin >= self.margin:
            return Verdict("shiny", margin, scores, count)
        if margin <= -self.margin:
            return Verdict("normal", margin, scores, count)
        return Verdict("uncertain", margin, scores, count)
