"""Example input script: run with `python main.py scripts/example.py`.

Everything here is plain Python; `pad` and the shorthand helpers are injected
by main.py.  Waits are in frames (60 = one second at 60fps).
"""

# Skip through a title screen / intro.
for _ in range(3):
    tap("START")
    wait(45)

# Walk right for a second, then stop.
stick(1, 0)
wait(60)
stick(0, 0)

# Hold B while tapping A (running while talking, in Colosseum terms).
with hold("B"):
    tap("A", frames=4)
    wait(20)
    tap("A", frames=4)

# Analog trigger: half press, then the full digital click.
l_trigger(0.5)
wait(30)
press("L")
wait(10)
release("L")
l_trigger(0)

# A held combo.  Note for hunters: B+X+Start soft resets Pokemon XD, but
# Pokemon Colosseum has no controller soft reset at all -- scripts/hunt.py
# resets through a Dolphin savestate hotkey instead.
press("B", "X", "START")
wait(15)
release("B", "X", "START")
wait(180)
