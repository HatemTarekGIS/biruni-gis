"""Insight: what the data says about itself.

spellcheck.py - the spatial spell checker («مدقق إملائي مكاني»): a value is checked against where its feature
lies (the district polygon around it, the nearest street, the numbering of its neighbours, what the neighbours
say, the measured geometry).
learned.py - rules learned from the data («قواعد تتعلّمها من بياناتك»): the implicit rules nobody wrote, with
their support and the features that break them, and broken_rules() for the drawing tool.

Both are pure analysis (no widgets); the dialogs live in spell_dialog.py and rules_dialog.py.
"""
