# Shiny hunt — St. Performer Diogo's Shadow Flaaffy (Pokémon Colosseum)

Scripted controller input into Dolphin, a hue-based shiny detector, and a
closed loop that reads the screen before every action. It resets, replays to
the battle, fights and snags the Flaaffy, reads the party portrait, and stops
the moment it sees a shiny — or the moment it stops being sure what it is
looking at.

## Quick start

Dolphin must be **focused** — input is dropped otherwise — and the game
running. Start a command, then click into Dolphin.

```sh
QT_QPA_PLATFORM=xcb dolphin-emu --exec="emulation/wiigamecube/roms/Pokemon Colosseum (Europe) (En,Fr,De,Es,It).ciso"

python scripts/hunt.py check-test    # are all 19 checks reading correctly?
python scripts/hunt.py fight         # battle loop only, from a battle in progress
python scripts/hunt.py loop --max 1  # reset -> start -> combat_start, no vision
python scripts/hunt.py run           # the hunt (needs jitter enabled, below)
```

`run` logs to the terminal and to `runs/<timestamp>/hunt.log` — `tail -f` it
from another terminal.

## How one attempt works

```
reset (Z, bound to Dolphin's Reset hotkey)
  -> start.toml          title screen -> save loaded -> back to Diogo
  -> combat_start.toml   talk to Diogo, battle begins
  -> closed loop, per turn at the command menu:
       UMBREON  -> BITE at Flaaffy, unless its HP is below the gate
       ESPEON   -> throw a Poké Ball
       broke out?     the next turn just comes round again
       Flaaffy gone?  open the party
                        listed     -> read the portrait -> shiny? STOP : reset
                        not listed -> it fainted -> reset
```

### Why the shiny check happens *after* the catch

A Shadow Pokémon's PID is assigned at first encounter and is guaranteed *not*
shiny against the NPC trainer's IDs. Snagging does not change the PID — it
changes the Original Trainer to you, and shininess is that PID against **your**
TID/SID ([Bulbapedia](https://bulbapedia.bulbagarden.net/wiki/Shadow_Pok%C3%A9mon)).
So it can never render shiny in battle; it must be caught and read off the
party portrait. XD shiny-locks this outright; Colosseum does not, which is the
only reason this hunt is possible.

### Why it reboots instead of loading a savestate

Colosseum seeds its RNG from the console clock at boot
([pokemonrng.com](https://www.pokemonrng.com/emulator-colosseum-general/)). A
savestate restores that RNG state verbatim, so the outcome depends only on how
many frames pass before the battle — with J frames of jitter you can reach
about J distinct Flaaffy, and the odds a shiny is among them are
`1-(1-1/8192)^J`: about 2% at J=150. The other 98% of the time the hunt cannot
succeed at all. Rebooting reseeds from the clock.

**Keep Custom RTC off** (Config → Advanced) or the boot seed is pinned and you
are back in that dead end.

## Files

```
main.py                 controller scripting: Dolphin pipe input + a Controller API
hunt.toml               everything tunable, commented
start.toml              recorded: title screen -> standing at Diogo
combat_start.toml       recorded: talk to Diogo, start the battle
controller_map.json     your pad's button/axis indices (from record.py calibrate)
assets/normal.png       reference party portraits for the shiny classifier
assets/shiny.png
assets/states/*.png     whole-window grabs the [checks] crop their regions from
scripts/hunt.py         the hunt, the battle loop, and all calibration commands
scripts/vision.py       shiny vs normal classifier (hue histograms)
scripts/states.py       screen checks: zncc / red / hue / hpbar modes
scripts/wincap.py       window capture + game viewport detection
scripts/record.py       record your real pad into a replayable route
scripts/probe.py        send inputs, capture, crop regions — for mapping new UI
scripts/pad_config.py   bind a Dolphin port to the pipe, and put it back
scripts/verify_inputs.py  exercise every input so you can watch it register
scripts/example.py      example input script for main.py
runs/                   per-run logs, CSV and frames
archive/                superseded files; delete whenever
```

## Setup from scratch

```sh
python main.py --setup                      # create the FIFO + a Dolphin profile
python scripts/pad_config.py pipe --port 1  # bind Port 1 (backs up, needs Dolphin closed)
python scripts/pad_config.py restore        # undo
```

Colosseum only reads **Port 1**, so this displaces whatever real pad is there.

Then in Dolphin: **Options → Hotkey Settings → Device `Pipe/0/pipe1`**, bind
`General/Reset` to the expression `Button Z`. Keyboard hotkeys do **not** work
here — Dolphin polls the keyboard through XInput2/XWayland, and synthetic keys
never arrive. Routing the reset down the pipe sidesteps that entirely, which is
why nothing in the route presses Z for any other purpose.

## Before a real hunt

`hunt.py run` refuses to start until you set this in `hunt.toml`:

```toml
jitter = [0, 150]        # ships as [0, 0], which makes a hunt impossible
```

## Tuning

| knob | what it does |
|---|---|
| `route.jitter` | RNG divergence before the battle. Must be non-zero. |
| `detect.margin`, `detect.min_score` | how sure the classifier must be; `selftest` prints real scores |
| `decide.uncertain_tolerance` | consecutive inconclusive *shiny checks* before stopping (default 2) |
| `decide.error_tolerance` | consecutive crashes before giving up (default 5) |
| `battle.weaken_while` | HP gate; below it, stop attacking so BITE cannot KO the Flaaffy |
| `dolphin.speed` | converts route frames to real time; route timings were tuned at `EmulationSpeed = 2.0` |

## Adding or fixing a check

Regions are **fractions of the game viewport**, not screen pixels, so they
survive the window moving, resizing or going fullscreen.

```sh
python scripts/probe.py --press A --wait 2 --out shot.png        # navigate + capture
python scripts/probe.py --out crop.png --crop x=0.5,0.1,0.1,0.05 # preview a region
python scripts/hunt.py check-test                                 # score every check now
python scripts/hunt.py check-test --from assets/states/party_open.png
```

Check modes:

- `zncc` — structural match against a template. Templates may be whole-window
  grabs; they are cropped to the region automatically.
- `red` — count the menu cursor's saturated red. Use this over any region on
  the battle menu.
- `hue` — same idea for any colour (`hue_range`), e.g. the cyan target arrow.
- `hpbar` — fraction of an HP bar that is filled.

**Keep regions tight.** The bag's whole title bar scores 0.925 between
different pockets because it is mostly identical chrome; the pocket title alone
scores 0.145. A wide region containing mostly the same pixels cannot
discriminate.

## Gotchas worth knowing

**Focus.** Dolphin drops all input when its window is not focused, and `wtype`
keystrokes never reach it at all. Typing in the terminal steals focus, so you
cannot drive the game and talk to a script at the same time.

**The battle menu is semi-transparent.** The 3D scene shows through it, so
structural matching over the menu box swings with whatever animates behind.
That is what the `red` and `hue` modes are for.

**Menus remember where the cursor was left.** Nothing counts presses; every
`until` step presses until the screen says it has arrived, and zero times when
it is already there.

**Menus animate in.** Checking instantly after opening one reads the old
screen. `first_settle_frames` on an `until` step waits before the first look —
without it, a check on a menu you are already in fails and the loop presses its
way all the way around.

**Timing.** Route waits are real-time frames (60 = 1 real second), pinned to
Dolphin's current `EmulationSpeed`. Changing it invalidates the recordings.

See `FINDINGS.md` for the full mapping session: every measured region, the
bugs that live testing surfaced, and what is still unproven.
