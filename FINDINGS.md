# UI mapping session — what I worked out while you slept

Everything below was measured against the live game and verified. All regions
are **fractions of the game viewport**, not screen pixels.

## The big structural correction

Regions were keyed to absolute screen coordinates from your reference
screenshots. niri reports **no absolute window position** on a scrolling
layout, and Dolphin had already moved since those shots — so every region was
stale before the first attempt. Captures now go through
`niri msg action screenshot-window --id <dolphin>`, and regions are fractions
of the letterboxed game area. Verified across three completely different
layouts (2400×1500 fullscreen, 944×1132 windowed, 1920×1200 fullscreen): the
`flaaffy_on_field` template taken at one resolution and HP/status scores
**0.988** at another.

## Discoveries that changed the design

**The battle menu is semi-transparent.** The 3D scene shows through it, so
structural matching over the menu box swings with whatever animates behind.
The cursor arrow is a saturated red nothing else uses — counting those pixels
gives 0.23–0.26 with the cursor and **exactly 0.000** without, immune to the
background. Same trick for the cyan target arrow (0.298 vs 0.000).

**Colosseum is a DOUBLE battle.** Both your Pokémon get an input before the
turn executes. The name box says which one the game is asking for, so
`turn_umbreon` / `turn_espeon` distinguish them.

**Throwing a ball is four steps, not one.** Bag → pocket → item → a
**USE / CANCEL** prompt → **target selection** (cyan arrow, because there are
two opponents). I had neither the USE prompt nor targeting in the model.

**Your menu-memory point, confirmed empirically:** after backing out of the
bag the cursor sat on ITEMS, not FIGHT. Every `until` step presses zero times
when already on target, so it does not matter.

**Wide regions do not discriminate.** The bag's whole title bar scores 0.925
between pockets (mostly identical chrome); the pocket title alone scores
0.145. The full name box scores 0.733; tight on the text, 0.367.

## Bugs found by running it for real

1. **`Controller.wait()` slept zero after any screenshot.** It scheduled
   against a virtual clock that fell ~1s behind during a capture, so `tap()`
   pressed and released with no gap and the emulator never saw the button.
   Every capture-driven action silently no-opped. Fixed with a resync
   threshold. This would have broken the whole closed loop.
2. **`run_steps` dispatch order.** An `until` step carries a `press` key, so
   it matched the plain press branch and fired once instead of looping.
3. **Stale clipboard.** niri hands screenshots over asynchronously; without
   clearing first, `wl-paste` returns the *previous* capture and you analyse
   the wrong frame.

## The 13 verified checks

cursor_fight / cursor_items / cursor_pkmn / cursor_call · flaaffy_on_field ·
bag_balls_pocket · bag_row0_pokeball · party_open · turn_umbreon /
turn_espeon · target_flaaffy / target_shroomish · flaaffy_in_party

Re-verify any time with `python scripts/hunt.py check-test`.

## Live end-to-end result

Ran the closed loop against the real battle. It played `weaken` (BITE) for
UMBREON, `throw_ball` for ESPEON, detected Flaaffy leaving the field, opened
the party and read the portrait:

```
portrait -> normal  margin=-0.836  normal=0.0995  shiny=0.0089
```

0.0995 against the reference crop's 0.1013 — **the shiny call is validated on
a real snag**, not just on the reference images. That Flaaffy is a dud, so the
hunt would reset, which is exactly right.

The first ball broke out and the next turn simply came round with Flaaffy
still on the field, so "broke out -> retry" needs no detection at all.

Two more bugs this surfaced:

4. **`require` raced the screen transition** -- it checked once, instantly,
   and failed while the party screen was still drawing. Now polls to a
   timeout.
5. **`move_tl` collides with `cursor_fight`** -- the command menu's FIGHT
   arrow and the move list's top-left arrow are at nearly the same place.
   Added a `move_menu` check on the PP / MOVE TYPE panel (scores -0.060
   against the command menu) so a failed A press cannot be mistaken for an
   open move list.

## Battle strategy as configured

| whose turn | action | why |
|---|---|---|
| UMBREON | `weaken` — BITE at Flaaffy | your pick; ~90 effective with STAB |
| ESPEON | `throw_ball` | |
| either, below the HP gate | `stall` — SNATCH | no damage, no KO |

`flaaffy_hp_high` reads the HP bar fill (0.993 on a full bar) and gates the
attack at 35%. Without it, BITE keeps swinging at a nearly-dead Flaaffy and
eventually KOs it — which costs the whole attempt.

## Left for you — one decision I would not make alone

**The HP gate threshold is a guess at 35%.** I have no data on how much BITE
takes off per hit, so I cannot say whether 35% is a safe floor or already too
low. One battle's worth of HP readings would settle it.

**The `fainted` path is still untested** — it needs a Flaaffy to actually
faint, which I was not willing to cause deliberately. The logic is the same
branch as snagged (party open, name absent), so it is low risk, but it is
unproven.

## Game state right now

**A non-shiny Flaaffy is snagged and sitting in your party**, party screen
open. Two Poké Balls used. Reset to get back to a clean pre-battle state —
the hunt does that itself on every attempt. Savestate 4 is untouched.

I did not save, release, deposit, or change any setting.
