"""Screen-state detection by template matching on one fixed UI region.

Colosseum's battle UI sits at fixed screen positions, so "which state am I in"
is a nearest-template lookup, not a perception problem.  One region (the menu /
text box area) is captured and compared against a reference crop per state.

Scoring is zero-mean normalised cross-correlation.  Plain pixel difference
scores everything highly -- two different dark menu boxes are mostly dark
either way -- while ZNCC keys on structure and is unmoved by brightness or
contrast shifts, which is what separates "Fight menu" from "Bag menu".

Templates live in assets/states/<name>.png; add them with
`python scripts/hunt.py state-add <name>`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from vision import to_hsv

SIZE = (64, 64)   # everything is compared at this resolution


def fingerprint(image: Image.Image, size: tuple[int, int] = SIZE) -> np.ndarray:
    """Zero-mean, unit-variance greyscale thumbnail."""
    small = image.convert("L").resize(size, Image.BILINEAR)
    arr = np.asarray(small, dtype=np.float64)
    arr -= arr.mean()
    norm = arr.std()
    return arr / norm if norm > 1e-6 else arr


def zncc(a: np.ndarray, b: np.ndarray) -> float:
    """Zero-mean normalised cross-correlation of two fingerprints, -1..1."""
    return float((a * b).mean())


@dataclass
class Match:
    name: str
    score: float
    runner_up: str = ""
    runner_up_score: float = 0.0

    @property
    def known(self) -> bool:
        return self.name != "unknown"

    @property
    def margin(self) -> float:
        return self.score - self.runner_up_score

    def __str__(self) -> str:
        tail = (f"  (next: {self.runner_up} {self.runner_up_score:.3f},"
                f" margin {self.margin:+.3f})") if self.runner_up else ""
        return f"{self.name:<18} {self.score:.3f}{tail}"


class StateSet:
    def __init__(self, directory: str | Path, threshold: float = 0.90):
        self.directory = Path(directory)
        self.threshold = threshold
        self.templates: dict[str, np.ndarray] = {}
        if self.directory.is_dir():
            for path in sorted(self.directory.glob("*.png")):
                self.templates[path.stem] = fingerprint(Image.open(path))

    def __len__(self) -> int:
        return len(self.templates)

    def identify(self, image: Image.Image) -> Match:
        if not self.templates:
            return Match("unknown", 0.0)
        probe = fingerprint(image)
        ranked = sorted(((zncc(probe, ref), name)
                         for name, ref in self.templates.items()), reverse=True)
        score, name = ranked[0]
        runner_up, runner_up_score = ("", 0.0)
        if len(ranked) > 1:
            runner_up_score, runner_up = ranked[1]
        if score < self.threshold:
            return Match("unknown", score, name, runner_up_score)
        return Match(name, score, runner_up, runner_up_score)

    def confusion(self) -> list[tuple[str, str, float]]:
        """Pairwise similarity between templates, worst (most confusable) first.

        Two states that score close to each other will be mixed up at runtime,
        so this is worth checking whenever a template is added.
        """
        names = list(self.templates)
        pairs = []
        for i, first in enumerate(names):
            for second in names[i + 1:]:
                pairs.append((first, second,
                              zncc(self.templates[first], self.templates[second])))
        return sorted(pairs, key=lambda row: -row[2])


def hue_fraction(image: Image.Image, lo: float, hi: float,
                 sat_min: float = 0.45, val_min: float = 0.30) -> float:
    """Fraction of pixels whose hue falls in [lo, hi] (wrapping at 360).

    Menu cursors are flat saturated colours nothing else in the UI uses, so
    counting them is immune to the semi-transparent menus that defeat
    structural matching.
    """
    hue, sat, val = to_hsv(image)
    strong = (sat >= sat_min) & (val >= val_min)
    inside = (hue >= lo) & (hue <= hi) if lo <= hi else (hue >= lo) | (hue <= hi)
    return float((strong & inside).mean())


def hp_fraction(image: Image.Image) -> float:
    """How full a Pokemon HP bar is, 0-1, by counting filled columns.

    The fill runs green -> yellow -> red as HP drops; all three are saturated
    with little blue, while the empty remainder and the bar's frame are not.
    Reads 0.993 on a full bar.
    """
    a = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
    mx = a.max(-1)
    mn = a.min(-1)
    sat = np.where(mx > 1e-6, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    fill = (sat > 0.45) & (mx > 0.30) & (a[..., 2] < 0.55 * mx)
    return float(fill.any(axis=0).mean())


def red_fraction(image: Image.Image) -> float:
    """Fraction of pixels that are the menu cursor's saturated red.

    Colosseum's battle menu is SEMI-TRANSPARENT -- the 3D scene shows through
    it -- so structural matching over the menu box swings with whatever
    animates behind. The cursor arrow is a saturated red nothing else in the
    UI uses, and counting those pixels is unaffected by the background.
    Measured: 0.23-0.26 with the cursor present, 0.000 without.
    """
    a = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    mx, mn = a.max(-1), a.min(-1)
    sat = np.where(mx > 1e-6, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    return float(((r > 0.35) & (r > g * 1.8) & (r > b * 1.8) & (sat > 0.5)).mean())


@dataclass
class Check:
    """One yes/no question about a fixed fraction of the game viewport."""
    name: str
    region: list[float]          # fx, fy, fw, fh of the rendered game area
    mode: str                    # "zncc" (structure) | "red" (cursor colour)
    threshold: float
    template: np.ndarray | None = None
    hue_range: tuple[float, float] = (0.0, 360.0)

    def score(self, screen: Image.Image, vp=None) -> float:
        from wincap import crop_fraction
        crop = crop_fraction(screen, self.region, vp)
        if self.mode == "red":
            return red_fraction(crop)
        if self.mode == "hue":
            return hue_fraction(crop, *self.hue_range)
        if self.mode == "hpbar":
            return hp_fraction(crop)
        return zncc(fingerprint(crop), self.template)


class CheckSet:
    """Independent predicates, each on its own slice of the game viewport.

    Regions are fractions of the rendered game area, not screen pixels, so
    they survive the window moving, resizing or going fullscreen. Templates
    are whole-window grabs and are cropped by the same fraction against their
    own viewport.
    """

    def __init__(self, config: dict, root: Path, default_threshold: float = 0.85):
        from wincap import viewport
        self.checks: dict[str, Check] = {}
        for name, conf in config.items():
            if not isinstance(conf, dict) or "region" not in conf:
                continue
            region = [float(v) for v in conf["region"]]
            if any(v > 1.0 for v in region):
                raise SystemExit(
                    f"check '{name}': region {region} looks like screen pixels; "
                    "regions are viewport fractions (0-1) now")
            mode = conf.get("mode", "zncc")
            threshold = float(conf.get("threshold",
                                       0.05 if mode in ("red", "hue")
                                       else default_threshold))
            if mode == "hpbar":
                threshold = float(conf.get("threshold", 0.35))
            hue_range = tuple(conf.get("hue_range", (0.0, 360.0)))
            template = None
            if mode not in ("red", "hue", "hpbar"):
                path = Path(root) / conf["template"]
                if not path.is_file():
                    raise SystemExit(f"check '{name}': no template at {path}")
                image = Image.open(path).convert("RGB")
                from wincap import crop_fraction
                template = fingerprint(crop_fraction(image, region, viewport(image)))
            self.checks[name] = Check(name, region, mode, threshold, template,
                                      hue_range)

    def __len__(self) -> int:
        return len(self.checks)

    def score(self, name: str, screen: Image.Image, vp=None) -> float:
        if name not in self.checks:
            raise SystemExit(f"no check named '{name}'")
        return self.checks[name].score(screen, vp)

    def present(self, name: str, screen: Image.Image, vp=None) -> bool:
        check = self.checks[name]
        return check.score(screen, vp) >= check.threshold

    def report(self, screen: Image.Image) -> list[tuple[str, float, bool]]:
        from wincap import viewport
        vp = viewport(screen)
        out = []
        for name, c in self.checks.items():
            score = c.score(screen, vp)
            out.append((name, score, score >= c.threshold))
        return out
