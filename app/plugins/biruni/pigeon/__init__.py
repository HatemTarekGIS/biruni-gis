"""Carrier pigeon: a team room's changes carried by hand where there is no network at all.

Each computer of a room makes a pack (a file, or a run of codes on the screen read by a camera) holding what it
has for the others: a member's changes for the host, the host's stream for the members, and the packs it is
carrying for someone else. Whoever opens it takes what is for them and carries the rest on, so a change reaches
the host after any number of hand-overs (store, carry, forward). Packs are sealed with the room key and signed by
whoever made them; the host decides every change exactly as if it had come over the network.

Modules: core (making, opening and carrying packs), ui (the window).
"""
