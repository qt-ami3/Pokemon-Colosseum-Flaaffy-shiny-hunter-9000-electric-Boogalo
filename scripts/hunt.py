#!/usr/bin/env python3
"""Soft-reset shiny hunt for St. Performer Diogo's Shadow Flaaffy.

Loop: soft reset -> replay the route back to the battle -> screenshot the
region where Flaaffy stands -> classify it -> reset again if it's a dud.
The hunt stops on a shiny, and also stops whenever it is not sure, because a
missed shiny is unrecoverable while a false stop costs one glance.

    python scripts/hunt.py selftest             # prove the classifier works
    python scripts/hunt.py snap                 # full screenshot, for calibration
    python scripts/hunt.py crop --region X,Y,W,H --from snap.png
    python scripts/hunt.py watch                # live verdicts on the region
    python scripts/hunt.py route                # replay the route, no hunting
    python scripts/hunt.py run                  # the hunt
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import random
import shutil
import subprocess
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import main as pad_driver                      # noqa: E402
from vision import Classifier, Verdict         # noqa: E402
from states import StateSet, CheckSet          # noqa: E402
from wincap import capture as wincap_capture, crop_fraction  # noqa: E402

CONFIG_PATH = ROOT / "hunt.toml"


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------
def load_config(path: Path = CONFIG_PATH) -> dict:
    if not path.is_file():
        raise SystemExit(f"missing config: {path}")
    with path.open("rb") as fh:
        return tomllib.load(fh)


def build_classifier(cfg: dict) -> Classifier:
    det = cfg["detect"]
    refs = {k: ROOT / v for k, v in cfg["references"].items()}
    return Classifier(
        refs,
        sat_min=det["sat_min"],
        val_min=det["val_min"],
        min_subject_px=det["min_subject_px"],
        margin=det["margin"],
        min_score=det["min_score"],
        white_balance_mode=det["white_balance"],
    )


# --------------------------------------------------------------------------
# capture
# --------------------------------------------------------------------------
def capture(region=None, output: str = "") -> Image.Image:
    """Grab the Dolphin game window; crop to a viewport fraction if given.

    Window-relative rather than screen-relative: niri reports no absolute
    window position on a scrolling layout, so any move, resize or fullscreen
    would silently invalidate every screen coordinate.
    """
    image = wincap_capture()
    if region:
        return crop_fraction(image, [float(v) for v in region])
    return image


def parse_region(text: str) -> list[int]:
    parts = [p for p in text.replace("x", ",").replace(" ", ",").split(",") if p]
    if len(parts) != 4:
        raise SystemExit("region must be X,Y,W,H")
    return [int(p) for p in parts]


def config_region(cfg: dict) -> list[int] | None:
    region = cfg["capture"]["region"]
    return list(region) if region else None


# --------------------------------------------------------------------------
# route execution
# --------------------------------------------------------------------------
def build_checks(cfg: dict) -> CheckSet:
    return CheckSet(cfg.get("checks", {}), ROOT / "assets",
                    cfg.get("states", {}).get("threshold", 0.85))


class Tee:
    """Mirror stdout to a log file, line-buffered so `tail -f` keeps up."""

    def __init__(self, path: Path):
        self.stream = sys.__stdout__
        self.file = path.open("a", buffering=1)

    def write(self, text: str) -> int:
        self.stream.write(text)
        self.file.write(text)
        return len(text)

    def flush(self) -> None:
        self.stream.flush()
        self.file.flush()

    def close(self) -> None:
        try:
            self.file.close()
        except Exception:
            pass


def stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


class RouteDesync(Exception):
    """A checkpoint was not reached -- the replay drifted off course."""


_STATE_CACHE: dict[str, StateSet] = {}
_CHECK_CACHE: dict[int, CheckSet] = {}


def cached_checks(cfg: dict) -> CheckSet:
    key = id(cfg)
    if key not in _CHECK_CACHE:
        _CHECK_CACHE[key] = build_checks(cfg)
    return _CHECK_CACHE[key]


def cached_states(cfg: dict) -> StateSet:
    conf = cfg.get("states", {})
    key = f"{conf.get('dir', 'assets/states')}|{conf.get('threshold', 0.90)}"
    if key not in _STATE_CACHE:
        _STATE_CACHE[key] = build_states(cfg)
    return _STATE_CACHE[key]


def wait_for_state(cfg: dict, wanted: str, timeout_s: float, log=print) -> None:
    """Block until `wanted` is on screen, or raise RouteDesync."""
    known = cached_states(cfg)
    if wanted not in known.templates:
        raise SystemExit(f"checkpoint '{wanted}' has no template in {known.directory}")
    region = states_region(cfg)
    poll = float(cfg.get("states", {}).get("poll_interval_s", 0.25))
    deadline = time.monotonic() + timeout_s
    best = None
    while time.monotonic() < deadline:
        match = known.identify(capture(region, cfg["capture"]["output"]))
        if match.name == wanted:
            return
        best = match
        time.sleep(poll)
    raise RouteDesync(f"expected '{wanted}' within {timeout_s:.0f}s, "
                      f"saw '{best.name if best else 'nothing'}'"
                      f" ({best.score:.3f})" if best else "")


def load_route_file(path: Path) -> list[dict]:
    """Read a route file: recorder output (bare `{ ... },` lines) or a normal
    TOML document with a top-level `steps` array."""
    if not path.is_file():
        raise SystemExit(f"route file not found: {path}")
    text = path.read_text()
    try:
        data = tomllib.loads(text)
        if isinstance(data.get("steps"), list):
            return data["steps"]
    except tomllib.TOMLDecodeError:
        pass
    try:
        return tomllib.loads("steps = [\n" + text + "\n]")["steps"]
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"{path}: not a usable route file ({exc})")


def route_phases(cfg: dict) -> list[tuple[str, list[dict]]]:
    """The attempt as an ordered list of (name, steps), jitter inserted."""
    route = cfg["route"]
    files = route.get("files") or []
    if not files:
        return [("boot", route.get("boot", []))]
    low, high = route.get("jitter", [0, 0])
    before = route.get("jitter_before", "")
    checkpoints = route.get("checkpoints", {})
    phases = []
    for name in files:
        if before and Path(name).name == Path(before).name and high > low:
            phases.append(("jitter", [{"jitter": [low, high]}]))
        steps = load_route_file(ROOT / name)
        expected = checkpoints.get(name) or checkpoints.get(Path(name).stem)
        if expected:
            steps = steps + [{"expect": expected, "timeout_s": 30}]
        phases.append((Path(name).stem, steps))
    return phases


def run_steps(pad: pad_driver.Controller, steps: list[dict], cfg: dict,
              log=print) -> None:
    for step in steps:
        if "comment" in step and len(step) == 1:
            log(f"    - {step['comment']}")
            continue
        if "wait" in step:
            pad.wait(float(step["wait"]))
        elif "until" in step:
            # Menu cursors persist between turns, so counting presses is wrong:
            # press, look, repeat until the screen says we have arrived.
            checks = cached_checks(cfg)
            target = step["until"]
            want_absent = bool(step.get("absent", False))
            buttons = step.get("press")
            buttons = [buttons] if isinstance(buttons, str) else list(buttons or [])
            limit = int(step.get("max", 8))
            settle = float(step.get("settle_frames", 12))
            presses = 0
            # Settle BEFORE the first look. Menus animate in, and checking
            # instantly reads the old screen -- which makes an `until` press
            # its way around a menu it was already sitting on.
            pad.wait(float(step.get("first_settle_frames", settle)))
            while True:
                screen = capture(None)
                score = checks.score(target, screen)
                if (score >= checks.checks[target].threshold) != want_absent:
                    break
                if presses >= limit:
                    if step.get("optional"):
                        break
                    raise RouteDesync(
                        f"'{target}' still "
                        f"{'present' if want_absent else 'not reached'} after "
                        f"{presses} presses of {'/'.join(buttons) or '(nothing)'}")
                if buttons:
                    # Cycle the buttons: a 2x2 menu needs two axes, and
                    # alternating them reaches any cell from any other.
                    button = buttons[presses % len(buttons)]
                    # Log the score that caused the press: a check that is
                    # marginally failing looks identical to one that is
                    # correctly navigating, until you can see the number.
                    log(f"    {target}={score:.3f} "
                        f"(need {checks.checks[target].threshold:.2f}) -> {button}")
                    pad.tap(button, frames=3, gap=3)
                presses += 1
                pad.wait(settle)
            if presses:
                log(f"    {target}: reached after {presses} press(es)")
        elif "require" in step:
            # Screens take a moment to draw, so this polls rather than checking
            # once -- an instant check races the transition and fails while the
            # screen is on its way in.
            checks = cached_checks(cfg)
            target = step["require"]
            timeout = float(step.get("timeout_s", 6.0))
            deadline = time.monotonic() + timeout
            while True:
                if checks.present(target, capture(None)):
                    break
                if time.monotonic() > deadline:
                    raise RouteDesync(
                        f"required check '{target}' not on screen after {timeout:.0f}s")
                time.sleep(0.2)
        elif "press" in step:
            buttons = step["press"]
            buttons = [buttons] if isinstance(buttons, str) else buttons
            pad.press(*buttons)
            pad.wait(float(step.get("frames", 10)))
            pad.release(*buttons)
        elif "tap" in step:
            for _ in range(int(step.get("times", 1))):
                pad.tap(step["tap"], frames=int(step.get("frames", 3)),
                        gap=int(step.get("gap", 20)))
        elif "mash" in step:
            interval = float(step.get("interval", 20))
            total = float(step.get("seconds", 5.0)) * pad.fps
            done = 0.0
            while done < total:
                pad.tap(step["mash"], frames=3, gap=max(interval - 3, 1))
                done += interval
        elif "expect" in step:
            wait_for_state(cfg, step["expect"], float(step.get("timeout_s", 20)), log)
        elif "down" in step:
            buttons = step["down"]
            pad.press(*([buttons] if isinstance(buttons, str) else buttons))
        elif "up" in step:
            buttons = step["up"]
            pad.release(*([buttons] if isinstance(buttons, str) else buttons))
        elif "stick_set" in step:
            pad.stick(*[float(v) for v in step["stick_set"]])
        elif "c_stick_set" in step:
            pad.c_stick(*[float(v) for v in step["c_stick_set"]])
        elif "key" in step:
            mods = step.get("mods", [])
            send_key(step["key"], [mods] if isinstance(mods, str) else mods, cfg)
            pad.wait(float(step.get("frames", 0)))
        elif "jitter" in step:
            low, high = step["jitter"]
            pad.wait(random.uniform(float(low), float(high)))
        elif "stick" in step:
            x, y = step["stick"]
            pad.stick(float(x), float(y))
            pad.wait(float(step.get("frames", 60)))
            pad.stick(0.0, 0.0)
        else:
            raise SystemExit(f"unknown route step: {step}")


# --------------------------------------------------------------------------
# decision over a sampled window
# --------------------------------------------------------------------------
class AttemptResult:
    def __init__(self, verdicts: list[Verdict], frames: list[Image.Image]):
        self.verdicts = verdicts
        self.frames = frames
        self.shiny = [v for v in verdicts if v.label == "shiny"]
        self.normal = [v for v in verdicts if v.label == "normal"]

    def decide(self, decide_cfg: dict) -> str:
        if len(self.shiny) >= decide_cfg["shiny_votes"]:
            return "shiny"
        if not self.shiny and len(self.normal) >= decide_cfg["normal_votes"]:
            return "normal"
        return "uncertain"

    @property
    def best(self) -> Verdict | None:
        decisive = [v for v in self.verdicts if v.decisive]
        pool = decisive or self.verdicts
        return max(pool, key=lambda v: abs(v.margin), default=None)


def sample_window(cfg: dict, clf: Classifier, pad: pad_driver.Controller | None,
                  log=print) -> AttemptResult:
    sample = cfg["sample"]
    region = config_region(cfg)
    if pad is not None and sample["settle_frames"]:
        pad.wait(float(sample["settle_frames"]))
    deadline = time.monotonic() + float(sample["duration_s"])
    verdicts: list[Verdict] = []
    frames: list[Image.Image] = []
    while time.monotonic() < deadline:
        image = capture(region, cfg["capture"]["output"])
        verdict = clf.classify(image)
        verdicts.append(verdict)
        frames.append(image)
        log(f"      {verdict}")
        time.sleep(float(sample["interval_s"]))
    return AttemptResult(verdicts, frames)


# --------------------------------------------------------------------------
# notification
# --------------------------------------------------------------------------
def alert(title: str, body: str, cfg: dict, urgent: bool = True) -> None:
    print("\a", end="", flush=True)
    banner = "=" * 72
    print(f"\n{banner}\n  {title}\n  {body}\n{banner}\n", flush=True)
    if shutil.which("notify-send"):
        subprocess.run(
            ["notify-send", "-u", "critical" if urgent else "normal",
             "-a", "shiny-hunt", title, body],
            check=False)
    command = cfg["decide"].get("notify_command", "")
    if command:
        subprocess.run(command, shell=True, check=False,
                       env={**os.environ, "HUNT_TITLE": title, "HUNT_BODY": body})


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_selftest(args, cfg: dict) -> int:
    """Classify the references and deliberately degraded copies of them."""
    import numpy as np
    from PIL import ImageEnhance, ImageFilter

    clf = build_classifier(cfg)
    print("discriminative hue bins:")
    for i, w in enumerate(clf.weights):
        if w > 0.02:
            print(f"    {i*10:3d}-{i*10+10:3d} deg   weight {w:.3f}")

    def aura(im, k, add):
        a = np.asarray(im.convert("RGB"), dtype=np.float32)
        return Image.fromarray(np.clip(a * k + np.array(add), 0, 255).astype("uint8"))

    def variants(im):
        a = np.asarray(im.convert("RGB")).astype(np.int16)
        rng = np.random.default_rng(0)
        half = a.copy()
        half[:, :a.shape[1] // 2] //= 4
        return {
            "clean": im,
            "dark -40%": ImageEnhance.Brightness(im).enhance(0.6),
            "bright +50%": ImageEnhance.Brightness(im).enhance(1.5),
            "desaturated": ImageEnhance.Color(im).enhance(0.7),
            "blurred": im.filter(ImageFilter.GaussianBlur(1.2)),
            "noisy": Image.fromarray(
                np.clip(a + rng.normal(0, 12, a.shape), 0, 255).astype("uint8")),
            "shadow aura": aura(im, 0.75, (60, 0, 90)),
            "light aura": aura(im, 0.85, (30, 0, 45)),
            "half in shadow": Image.fromarray(half.astype("uint8")),
        }

    failures = 0
    for truth in ("normal", "shiny"):
        source = Image.open(ROOT / cfg["references"][truth])
        print(f"\n== truth: {truth}")
        for name, image in variants(source).items():
            verdict = clf.classify(image)
            ok = verdict.label == truth
            failures += not ok
            print(f"   {name:<16} {verdict}{'' if ok else '   <-- MISCLASSIFIED'}")

    # A blank frame and a screen full of nothing must not be called anything.
    for name, image in (("black frame", Image.new("RGB", (64, 64))),
                        ("grey frame", Image.new("RGB", (64, 64), (128, 128, 128)))):
        verdict = clf.classify(image)
        ok = verdict.label in ("no-subject", "uncertain")
        failures += not ok
        print(f"\n   {name:<16} {verdict}{'' if ok else '   <-- SHOULD BE INCONCLUSIVE'}")

    print(f"\n{'PASS' if not failures else f'FAIL ({failures})'}")
    return 1 if failures else 0


def cmd_snap(args, cfg: dict) -> int:
    image = capture(None, cfg["capture"]["output"])
    out = Path(args.out or "snap.png")
    image.save(out)
    print(f"{out}  {image.width}x{image.height}")
    print("Open it, find Flaaffy's head, then:")
    print(f"  python scripts/hunt.py crop --region X,Y,W,H --from {out}")
    return 0


def cmd_crop(args, cfg: dict) -> int:
    region = parse_region(args.region) if args.region else config_region(cfg)
    if not region:
        raise SystemExit("no region given and capture.region is empty in hunt.toml")
    x, y, w, h = region
    if args.source:
        image = Image.open(args.source).convert("RGB").crop((x, y, x + w, y + h))
    else:
        image = capture(region, cfg["capture"]["output"])
    out = Path(args.out or "crop.png")
    image.save(out)
    print(f"{out}  {image.width}x{image.height}")
    print(build_classifier(cfg).classify(image))
    return 0


def cmd_classify(args, cfg: dict) -> int:
    clf = build_classifier(cfg)
    for path in args.paths:
        print(f"{path}: {clf.classify(Image.open(path))}")
    return 0


def cmd_watch(args, cfg: dict) -> int:
    """Live verdicts on the capture region -- the calibration loop."""
    clf = build_classifier(cfg)
    region = config_region(cfg)
    if not region:
        print("warning: capture.region is empty, classifying the whole screen",
              file=sys.stderr)
    deadline = time.monotonic() + args.seconds
    try:
        while time.monotonic() < deadline:
            print(clf.classify(capture(region, cfg["capture"]["output"])), flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    return 0


def states_region(cfg: dict) -> list[int] | None:
    region = cfg.get("states", {}).get("region")
    return list(region) if region else None


def build_states(cfg: dict) -> StateSet:
    conf = cfg.get("states", {})
    return StateSet(ROOT / conf.get("dir", "assets/states"),
                    threshold=conf.get("threshold", 0.90))


def cmd_state_add(args, cfg: dict) -> int:
    """Save the current UI region as the reference for a named state."""
    region = parse_region(args.region) if args.region else states_region(cfg)
    if not region:
        raise SystemExit("set states.region in hunt.toml, or pass --region X,Y,W,H")
    directory = ROOT / cfg.get("states", {}).get("dir", "assets/states")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{args.name}.png"
    if path.exists() and not args.force:
        raise SystemExit(f"{path} exists; pass --force to replace it")
    capture(region, cfg["capture"]["output"]).save(path)
    print(f"saved {path}")
    known = build_states(cfg)
    worst = known.confusion()[:1]
    if worst and worst[0][2] > 0.9:
        a, b, score = worst[0]
        print(f"WARNING: '{a}' and '{b}' score {score:.3f} against each other -- "
              "too alike to tell apart. Pick a region where they differ.")
    return 0


def cmd_state_list(args, cfg: dict) -> int:
    known = build_states(cfg)
    print(f"{len(known)} states in {known.directory}:")
    for name in known.templates:
        print(f"  {name}")
    pairs = known.confusion()
    if pairs:
        print("\nmost confusable pairs (want these well below the threshold "
              f"of {known.threshold}):")
        for first, second, score in pairs[:6]:
            flag = "  <-- TOO ALIKE" if score > 0.9 else ""
            print(f"  {first:<18} vs {second:<18} {score:+.3f}{flag}")
    return 0


def cmd_state_watch(args, cfg: dict) -> int:
    known = build_states(cfg)
    if not len(known):
        raise SystemExit("no state templates yet; capture one with state-add")
    region = states_region(cfg)
    deadline = time.monotonic() + args.seconds
    try:
        while time.monotonic() < deadline:
            print(known.identify(capture(region, cfg["capture"]["output"])),
                  flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    return 0


# --------------------------------------------------------------------------
# the closed loop: watch the battle, act on what is actually on screen
# --------------------------------------------------------------------------
def fight(cfg: dict, pad: pad_driver.Controller, checks: CheckSet,
          clf: Classifier, log=print) -> tuple[str, AttemptResult | None]:
    """Drive the battle from what is on screen.

    Colosseum is a double battle, so each turn asks both of your Pokemon for an
    action; `[battle.turns]` maps whose-turn-it-is to the action list to run.

    Flaaffy still on the field -> act for whichever Pokemon is being asked.
                                  A ball that breaks out simply comes round
                                  again next turn, so no special case for it.
    Flaaffy gone               -> open the party. Listed means snagged, and the
                                  portrait answers the shiny question. Not
                                  listed means it fainted.

    Outcomes: shiny | uncertain | fainted | reset | timeout.
    """
    battle = cfg.get("battle", {})
    anchors = battle.get("anchor_any") or [battle.get("anchor", "command_menu")]
    turns = battle.get("turns", {})
    conf = cfg.get("states", {})
    poll = float(conf.get("poll_interval_s", 0.25))
    idle_limit = int(conf.get("unknown_tolerance", 12)) * 4
    deadline = time.monotonic() + float(conf.get("max_seconds", 300))

    idle = 0
    acted = 0
    max_actions = int(battle.get("max_actions", 40))
    while time.monotonic() < deadline:
        screen = capture(None)
        if not any(checks.present(a, screen) for a in anchors):
            # Animations, text and transitions look like nothing in particular;
            # only act from the stable per-turn anchor.
            idle += 1
            if idle > idle_limit:
                log("      never reached a command menu -> reset")
                return "reset", None
            time.sleep(poll)
            continue
        idle = 0

        if checks.present("flaaffy_on_field", screen):
            action = next((name for check, name in turns.items()
                           if checks.present(check, screen)), None)
            if action is None:
                time.sleep(poll)
                continue
            gate = battle.get("weaken_while")
            if action == "weaken" and gate and not checks.present(gate, screen):
                # Flaaffy is low enough; keep hitting it and the attempt dies
                # to a KO instead of a failed catch.
                action = battle.get("weaken_fallback", action)
                log("      Flaaffy is low -- holding off the attack")
            acted += 1
            if acted > max_actions:
                log(f"      {max_actions} actions without resolution -> reset")
                return "reset", None
            log(f"      {action}")
            run_steps(pad, battle.get(action, []), cfg, log=lambda m: None)
            pad.wait(float(battle.get("action_cooldown_frames", 120)))
            continue

        log("      Flaaffy is off the field -- checking the party")
        run_steps(pad, battle.get("open_party", []), cfg, log=lambda m: None)
        pad.wait(float(battle.get("party_cooldown_frames", 110)))
        screen = capture(None)
        if not checks.present("flaaffy_in_party", screen):
            log("      not in the party -> it fainted")
            return "fainted", None

        result = sample_window(cfg, clf, pad, log=log)
        decision = result.decide(cfg["decide"])
        if decision == "shiny":
            return "shiny", result
        if decision == "uncertain":
            return "uncertain", result
        log("      snagged, not shiny -> reset")
        return "reset", result
    return "timeout", None


def cmd_fight(args, cfg: dict) -> int:
    """Run just the battle loop against whatever is on screen right now."""
    known = build_checks(cfg)
    if not len(known):
        raise SystemExit("no [checks.*] configured in hunt.toml")
    clf = build_classifier(cfg)
    pipe, pad = open_pad(cfg, dry_run=args.dry_run)
    try:
        try:
            outcome, _ = fight(cfg, pad, known, clf)
        except RouteDesync as exc:
            outcome = f"reset ({exc})"
        print(f"outcome: {outcome}")
    finally:
        pad.neutral()
        pipe.close()
    return 0


def cmd_check_test(args, cfg: dict) -> int:
    """Score every check against the screen now, or against a saved shot."""
    checks = build_checks(cfg)
    if not len(checks):
        raise SystemExit("no [checks.*] configured in hunt.toml")
    screen = (Image.open(args.source).convert("RGB") if args.source
              else capture(None, cfg["capture"]["output"]))
    for name, score, present in checks.report(screen):
        print(f"  {name:<20} {score:+.3f}  {'PRESENT' if present else 'absent'}")
    return 0


def cmd_focus(args, cfg: dict) -> int:
    """Show the focused window and the Dolphin window's geometry."""
    print(f"focused app_id: {focused_app_id()!r}")
    proc = subprocess.run(["niri", "msg", "--json", "windows"], capture_output=True)
    if proc.returncode != 0:
        print("niri msg windows failed", file=sys.stderr)
        return 1
    wanted = cfg["dolphin"].get("app_id", "dolphin").lower()
    for window in json.loads(proc.stdout):
        if wanted in (window.get("app_id") or "").lower():
            size = (window.get("layout") or {}).get("window_size")
            print(f"  id={window['id']} title={window.get('title')!r} size={size}")
            print("  Dolphin renders the game inside this window; take a snap and "
                  "read the region off it:")
            print("    python scripts/hunt.py snap")
    return 0


def open_pad(cfg: dict, dry_run: bool = False) -> tuple[pad_driver.Pipe, pad_driver.Controller]:
    dolphin = cfg["dolphin"]
    pipe_path = pad_driver.user_dir() / "Pipes" / dolphin["pipe"]
    pipe = pad_driver.Pipe(pipe_path, dry_run=dry_run)
    pipe.open(timeout=None if dry_run else 120.0)
    pad = pad_driver.Controller(pipe, fps=dolphin["fps"], speed=dolphin["speed"])
    return pipe, pad


def cmd_route(args, cfg: dict) -> int:
    pipe, pad = open_pad(cfg, dry_run=args.dry_run)
    phases = ["reset", "boot"] if args.phase == "all" else [args.phase]
    try:
        for phase in phases:
            print(f"  [{phase}]")
            started = time.monotonic()
            run_steps(pad, cfg["route"][phase], cfg)
            print(f"  [{phase}] done in {time.monotonic() - started:.1f}s")
        if args.sample:
            print("  [sample]")
            clf = build_classifier(cfg)
            result = sample_window(cfg, clf, pad)
            print(f"  verdict: {result.decide(cfg['decide'])}")
    finally:
        pad.neutral()
        pipe.close()
    return 0


def cmd_loop(args, cfg: dict) -> int:
    """Replay the whole attempt on repeat, with no vision. Use this to confirm
    the route loops cleanly before adding shiny detection."""
    phases = route_phases(cfg)
    end_delay = float(cfg["route"].get("end_delay_frames", 180))
    print("attempt = reset ->", " -> ".join(name for name, _ in phases),
          f"-> {end_delay/60:.0f}s delay")

    pipe, pad = open_pad(cfg, dry_run=args.dry_run)
    iteration = 0
    started = time.monotonic()
    try:
        while not args.max or iteration < args.max:
            iteration += 1
            print(f"\n=== iteration {iteration}"
                  f"  ({(time.monotonic() - started)/60:.1f} min elapsed)")
            mark = time.monotonic()
            run_steps(pad, cfg["route"]["reset"], cfg, log=lambda m: None)
            print(f"    {'reset':<16} {time.monotonic() - mark:6.1f}s")
            for name, steps in phases:
                mark = time.monotonic()
                run_steps(pad, steps, cfg, log=lambda m: None)
                print(f"    {name:<16} {time.monotonic() - mark:6.1f}s"
                      f"  ({len(steps)} steps)")
            mark = time.monotonic()
            pad.wait(end_delay)
            print(f"    {'end delay':<16} {time.monotonic() - mark:6.1f}s")
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        pad.neutral()
        pipe.close()
    print(f"\n{iteration} iterations in {(time.monotonic() - started)/60:.1f} min")
    return 0


def dolphin_alive(name: str) -> bool:
    return subprocess.run(["pgrep", "-x", name], capture_output=True).returncode == 0


def cmd_run(args, cfg: dict) -> int:
    decide = cfg["decide"]
    clf = build_classifier(cfg)
    if not config_region(cfg):
        raise SystemExit(
            "capture.region is empty in hunt.toml -- calibrate it first:\n"
            "  python scripts/hunt.py snap\n"
            "  python scripts/hunt.py crop --region X,Y,W,H --from snap.png\n"
            "  python scripts/hunt.py watch")

    low, high = cfg["route"].get("jitter", [0, 0])
    if high <= low:
        raise SystemExit(
            "route.jitter is disabled in hunt.toml. Without it Dolphin replays\n"
            "the same frames every attempt and re-rolls the identical Flaaffy,\n"
            "so the hunt can never succeed. Set it to e.g. [0, 150] first.")
    phases = route_phases(cfg)
    known = build_checks(cfg)
    if not len(known):
        raise SystemExit("no [checks.*] configured in hunt.toml -- see the\n"
                         "commented block there, then verify with `check-test`.")

    run_dir = ROOT / decide["run_dir"] / datetime.now().strftime("%Y%m%d-%H%M%S")
    frames_dir = run_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "log.csv"
    log_file = log_path.open("w", newline="")
    log = csv.writer(log_file)
    log.writerow(["attempt", "time", "decision", "best_label", "best_margin",
                  "subject_px", "shiny_votes", "normal_votes", "frames"])
    log_path_txt = run_dir / "hunt.log"
    tee = Tee(log_path_txt)
    sys.stdout = tee
    print(f"run directory: {run_dir}")
    print(f"log: {log_path_txt}   (tail -f it)")
    print(f"started {datetime.now().isoformat(timespec='seconds')}")

    max_attempts = int(decide["max_attempts"]) or (args.max or 0)
    if args.max:
        max_attempts = args.max
    process = cfg["dolphin"]["process"]

    pipe, pad = open_pad(cfg, dry_run=args.dry_run)
    attempt = 0
    consecutive_uncertain = 0
    consecutive_errors = 0
    tally: dict[str, int] = {}
    started = time.monotonic()
    outcome = "stopped"
    try:
        while True:
            if max_attempts and attempt >= max_attempts:
                outcome = "attempt limit reached"
                break
            if not args.dry_run and not dolphin_alive(process):
                outcome = f"{process} is no longer running"
                break

            attempt += 1
            elapsed = time.monotonic() - started
            rate = attempt / (elapsed / 3600) if elapsed > 60 else 0.0
            summary = " ".join(f"{k}={v}" for k, v in sorted(tally.items()))
            print(f"\n--- attempt {attempt}  [{stamp()}]  {elapsed/60:.1f} min"
                  + (f"  {rate:.0f}/hr" if rate else "")
                  + (f"  {summary}" if summary else ""))
            run_steps(pad, cfg["route"]["reset"], cfg, log=lambda m: None)
            desynced = False
            for name, steps in phases:
                try:
                    run_steps(pad, steps, cfg, log=lambda m: None)
                except RouteDesync as exc:
                    print(f"    {name}: {exc} -> reset")
                    desynced = True
                    break
            try:
                if desynced:
                    raise RouteDesync("replay drifted")
                outcome_name, result = fight(cfg, pad, known, clf,
                                             log=lambda m: print(m))
            except RouteDesync as exc:
                print(f"      desync: {exc} -> reset")
                outcome_name, result = "reset", None
            except SystemExit:
                raise
            except Exception as exc:
                # One bad attempt must not end an overnight run. Screenshots
                # fail, windows lose focus, the clipboard hiccups -- reset and
                # carry on, but stop if it keeps happening, because something
                # is actually broken rather than flaky.
                consecutive_errors += 1
                print(f"      ERROR {type(exc).__name__}: {exc} "
                      f"(consecutive {consecutive_errors})")
                if consecutive_errors >= int(decide.get("error_tolerance", 5)):
                    outcome = f"stopped: {consecutive_errors} errors in a row"
                    break
                outcome_name, result = "reset", None
            decision = {"shiny": "shiny", "uncertain": "uncertain"}.get(
                outcome_name, "normal")
            best = result.best if result else None
            result = result or AttemptResult([], [])

            stem = f"{attempt:05d}-{decision}"
            saved = []
            if decision == "normal":
                if result.frames:
                    path = frames_dir / f"{stem}.png"
                    result.frames[len(result.frames) // 2].save(path)
                    saved.append(path.name)
            else:
                for i, frame in enumerate(result.frames):
                    path = frames_dir / f"{stem}-{i:02d}.png"
                    frame.save(path)
                    saved.append(path.name)
                full = frames_dir / f"{stem}-fullscreen.png"
                capture(None, cfg["capture"]["output"]).save(full)
                saved.append(full.name)

            tally[outcome_name] = tally.get(outcome_name, 0) + 1
            consecutive_errors = 0
            log.writerow([attempt, datetime.now().isoformat(timespec="seconds"),
                          f"{decision}/{outcome_name}", best.label if best else "",
                          f"{best.margin:+.3f}" if best else "",
                          best.subject_px if best else "",
                          len(result.shiny), len(result.normal), " ".join(saved)])
            log_file.flush()
            print(f"    => {outcome_name}  (shiny={len(result.shiny)} "
                  f"normal={len(result.normal)} of {len(result.verdicts)} frames)")
            if outcome_name == "caught":
                outcome = "CAUGHT"
                break

            if decision == "shiny":
                outcome = "SHINY FOUND"
                break
            if decision == "uncertain":
                consecutive_uncertain += 1
                if consecutive_uncertain > int(decide["uncertain_tolerance"]):
                    outcome = "stopped: inconclusive frames"
                    break
            else:
                consecutive_uncertain = 0
                prune_frames(frames_dir, int(decide["keep_normal_frames"]))
    except KeyboardInterrupt:
        outcome = "interrupted"
    finally:
        pad.neutral()
        pipe.close()
        log_file.close()

    minutes = (time.monotonic() - started) / 60
    print(f"\nfinished {datetime.now().isoformat(timespec='seconds')}: {outcome}")
    print("tally: " + (" ".join(f"{k}={v}" for k, v in sorted(tally.items())) or "none"))
    summary = {"outcome": outcome, "attempts": attempt, "minutes": round(minutes, 1),
               "tally": tally, "run_dir": str(run_dir)}
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))

    if outcome == "CAUGHT":
        alert("FLAAFFY CAUGHT", f"attempt {attempt} after {minutes:.0f} min\n{run_dir}",
              cfg)
    elif outcome == "SHINY FOUND":
        alert("SHINY FLAAFFY", 
              f"attempt {attempt} after {minutes:.0f} min -- the battle is waiting "
              f"for you, inputs are released.\n{run_dir}", cfg)
    elif outcome.startswith("stopped: inconclusive"):
        alert("Hunt stopped: could not tell",
              f"attempt {attempt}. Check the saved frames in {frames_dir}", cfg)
    else:
        alert("Hunt finished", f"{outcome} after {attempt} attempts "
                               f"({minutes:.0f} min)\n{run_dir}", cfg, urgent=False)
    print(json.dumps(summary, indent=2))
    sys.stdout = sys.__stdout__
    tee.close()
    return 0 if outcome == "SHINY FOUND" else 2


def prune_frames(frames_dir: Path, keep: int) -> None:
    if keep <= 0:
        return
    duds = sorted(frames_dir.glob("*-normal.png"))
    for path in duds[:-keep]:
        path.unlink(missing_ok=True)


# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("selftest", help="classify the references and degraded copies")
    sub.add_parser("focus", help="show the focused window and Dolphin's geometry")

    p = sub.add_parser("snap", help="full screenshot for calibration")
    p.add_argument("-o", "--out")

    p = sub.add_parser("crop", help="crop and classify a region")
    p.add_argument("--region", help="X,Y,W,H (default: capture.region)")
    p.add_argument("--from", dest="source", help="crop this file instead of the screen")
    p.add_argument("-o", "--out")

    p = sub.add_parser("classify", help="classify image files")
    p.add_argument("paths", nargs="+")

    p = sub.add_parser("watch", help="live verdicts on the capture region")
    p.add_argument("--seconds", type=float, default=60.0)
    p.add_argument("--interval", type=float, default=0.5)

    p = sub.add_parser("route", help="replay the route without hunting")
    p.add_argument("--phase", choices=["reset", "boot", "all"], default="all")
    p.add_argument("--sample", action="store_true", help="classify afterwards")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("state-add", help="save the UI region as a named state")
    p.add_argument("name")
    p.add_argument("--region", help="X,Y,W,H (default: states.region)")
    p.add_argument("--force", action="store_true")

    sub.add_parser("state-list", help="list states and how confusable they are")

    p = sub.add_parser("state-watch", help="live state identification")
    p.add_argument("--seconds", type=float, default=60.0)
    p.add_argument("--interval", type=float, default=0.4)

    p = sub.add_parser("check-test", help="score every check against the screen")
    p.add_argument("--from", dest="source", help="a saved screenshot instead")

    p = sub.add_parser("fight", help="run the battle loop only")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("loop", help="replay the route on repeat, no vision")
    p.add_argument("--max", type=int, default=0, help="stop after N iterations")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("run", help="run the hunt")
    p.add_argument("--max", type=int, default=0, help="stop after N attempts")
    p.add_argument("--dry-run", action="store_true", help="print inputs, still captures")

    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    handlers = {"selftest": cmd_selftest, "snap": cmd_snap, "crop": cmd_crop,
                "focus": cmd_focus, "loop": cmd_loop,
                "state-add": cmd_state_add, "state-list": cmd_state_list,
                "state-watch": cmd_state_watch, "fight": cmd_fight,
                "check-test": cmd_check_test,
                "classify": cmd_classify, "watch": cmd_watch, "route": cmd_route,
                "run": cmd_run}
    return handlers[args.command](args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
