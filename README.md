# Shiny hunt: St. Performer Diogo's Shadow Flaaffy (Pokémon Colosseum)

Scripted controller input into Dolphin, plus a hue-based shiny detector, driving
a soft-reset loop that stops the moment it sees a shiny — or the moment it stops
being sure of what it is looking at.

```
main.py                 controller scripting: Dolphin pipe input + a Controller API
scripts/vision.py       shiny vs normal classifier (hue histograms)
scripts/hunt.py         the hunt loop and all the calibration tools
scripts/pad_config.py   bind a Dolphin port to the pipe, and put it back
scripts/example.py      example input script for main.py
hunt.toml               everything tunable, commented
assets/                 your normal.png / shiny.png reference crops
runs/                   per-run logs and frames (created on first run)
```

## How it works

One attempt is: **reboot → replay to Diogo → battle → snag the Flaaffy → read
the party sprite → classify.** Dud, reset, repeat. Each replayed file is
verified against a screen state before the next one runs, so drift is caught
rather than compounded.

Three design points worth knowing, because they are the ones that can bite:

**The shiny check has to happen after the capture.** A Shadow Pokémon's PID is
assigned at first encounter and is guaranteed *not* shiny against the NPC
trainer's IDs. Snagging does not change the PID — it changes the Original
Trainer to you, and shininess is that PID against *your* TID/SID
([Bulbapedia](https://bulbapedia.bulbagarden.net/wiki/Shadow_Pok%C3%A9mon)). So
it can never render shiny in battle; you must catch it and read the party
sprite. XD shiny-locks this outright; Colosseum does not, which is the only
reason the hunt is possible.

**Colosseum has no controller soft reset.** B+X+Start is Pokémon XD only
([Bulbapedia](https://bulbapedia.bulbagarden.net/wiki/Soft_reset),
[PokéBase](https://pokemondb.net/pokebase/152716/how-to-softreset-in-every-pokemon-game-its-in)).
So the reset is Dolphin's Reset hotkey sent with `wtype`, not a pad input. Bind
it under Options → Hotkey Settings → General → Reset; it has no default.

**It must be a reboot, not a savestate.** Colosseum seeds its RNG from the
console clock at boot and advances it deterministically
([pokemonrng.com](https://www.pokemonrng.com/emulator-colosseum-general/),
[aldelaro5](https://aldelaro5.wordpress.com/2018/09/09/controlling-luck-in-video-games-an-explanation-of-the-rng-manipulation-on-pokemon-colosseum-and-xd/)).
A savestate restores that RNG state verbatim, so the Pokémon you get depends
only on how many frames pass before the battle starts — with J frames of jitter
you can reach about J distinct Flaaffy, and the odds a shiny is among them are
`1-(1-1/8192)^J`, about 2% at J=150. The other 98% of the time the hunt cannot
succeed at all. Rebooting reseeds from the clock. It costs ~40s per attempt
instead of ~5s, and that trade isn't optional. Keep **Custom RTC off**
(Config → Advanced) or the boot seed is pinned and you're back in the dead end.

**The detector reads hue, not colour distance.** Your two crops differ in
brightness (V 0.48 vs 0.72) as much as in palette, so an RGB-distance detector
would mostly be measuring the lighting. Hue separates them cleanly — normal
Flaaffy's pink nose sits at ~335°, shiny's orange at ~13° — and survives the
Shadow aura after black-level + grey-world colour-cast removal. `selftest`
classifies 18 deliberately degraded copies of your references; all 18 pass.

Ambiguity always stops the hunt rather than continuing. A false stop costs you a
glance at a screenshot; a false "normal" costs you the shiny permanently.

## Setup, in order

Everything below needs the game running, which is why it is yours to do rather
than something I could finish.

**1. Bind Port 1 to the pipe** (Colosseum only reads Port 1, so this displaces
the HORIPAD currently configured there; it backs the file up first, and Dolphin
must be closed because it rewrites its config on exit):

```
python main.py --setup                    # creates the FIFO + a Dolphin profile
python scripts/pad_config.py pipe --port 1
```

Undo at any time with `python scripts/pad_config.py restore`.

**2. Smoke-test the pad.** Start the game, then:

```
python scripts/example.py     # via: python main.py scripts/example.py
```

If the menus respond, pipe input works. If `main.py` sits at "waiting for
Dolphin to open ...", the port is not bound to `Pipe/0/pipe1`.

**3. Save in-game next to Diogo** so the route has the least walking to do
after each reboot, and bind the Reset hotkey. Leave Dolphin focused, windowed,
and unmoved for the whole hunt — keystrokes go to the focused window.

**4. Calibrate the capture region.** Get into the battle by hand, then:

```
python scripts/hunt.py snap                              # full screenshot
python scripts/hunt.py crop --region X,Y,W,H --from snap.png
```

Aim at Flaaffy's head and nose — the pink/orange nose is the signal. Put the
rectangle in `hunt.toml` as `capture.region`, then watch it live:

```
python scripts/hunt.py watch
```

You want a steady `normal margin=-0.8` or so. Weak or flapping verdicts mean the
region is off, too tight, or catching the camera pan.

**5. Tune the route timings.** `route.boot` assumes six A presses get you from
the reboot into the battle:

```
python scripts/hunt.py route --sample
```

Adjust `taps`/`gap`/`wait` in `hunt.toml` until it lands in the battle with
Flaaffy on screen every time.

**6. Hunt.**

```
python scripts/hunt.py run            # or --max 20 for a trial
```

Progress prints per attempt; `runs/<timestamp>/log.csv` records every verdict and
`runs/<timestamp>/frames/` keeps the evidence. On a hit you get a critical
desktop notification, a terminal bell, and the battle is left sitting there with
all inputs released — the catch is yours to make, deliberately, rather than
something a script fumbles.

## Tuning notes

- `detect.margin` / `detect.min_score` — raise for stricter, more stop-happy
  behaviour; `selftest` prints the actual scores to calibrate against.
- `decide.uncertain_tolerance` — consecutive inconclusive attempts allowed
  before stopping. Default 2. Raising it buys unattended endurance at the cost
  of the safety property above.
- `dolphin.speed` — converts route frames to real time. Left at 1.0, route
  numbers are real-time frames (60 = 1 real second) regardless of emulation
  speed, which is how the current timings were tuned at `EmulationSpeed = 2.0`.
  Changing Dolphin's speed setting invalidates them either way.
- `dolphin.require_focus` — keystrokes go to the focused window, so the hunt
  refuses to send F1 unless Dolphin has focus. Don't turn this off.
