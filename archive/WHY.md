# Archived — superseded, kept because they are not trivially regenerable

- `walk.toml`, `launch.toml` — folded into `start.toml`, which covers title
  screen through to standing at Diogo.
- `catch_loop.toml` — the recorded catch sequence. No longer replayed: the
  closed loop in `hunt.py` fights and catches by reading the screen, which is
  what fixed the run-to-run drift.
- `input_read.py` — the original pygame reader. Superseded by
  `python scripts/record.py monitor`, which also maps buttons to GC names.
- `inpus_list.md` — early pad button indices (now `controller_map.json`) and
  the launch command (now in the README).
- `timing.md` — hand-tuned launch timings, now recorded in `start.toml`.
- `diogo_route.py` — hand-tuning scratchpad, superseded by `record.py`.

Delete this directory whenever you like; nothing references it.
