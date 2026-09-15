#!/usr/bin/env python3
"""Send inputs, capture the game window, and crop named viewport fractions.

    python scripts/probe.py --press D_RIGHT --wait 0.8 --out step.png \
        --crop menu=0.10106,0.76,0.49468,0.14
"""
from __future__ import annotations
import argparse, sys, time
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
import main as pad_driver
from wincap import capture, viewport, crop_fraction


def send(buttons: list[str], pipe_name: str = "pipe1") -> None:
    if not buttons:
        return
    path = pad_driver.user_dir() / "Pipes" / pipe_name
    pipe = pad_driver.Pipe(path)
    pipe.open(timeout=20.0)
    pad = pad_driver.Controller(pipe)
    try:
        for b in buttons:
            pad.tap(b, frames=3, gap=10)
            pad.wait(10)
        pad.neutral()
    finally:
        pipe.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--press", default="", help="comma-separated buttons, in order")
    ap.add_argument("--wait", type=float, default=0.8, help="seconds before capturing")
    ap.add_argument("--out", default="probe.png")
    ap.add_argument("--scale", type=float, default=0.42)
    ap.add_argument("--crop", action="append", default=[],
                    help="name=fx,fy,fw,fh  (repeatable); stacked into <out>")
    args = ap.parse_args()

    buttons = [b.strip().upper() for b in args.press.split(",") if b.strip()]
    if buttons:
        print("pressing:", " ".join(buttons))
        send(buttons)
    time.sleep(args.wait)

    image = capture()
    vp = viewport(image)
    print(f"viewport {list(vp)}")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    if args.crop:
        crops = []
        for spec in args.crop:
            name, _, nums = spec.partition("=")
            frac = [float(v) for v in nums.split(",")]
            c = crop_fraction(image, frac, vp)
            print(f"  {name}: {c.size}")
            crops.append(c)
        W = max(c.width for c in crops); H = sum(c.height + 8 for c in crops)
        sheet = Image.new("RGB", (W, H), (25, 25, 25))
        y = 0
        for c in crops:
            sheet.paste(c, (0, y)); y += c.height + 8
        k = max(1, int(900 / max(W, 1)))
        sheet.resize((W * k, H * k), Image.NEAREST).save(out)
    else:
        image.resize((int(image.width * args.scale),
                      int(image.height * args.scale)), Image.LANCZOS).save(out)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
