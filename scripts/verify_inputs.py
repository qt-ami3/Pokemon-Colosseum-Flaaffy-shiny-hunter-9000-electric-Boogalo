"""Exercise every input one at a time, so you can watch them register.

    python main.py scripts/verify_inputs.py

Watch it in Dolphin: Controllers -> Port 1 -> Standard Controller -> Configure.
That dialog highlights each button as it is pressed and moves the stick preview
live, so you can confirm the pipe bindings without being in a battle. Anything
that stays dark is mis-bound in the profile.

Pay attention to the analog triggers: the L-Analog / R-Analog bindings in the
generated profile are the one part I could not verify without the game running.
"""

HOLD = 45   # frames each input is held (0.75s at 60fps)
GAP = 30    # frames between inputs

print("Focus Dolphin's controller Configure dialog now ...")
for n in (3, 2, 1):
    print(f"  {n}")
    wait(60)

print("\n-- buttons")
for name in ("A", "B", "X", "Y", "Z", "START",
             "D_UP", "D_DOWN", "D_LEFT", "D_RIGHT"):
    print(f"   {name}")
    press(name)
    wait(HOLD)
    release(name)
    wait(GAP)

print("\n-- digital shoulder clicks")
for name in ("L", "R"):
    print(f"   {name} (click)")
    press(name)
    wait(HOLD)
    release(name)
    wait(GAP)

print("\n-- analog triggers (should sweep, not just snap on)")
for name, set_trigger in (("L", l_trigger), ("R", r_trigger)):
    print(f"   {name} analog 0 -> 1")
    for step in range(0, 11):
        set_trigger(step / 10)
        wait(6)
    set_trigger(0)
    wait(GAP)

print("\n-- main stick")
for label, (x, y) in (("up", (0, 1)), ("down", (0, -1)),
                      ("left", (-1, 0)), ("right", (1, 0)),
                      ("half right", (0.5, 0)), ("diagonal up-right", (0.7, 0.7))):
    print(f"   {label}")
    stick(x, y)
    wait(HOLD)
    stick(0, 0)
    wait(GAP)

print("\n-- C-stick")
for label, (x, y) in (("up", (0, 1)), ("down", (0, -1)),
                      ("left", (-1, 0)), ("right", (1, 0))):
    print(f"   {label}")
    c_stick(x, y)
    wait(HOLD)
    c_stick(0, 0)
    wait(GAP)

print("\ndone -- anything that never lit up is mis-bound in the profile")
