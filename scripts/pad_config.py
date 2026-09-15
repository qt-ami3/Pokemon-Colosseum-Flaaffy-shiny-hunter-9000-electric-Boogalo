#!/usr/bin/env python3
"""Point a Dolphin controller port at the input pipe, and put it back again.

Pokemon Colosseum reads Port 1, so the hunt needs Port 1 bound to the pipe --
which means displacing whatever real pad is configured there.  This rewrites
~/.config/dolphin-emu/GCPadNew.ini, so it takes a backup first and refuses to
run while Dolphin is open (Dolphin rewrites its config on exit and would
clobber the change).

    python scripts/pad_config.py show
    python scripts/pad_config.py pipe [--port 1] [--pipe pipe1]
    python scripts/pad_config.py restore
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from main import PROFILE, config_dir  # noqa: E402

INI = config_dir() / "GCPadNew.ini"


def dolphin_running() -> bool:
    return subprocess.run(["pgrep", "-x", "dolphin-emu"],
                          capture_output=True).returncode == 0


def sections(text: str) -> list[tuple[str, str]]:
    """Split an ini into (header, body) pairs, preserving everything."""
    parts = re.split(r"(?m)^(\[[^\]]+\])\s*$", text)
    out = []
    if parts[0].strip():
        out.append(("", parts[0]))
    for i in range(1, len(parts), 2):
        out.append((parts[i], parts[i + 1]))
    return out


def confirm(summary: list[str], yes: bool) -> None:
    print("\n".join(summary))
    if yes:
        print("\n--yes given, proceeding.")
        return
    if not sys.stdin.isatty():
        raise SystemExit("refusing to rewrite the config non-interactively; pass --yes")
    print("\nThis overwrites your Dolphin controller config (a backup is taken).")
    time.sleep(3)
    if input("Type 'yes' to continue: ").strip().lower() != "yes":
        raise SystemExit("aborted, nothing changed")


def backup() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = INI.with_suffix(f".ini.bak-{stamp}")
    shutil.copy2(INI, target)
    print(f"backup: {target}")
    return target


def cmd_show(args) -> int:
    for header, body in sections(INI.read_text()):
        if header.startswith("[GCPad"):
            device = re.search(r"(?m)^Device\s*=\s*(.+)$", body)
            print(f"{header:<10} {device.group(1).strip() if device else '(no Device)'}")
    return 0


def cmd_pipe(args) -> int:
    if dolphin_running():
        raise SystemExit("close Dolphin first -- it rewrites this file when it exits")
    header = f"[GCPad{args.port}]"
    text = INI.read_text()
    parts = sections(text)
    if not any(h == header for h, _ in parts):
        raise SystemExit(f"{header} not found in {INI}")
    current = next(b for h, b in parts if h == header)
    device = re.search(r"(?m)^Device\s*=\s*(.+)$", current)
    confirm([
        f"file:    {INI}",
        f"section: {header}",
        f"from:    {device.group(1).strip() if device else '(none)'}",
        f"to:      Pipe/0/{args.pipe}",
    ], args.yes)
    backup()
    body = "\n" + PROFILE.format(name=args.pipe)
    rebuilt = "".join(h + (body if h == header else b) for h, b in parts)
    INI.write_text(rebuilt)
    print(f"{header} now reads from Pipe/0/{args.pipe}")
    print("Undo with: python scripts/pad_config.py restore")
    return 0


def cmd_restore(args) -> int:
    if dolphin_running():
        raise SystemExit("close Dolphin first -- it rewrites this file when it exits")
    backups = sorted(INI.parent.glob("GCPadNew.ini.bak-*"))
    if not backups:
        raise SystemExit("no backups found")
    newest = backups[-1]
    confirm([f"restore: {newest}", f"over:    {INI}"], args.yes)
    shutil.copy2(newest, INI)
    print(f"restored {newest.name}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show", help="print the device bound to each port")
    p = sub.add_parser("pipe", help="bind a port to the input pipe")
    p.add_argument("--port", type=int, default=1)
    p.add_argument("--pipe", default="pipe1")
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("restore", help="restore the most recent backup")
    p.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    return {"show": cmd_show, "pipe": cmd_pipe, "restore": cmd_restore}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
