"""Hand-tuning scratchpad for the savestate -> battle sequence.

    python main.py scripts/diogo_route.py

One process, one pipe connection, frame-scheduled waits that don't drift.
Edit the numbers until it lands in the battle every time, then copy them into
hunt.toml's [route] boot list.
"""

wait_s(22)      # savestate load / whatever you are waiting out
tap("A")

wait_s(4)
tap("A")

wait_s(6)
tap("A")

wait_s(2)
tap("UP")
tap("A")
