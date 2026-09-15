#!/usr/bin/env python3
"""Record your real controller and translate it into a replayable route.

pygame reads the physical pad; every input is forwarded down the Dolphin pipe as
it happens, so the pad drives the game through the same transport the hunt will
replay later.  Port 1 stays bound to the pipe throughout -- no rebinding, and
what you record is exactly what plays back.

    python scripts/record.py calibrate          # teach it your pad, once
    python scripts/record.py record -o walk.toml
    python scripts/record.py record -o walk.py --format py

Paste the TOML steps into hunt.toml's [route] boot list, or run the .py with
`python main.py walk.py`.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main as pad_driver  # noqa: E402

MAP_PATH = ROOT / "controller_map.json"
GC_BUTTONS = ("A", "B", "X", "Y", "Z", "START", "L", "R",
              "D_UP", "D_DOWN", "D_LEFT", "D_RIGHT")
FPS = 60.0
STICK_EPS = 0.08        # ignore stick moves smaller than this
DEADZONE = 0.15


def load_pygame():
    import os
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    # SDL drops joystick events when its app is not in the foreground, and the
    # whole point here is to play in Dolphin while this records in the
    # background.  Without this the recorder goes deaf the moment you click
    # into the game.
    os.environ["SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS"] = "1"
    import pygame
    pygame.init()
    pygame.joystick.init()
    if pygame.joystick.get_count() == 0:
        raise SystemExit("no controller found -- is the pad plugged in?")
    stick = pygame.joystick.Joystick(0)
    stick.init()
    print(f"controller: {stick.get_name()}  "
          f"({stick.get_numbuttons()} buttons, {stick.get_numaxes()} axes, "
          f"{stick.get_numhats()} hats)")
    return pygame, stick


# --------------------------------------------------------------------------
def cmd_calibrate(args) -> int:
    pygame, _ = load_pygame()
    mapping = {"buttons": {}, "hats": {}, "axes": {}}

    print("\nPress each input when prompted. Enter to skip, Ctrl+C to stop.\n")
    for name in GC_BUTTONS:
        print(f"  press {name:<9} ", end="", flush=True)
        pygame.event.clear()
        found = None
        while found is None:
            for event in pygame.event.get():
                if event.type == pygame.JOYBUTTONDOWN:
                    found = ("button", event.button)
                elif event.type == pygame.JOYHATMOTION and event.value != (0, 0):
                    found = ("hat", [event.hat, list(event.value)])
            if sys.stdin in select_ready():
                sys.stdin.readline()
                found = ("skip", None)
            time.sleep(0.01)
        kind, value = found
        if kind == "button":
            mapping["buttons"][str(value)] = name
            print(f"-> button {value}")
        elif kind == "hat":
            mapping["hats"][f"{value[0]}:{value[1][0]},{value[1][1]}"] = name
            print(f"-> hat {value[0]} {tuple(value[1])}")
        else:
            print("-> skipped")
        # wait for release so one press does not answer two prompts
        time.sleep(0.35)
        pygame.event.clear()

    for stick_name, prompts in (("main", ("right", "up")), ("c", ("right", "up"))):
        for direction in prompts:
            print(f"  push {stick_name.upper()}-stick {direction:<6} ",
                  end="", flush=True)
            pygame.event.clear()
            axis = None
            while axis is None:
                for event in pygame.event.get():
                    if event.type == pygame.JOYAXISMOTION and abs(event.value) > 0.6:
                        axis = (event.axis, 1 if event.value > 0 else -1)
                if sys.stdin in select_ready():
                    sys.stdin.readline()
                    axis = ("skip", 0)
                time.sleep(0.01)
            if axis[0] == "skip":
                print("-> skipped")
            else:
                key = f"{stick_name}_{'x' if direction == 'right' else 'y'}"
                mapping["axes"][key] = {"axis": axis[0], "sign": axis[1]}
                print(f"-> axis {axis[0]} sign {axis[1]:+d}")
            time.sleep(0.35)
            pygame.event.clear()

    MAP_PATH.write_text(json.dumps(mapping, indent=2))
    print(f"\nsaved {MAP_PATH}")
    return 0


def select_ready():
    import select
    ready, _, _ = select.select([sys.stdin], [], [], 0)
    return ready


# --------------------------------------------------------------------------
def cmd_record(args) -> int:
    if not MAP_PATH.is_file():
        raise SystemExit(f"no {MAP_PATH.name}; run: python scripts/record.py calibrate")
    mapping = json.loads(MAP_PATH.read_text())
    buttons = {int(k): v for k, v in mapping["buttons"].items()}
    hats = mapping["hats"]
    axes = mapping["axes"]
    blocked = {b.strip().upper() for b in args.block.split(",") if b.strip()}

    pygame, _ = load_pygame()
    pipe_path = pad_driver.user_dir() / "Pipes" / args.pipe
    pipe = pad_driver.Pipe(pipe_path, dry_run=args.no_passthrough)
    pipe.open(timeout=None if args.no_passthrough else 60.0)
    pad = pad_driver.Controller(pipe)

    timeline: list[tuple[float, str, object]] = []
    held: set[str] = set()
    last_stick = {"main": (0.0, 0.0), "c": (0.0, 0.0)}
    raw = {}

    if blocked:
        print(f"blocked (not forwarded): {', '.join(sorted(blocked))}")
    print("\nRecording. Focus Dolphin and play. Ctrl+C here when done.\n")
    start = time.perf_counter()
    deadline = start + args.seconds if args.seconds else None

    def emit(kind, payload):
        timeline.append((time.perf_counter() - start, kind, payload))

    def press(name):
        if name in blocked or name in held:
            return
        held.add(name)
        emit("down", name)
        pad.press(name)

    def release(name):
        if name in blocked or name not in held:
            return
        held.discard(name)
        emit("up", name)
        pad.release(name)

    try:
        while deadline is None or time.perf_counter() < deadline:
            for event in pygame.event.get():
                if event.type == pygame.JOYBUTTONDOWN and event.button in buttons:
                    press(buttons[event.button])
                elif event.type == pygame.JOYBUTTONUP and event.button in buttons:
                    release(buttons[event.button])
                elif event.type == pygame.JOYHATMOTION:
                    for key, name in hats.items():
                        hat, vector = key.split(":")
                        want = tuple(int(v) for v in vector.split(","))
                        if int(hat) == event.hat:
                            (press if event.value == want else release)(name)
                elif event.type == pygame.JOYAXISMOTION:
                    raw[event.axis] = event.value

            for stick_name in ("main", "c"):
                cfg_x = axes.get(f"{stick_name}_x")
                cfg_y = axes.get(f"{stick_name}_y")
                if not cfg_x or not cfg_y:
                    continue
                x = raw.get(cfg_x["axis"], 0.0) * cfg_x["sign"]
                y = raw.get(cfg_y["axis"], 0.0) * cfg_y["sign"]
                x = 0.0 if abs(x) < DEADZONE else round(x, 2)
                y = 0.0 if abs(y) < DEADZONE else round(y, 2)
                px, py = last_stick[stick_name]
                if abs(x - px) > STICK_EPS or abs(y - py) > STICK_EPS:
                    last_stick[stick_name] = (x, y)
                    emit("stick" if stick_name == "main" else "c_stick", (x, y))
                    (pad.stick if stick_name == "main" else pad.c_stick)(x, y)
            time.sleep(1 / 240)
    except KeyboardInterrupt:
        pass

    for name in sorted(held):
        emit("up", name)
    pad.neutral()
    pipe.close()

    duration = timeline[-1][0] if timeline else 0.0
    print(f"\ncaptured {len(timeline)} events over {duration:.1f}s")
    if not timeline:
        print("nothing recorded -- did the pad events reach pygame?")
        return 1

    text = (render_py if args.format == "py" else render_toml)(timeline)
    if args.out:
        Path(args.out).write_text(text)
        print(f"wrote {args.out}")
    else:
        print()
        print(text)
    return 0


# --------------------------------------------------------------------------
def to_steps(timeline) -> list[tuple[str, object, int]]:
    """Insert wait gaps between events, in frames of real time."""
    steps = []
    previous = 0.0
    for at, kind, payload in timeline:
        gap = round((at - previous) * FPS)
        if gap > 0:
            steps.append(("wait", gap, 0))
        steps.append((kind, payload, 0))
        previous = at
    return steps


def render_toml(timeline) -> str:
    lines = [
        "    # recorded with scripts/record.py -- paste into hunt.toml [route] boot",
        "    # remember to leave a { jitter = [0, 150] } before the battle triggers",
    ]
    for kind, payload, _ in to_steps(timeline):
        if kind == "wait":
            lines.append(f"    {{ wait = {payload} }},")
        elif kind in ("down", "up"):
            lines.append(f'    {{ {kind} = "{payload}" }},')
        elif kind == "stick":
            lines.append(f"    {{ stick_set = [{payload[0]}, {payload[1]}] }},")
        elif kind == "c_stick":
            lines.append(f"    {{ c_stick_set = [{payload[0]}, {payload[1]}] }},")
    return "\n".join(lines)


def render_py(timeline) -> str:
    lines = ['"""Recorded with scripts/record.py -- run: python main.py <this file>"""', ""]
    for kind, payload, _ in to_steps(timeline):
        if kind == "wait":
            lines.append(f"wait({payload})")
        elif kind == "down":
            lines.append(f'press("{payload}")')
        elif kind == "up":
            lines.append(f'release("{payload}")')
        elif kind == "stick":
            lines.append(f"stick({payload[0]}, {payload[1]})")
        elif kind == "c_stick":
            lines.append(f"c_stick({payload[0]}, {payload[1]})")
    return "\n".join(lines) + "\n"


def cmd_monitor(args) -> int:
    """Print events as they arrive -- click into another window to prove
    background delivery works."""
    pygame, _ = load_pygame()
    mapping = json.loads(MAP_PATH.read_text()) if MAP_PATH.is_file() else {
        "buttons": {}, "hats": {}, "axes": {}}
    buttons = {int(k): v for k, v in mapping["buttons"].items()}
    print(f"\nwatching for {args.seconds:.0f}s -- focus Dolphin and press things\n")
    deadline = time.perf_counter() + args.seconds
    seen = 0
    while time.perf_counter() < deadline:
        for event in pygame.event.get():
            if event.type == pygame.JOYBUTTONDOWN:
                seen += 1
                print(f"  button {event.button} -> {buttons.get(event.button, '(unmapped)')}")
            elif event.type == pygame.JOYBUTTONUP:
                seen += 1
            elif event.type == pygame.JOYHATMOTION:
                seen += 1
                print(f"  hat {event.hat} = {event.value}")
            elif event.type == pygame.JOYAXISMOTION and abs(event.value) > 0.5:
                seen += 1
                print(f"  axis {event.axis} = {event.value:+.2f}")
        time.sleep(1 / 240)
    print(f"\n{seen} events seen")
    if seen == 0:
        print("none at all -- pad unplugged, or SDL background events blocked")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("calibrate", help="map your pad's buttons and axes")
    p = sub.add_parser("monitor", help="print events live, to check they arrive")
    p.add_argument("--seconds", type=float, default=20.0)
    p = sub.add_parser("record", help="record and translate a sequence")
    p.add_argument("-o", "--out")
    p.add_argument("--format", choices=["toml", "py"], default="toml")
    p.add_argument("--seconds", type=float, default=0.0, help="auto-stop after N seconds")
    p.add_argument("--pipe", default="pipe1")
    p.add_argument("--block", default="Z",
                   help="buttons to read but NOT forward (default Z, the reset hotkey)")
    p.add_argument("--no-passthrough", action="store_true",
                   help="record only, do not drive the game")
    args = parser.parse_args(argv)
    return {"calibrate": cmd_calibrate, "record": cmd_record,
            "monitor": cmd_monitor}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
