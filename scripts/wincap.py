#!/usr/bin/env python3
"""Capture the Dolphin game window and locate the rendered game inside it.

Window-relative, so it does not care where niri puts the window, and the
viewport box lets every region be stored as a fraction of the game area
instead of absolute screen pixels -- which survives resizing and fullscreen.

    python scripts/wincap.py out.png            # capture + print viewport
    python scripts/wincap.py out.png --crop 0.69,0.11,0.09,0.03
"""
from __future__ import annotations
import argparse, json, subprocess, sys, time
from pathlib import Path
import numpy as np
from PIL import Image


def game_window_id() -> int:
    out = subprocess.run(["niri", "msg", "--json", "windows"],
                         capture_output=True, check=True).stdout
    best = None
    for w in json.loads(out):
        if "dolphin" not in (w.get("app_id") or "").lower():
            continue
        title = w.get("title") or ""
        # the render window carries the game name; the plain one is the GUI
        score = (2 if "|" in title else 0) + (1 if "Pokemon" in title else 0)
        if best is None or score > best[0]:
            best = (score, w["id"])
    if best is None:
        raise SystemExit("no Dolphin window found")
    return best[1]


def capture(window_id: int | None = None) -> Image.Image:
    wid = window_id or game_window_id()
    # niri hands the shot over via the clipboard asynchronously; without
    # clearing first, wl-paste happily returns the PREVIOUS capture and you
    # silently analyse a stale frame.
    subprocess.run(["wl-copy", "--clear"], capture_output=True)
    subprocess.run(["niri", "msg", "action", "screenshot-window",
                    "--id", str(wid), "-d", "false"],
                   capture_output=True, check=True)
    data = b""
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        data = subprocess.run(["wl-paste"], capture_output=True).stdout
        if data:
            break
        time.sleep(0.1)
    if not data:
        raise SystemExit("screenshot never reached the clipboard")
    return Image.open(__import__("io").BytesIO(data)).convert("RGB")


def viewport(image: Image.Image, threshold: int = 14) -> tuple[int, int, int, int]:
    """Bounding box of the rendered game inside the letterboxed window."""
    a = np.asarray(image.convert("L"))
    rows = np.where(a.max(axis=1) > threshold)[0]
    cols = np.where(a.max(axis=0) > threshold)[0]
    if not len(rows) or not len(cols):
        raise SystemExit("window looks entirely black")
    return (int(cols[0]), int(rows[0]),
            int(cols[-1] - cols[0] + 1), int(rows[-1] - rows[0] + 1))


def crop_fraction(image: Image.Image, frac, vp=None) -> Image.Image:
    vx, vy, vw, vh = vp or viewport(image)
    fx, fy, fw, fh = frac
    x, y = round(vx + fx * vw), round(vy + fy * vh)
    w, h = round(fw * vw), round(fh * vh)
    return image.crop((x, y, x + w, y + h))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--crop", help="fx,fy,fw,fh as viewport fractions")
    ap.add_argument("--scale", type=float, default=1.0)
    args = ap.parse_args()
    image = capture()
    vp = viewport(image)
    print(f"window {image.size[0]}x{image.size[1]}  viewport {list(vp)}  "
          f"aspect {vp[2]/vp[3]:.3f}")
    if args.crop:
        image = crop_fraction(image, [float(v) for v in args.crop.split(",")], vp)
        print(f"crop {image.size[0]}x{image.size[1]}")
    if args.scale != 1.0:
        image = image.resize((max(1, int(image.width * args.scale)),
                              max(1, int(image.height * args.scale))), Image.LANCZOS)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    image.save(args.out)
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
