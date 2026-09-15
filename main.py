#!/usr/bin/env python3
"""
Script GameCube controller inputs and feed them to Dolphin.

Dolphin can take controller input from a named pipe (FIFO).  This program
creates that pipe, speaks Dolphin's pipe protocol, and exposes a small Python
API so input sequences are written as ordinary Python scripts.

    python main.py --setup                 # create the pipe + a Dolphin profile
    python main.py scripts/example.py      # run a script (waits for Dolphin)
    python main.py --dry-run scripts/example.py
    python main.py --eval "tap('A'); wait(60); tap('START')"

Inside Dolphin: Controllers -> Port N -> Standard Controller -> Configure,
then load the generated profile (or set Device to Pipe/0/<pipe name> by hand).
"""

from __future__ import annotations

import argparse
import errno
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

# Button names understood by Dolphin's pipe input.
BUTTONS = (
    "A", "B", "X", "Y", "Z", "START", "L", "R",
    "D_UP", "D_DOWN", "D_LEFT", "D_RIGHT",
)

# Convenience aliases so scripts can be written the way people talk.
ALIASES = {
    "UP": "D_UP", "DOWN": "D_DOWN", "LEFT": "D_LEFT", "RIGHT": "D_RIGHT",
    "DPAD_UP": "D_UP", "DPAD_DOWN": "D_DOWN",
    "DPAD_LEFT": "D_LEFT", "DPAD_RIGHT": "D_RIGHT",
    "PLUS": "START", "MINUS": "START",
}

DEFAULT_PIPE_NAME = "pipe1"
NTSC_FPS = 60.0


def user_dir() -> Path:
    """Dolphin's user directory (where Pipes/ lives)."""
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "dolphin-emu"


def config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "dolphin-emu"


def normalise(button: str) -> str:
    name = button.strip().upper()
    name = ALIASES.get(name, name)
    if name not in BUTTONS:
        raise ValueError(f"unknown button {button!r}; valid: {', '.join(BUTTONS)}")
    return name


def clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


class Pipe:
    """The transport: a Dolphin input FIFO, or stdout when dry running."""

    def __init__(self, path: Path, dry_run: bool = False, echo: bool = False):
        self.path = path
        self.dry_run = dry_run
        self.echo = echo
        self._fh = None

    def create(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            if not self.path.is_fifo():
                raise SystemExit(f"{self.path} exists and is not a FIFO; remove it first")
            return
        os.mkfifo(self.path, 0o666)

    def open(self, timeout: float | None = 60.0) -> None:
        if self.dry_run:
            return
        self.create()
        # A FIFO cannot be opened for writing until a reader exists, i.e. until
        # Dolphin has started emulation with this pipe as its input device.
        deadline = None if timeout is None else time.monotonic() + timeout
        waited = False
        while True:
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)
                break
            except OSError as exc:
                if exc.errno != errno.ENXIO:
                    raise
                if deadline is not None and time.monotonic() > deadline:
                    raise SystemExit(
                        f"timed out waiting for Dolphin to open {self.path}.\n"
                        "Start a game with a controller port set to "
                        f"Pipe/0/{self.path.name}."
                    )
                if not waited:
                    print(f"waiting for Dolphin to open {self.path} ...", file=sys.stderr)
                    waited = True
                time.sleep(0.25)
        os.set_blocking(fd, True)
        self._fh = os.fdopen(fd, "w", buffering=1)

    def send(self, command: str) -> None:
        if self.echo or self.dry_run:
            print(command)
        if self.dry_run:
            return
        if self._fh is None:
            raise RuntimeError("pipe is not open")
        try:
            self._fh.write(command + "\n")
        except BrokenPipeError:
            raise SystemExit("Dolphin closed the pipe (emulation stopped?)")

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None


class Controller:
    """The scripting API.  All waits are measured in emulator frames."""

    RESYNC_AFTER = 0.25   # seconds of external stall before the clock resets

    def __init__(self, pipe: Pipe, fps: float = NTSC_FPS, speed: float = 1.0):
        self.pipe = pipe
        self.fps = fps
        self.speed = speed          # >1 runs a script faster than real time
        self.frame = 0              # frames of script time elapsed
        self._clock = time.perf_counter()
        self._held: set[str] = set()

    # -- timing ----------------------------------------------------------
    def wait(self, frames: float = 1) -> None:
        """Sleep for `frames` frames of script time (drift-free)."""
        now = time.perf_counter()
        if self._clock < now - self.RESYNC_AFTER:
            # Something outside the script blocked for a long time -- taking a
            # screenshot, say. Without resyncing, the virtual clock stays in
            # the past and every subsequent wait sleeps zero, which silently
            # turns tap() into a press and release with no gap: the emulator
            # never sees the button held at all.
            self._clock = now
        self.frame += frames
        self._clock += frames / (self.fps * self.speed)
        remaining = self._clock - time.perf_counter()
        if remaining > 0:
            time.sleep(remaining)

    def wait_s(self, seconds: float) -> None:
        self.wait(seconds * self.fps)

    # -- buttons ---------------------------------------------------------
    def press(self, *buttons: str) -> None:
        for button in buttons:
            name = normalise(button)
            self.pipe.send(f"PRESS {name}")
            self._held.add(name)

    def release(self, *buttons: str) -> None:
        for button in buttons:
            name = normalise(button)
            self.pipe.send(f"RELEASE {name}")
            self._held.discard(name)

    def tap(self, *buttons: str, frames: int = 3, gap: int = 3) -> None:
        """Press, hold for `frames`, release, then idle for `gap` frames."""
        self.press(*buttons)
        self.wait(frames)
        self.release(*buttons)
        self.wait(gap)

    @contextmanager
    def hold(self, *buttons: str):
        """with pad.hold('B'): pad.tap('A')"""
        self.press(*buttons)
        try:
            yield self
        finally:
            self.release(*buttons)

    # -- analog ----------------------------------------------------------
    def stick(self, x: float = 0.0, y: float = 0.0) -> None:
        """Main stick, -1..1 per axis (+x right, +y up).  (0,0) = centre."""
        self._axis("MAIN", x, y)

    def c_stick(self, x: float = 0.0, y: float = 0.0) -> None:
        self._axis("C", x, y)

    def _axis(self, name: str, x: float, y: float) -> None:
        # Dolphin wants both axes in ONE command: "SET MAIN <x> <y>".  Sending
        # "SET MAIN X <v>" is parsed as the x/y form with atof("X") == 0.0,
        # which silently jams the stick hard left.
        self.pipe.send(f"SET {name} {(clamp(x, -1, 1) + 1) / 2:.4f} "
                       f"{(clamp(y, -1, 1) + 1) / 2:.4f}")

    def stick_raw(self, x: float = 0.5, y: float = 0.5) -> None:
        """Main stick in Dolphin's own 0..1 units (0.5 = centre)."""
        self.pipe.send(f"SET MAIN {clamp(x, 0, 1):.4f} {clamp(y, 0, 1):.4f}")

    def l_trigger(self, value: float = 1.0) -> None:
        """Analog L, 0..1.  Full press also needs the digital click: press('L')."""
        self.pipe.send(f"SET L {clamp(value, 0, 1):.4f}")

    def r_trigger(self, value: float = 1.0) -> None:
        self.pipe.send(f"SET R {clamp(value, 0, 1):.4f}")

    # -- housekeeping ----------------------------------------------------
    def neutral(self) -> None:
        """Release everything and centre both sticks."""
        for name in list(self._held):
            self.release(name)
        self.stick(0, 0)
        self.c_stick(0, 0)
        self.l_trigger(0)
        self.r_trigger(0)


PROFILE = """\
Device = Pipe/0/{name}
Buttons/A = `Button A`
Buttons/B = `Button B`
Buttons/X = `Button X`
Buttons/Y = `Button Y`
Buttons/Z = `Button Z`
Buttons/Start = `Button START`
D-Pad/Up = `Button D_UP`
D-Pad/Down = `Button D_DOWN`
D-Pad/Left = `Button D_LEFT`
D-Pad/Right = `Button D_RIGHT`
Triggers/L = `Button L`
Triggers/R = `Button R`
Triggers/L-Analog = `Axis L -`
Triggers/R-Analog = `Axis R -`
Main Stick/Up = `Axis MAIN Y +`
Main Stick/Down = `Axis MAIN Y -`
Main Stick/Left = `Axis MAIN X -`
Main Stick/Right = `Axis MAIN X +`
C-Stick/Up = `Axis C Y +`
C-Stick/Down = `Axis C Y -`
C-Stick/Left = `Axis C X -`
C-Stick/Right = `Axis C X +`
"""


def do_setup(pipe_path: Path, profile_name: str) -> None:
    pipe = Pipe(pipe_path)
    pipe.create()
    print(f"pipe: {pipe_path}")

    profile_dir = config_dir() / "Profiles" / "GCPad"
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_path = profile_dir / f"{profile_name}.ini"
    profile_path.write_text(PROFILE.format(name=pipe_path.name))
    print(f"profile: {profile_path}")
    print(
        "\nIn Dolphin:\n"
        "  Controllers -> Port N -> Standard Controller -> Configure\n"
        f"  Profile: load '{profile_name}' -> the Device field should read "
        f"Pipe/0/{pipe_path.name}\n\n"
        "Most games (Pokemon Colosseum included) only read Port 1, so that is\n"
        "usually the port you want -- which displaces the real pad configured\n"
        "there. scripts/pad_config.py does that swap with a backup, and puts it\n"
        "back afterwards:\n"
        "  python scripts/pad_config.py pipe --port 1\n"
        "  python scripts/pad_config.py restore\n\n"
        "Then start the game and run:  python main.py scripts/example.py"
    )


SCRIPT_HELP = """\
Scripts are plain Python.  Available names:
  pad                     the Controller object
  press / release / tap   pad.press('A'), pad.tap('A', frames=3)
  hold                    with hold('B'): tap('A')
  stick / c_stick         stick(1, 0) = full right, stick(0, 0) = centre
  l_trigger / r_trigger   0..1 analog
  wait / wait_s           wait(60) = one second at 60fps
  neutral                 release everything
  BUTTONS                 tuple of valid button names
"""


def build_namespace(pad: Controller) -> dict:
    return {
        "pad": pad,
        "press": pad.press,
        "release": pad.release,
        "tap": pad.tap,
        "hold": pad.hold,
        "stick": pad.stick,
        "c_stick": pad.c_stick,
        "stick_raw": pad.stick_raw,
        "l_trigger": pad.l_trigger,
        "r_trigger": pad.r_trigger,
        "wait": pad.wait,
        "wait_s": pad.wait_s,
        "neutral": pad.neutral,
        "BUTTONS": BUTTONS,
        "__name__": "__input_script__",
    }


def run_source(source: str, filename: str, pad: Controller) -> None:
    namespace = build_namespace(pad)
    code = compile(source, filename, "exec")
    try:
        exec(code, namespace)
        # Support both styles: top-level statements, or a `script(pad)` function.
        entry = namespace.get("script")
        if callable(entry):
            entry(pad)
    finally:
        pad.neutral()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Script GameCube controller input into Dolphin.",
        epilog=SCRIPT_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("script", nargs="?", help="Python input script to run")
    parser.add_argument("--eval", "-e", metavar="CODE", help="run inline code instead of a file")
    parser.add_argument("--setup", action="store_true",
                        help="create the pipe and a Dolphin controller profile, then exit")
    parser.add_argument("--pipe", default=DEFAULT_PIPE_NAME, help="pipe name (default: %(default)s)")
    parser.add_argument("--pipe-dir", type=Path, default=None,
                        help=f"default: {user_dir() / 'Pipes'}")
    parser.add_argument("--profile-name", default="PipeScript", help="name of the generated profile")
    parser.add_argument("--fps", type=float, default=NTSC_FPS, help="frames per second (default: 60)")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="script speed multiplier, match Dolphin's emulation speed")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the pipe commands instead of sending them")
    parser.add_argument("--echo", action="store_true", help="print commands while sending them")
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="seconds to wait for Dolphin to open the pipe (0 = forever)")
    args = parser.parse_args(argv)

    pipe_dir = args.pipe_dir or (user_dir() / "Pipes")
    pipe_path = pipe_dir / args.pipe

    if args.setup:
        do_setup(pipe_path, args.profile_name)
        return 0

    if args.eval:
        source, filename = args.eval, "<eval>"
    elif args.script:
        path = Path(args.script)
        if not path.is_file():
            parser.error(f"no such script: {path}")
        source, filename = path.read_text(), str(path)
    else:
        parser.print_help()
        return 1

    pipe = Pipe(pipe_path, dry_run=args.dry_run, echo=args.echo)
    pipe.open(timeout=None if args.timeout == 0 else args.timeout)
    pad = Controller(pipe, fps=args.fps, speed=args.speed)
    try:
        run_source(source, filename, pad)
    except KeyboardInterrupt:
        print("\ninterrupted - releasing inputs", file=sys.stderr)
        pad.neutral()
        return 130
    finally:
        pipe.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
