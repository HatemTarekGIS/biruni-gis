"""Team computing: the idle computers of a team room become one free processing grid.

A heavy job (overlaps and gaps of a whole city's parcels, a buffer of every pipe dissolved, the slope of a large
DEM) is cut into tiles (split.py). The room's host keeps a board of the tiles (jobs.Board, reached through the
room's own sealed messages: work_* requests of team/hub.py); the members who chose to share their computer take
tiles when they are not editing, run them in a separate low-priority process (runner.py) and send back the result,
signed with their member key (worker.py). The computer that asked keeps a lane of its own, so nothing waits for a
worker that left, and checks a share of the tiles twice - every tile of a member it has not worked with yet - so a
worker that sends a wrong answer is caught, marked and its tiles computed again (owner.py). Then it stitches the
tiles into one result that is the same as a run on one computer.

Only a fixed list of tools runs on a worker (jobs.TASKS) with checked parameters; the data travels as numbers and
geometries in the message, never a path or code.
"""
