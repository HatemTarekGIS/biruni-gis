"""List every user-visible English string of the Biruni plugin and report which
ones are missing from a translation file.

Collected: tr("…") calls, ribbon tab/group titles, command labels, algorithm
NAME/GROUP/HELP attributes, option tables (ORDERS, FORMATS, *_UNITS) and the
toolbox catalogue (CATEGORIES titles, COMMANDS label/help, LABELS).

usage: python extract_strings.py <plugin dir> [i18n/ar.json]
"""
import ast
import json
import os
import re
import sys

plugin = sys.argv[1]
strings = set()


def const(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def first_strings(node, index=0):
    """index-th string of every tuple in a list/dict literal (or the strings of a plain list)."""
    out = []
    elts = node.values if isinstance(node, ast.Dict) else getattr(node, "elts", [])
    for e in elts:
        if const(e):
            out.append(const(e))
        elif isinstance(e, ast.Tuple) and len(e.elts) > index and const(e.elts[index]):
            out.append(const(e.elts[index]))
    return out


# option tables whose texts are shown through tr(): name -> which strings to take
FIRST = {"ORDERS", "FORMATS", "AREA_UNITS", "LENGTH_UNITS", "TITLES", "EXAMPLES", "S2_PRODUCTS", "LEVELS", "SPLIT",
         "MODES", "DIRECTIONS", "OUTPUTS", "METHODS", "COLS", "ACTIONS", "KINDS", "AUTO", "LABELS"}
SECOND = {"CATEGORIES", "ENCODINGS", "OSM_SETS", "WORLDCOVER_CLASSES", "TABLES"}
ALL = {"LESSONS"}             # every string inside
SKIP_FILES = {"theme.py"}     # its MODES are setting keys, not texts


def all_strings(node):
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


for dirpath, dirs, files in os.walk(plugin):
    dirs[:] = [d for d in dirs if d not in ("resources", "i18n", "__pycache__")]
    for fn in files:
        if not fn.endswith(".py") or fn in SKIP_FILES:
            continue
        tree = ast.parse(open(os.path.join(dirpath, fn), encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if len(targets) == 1 and isinstance(targets[0], ast.Name):
                    name = targets[0].id
                    if name in ("NAME", "GROUP", "HELP") and const(node.value):
                        strings.add(const(node.value))
                    elif name in FIRST:
                        strings.update(first_strings(node.value, 0))
                    elif name in SECOND:
                        strings.update(first_strings(node.value, 1))
                    elif name in ALL:  # minus command ids like "map_sheet"
                        strings.update(x for x in all_strings(node.value) if not re.fullmatch(r"[a-z_]+", x))
                    elif name == "COMMANDS":
                        strings.update(first_strings(node.value, 0))
                        strings.update(first_strings(node.value, 2))
                    elif name == "TERMS":  # the ArcGIS dictionary: only the notes are translated
                        strings.update(x for x in first_strings(node.value, 2) if x)
            if not isinstance(node, ast.Call):
                continue
            fname = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            args = node.args
            if fname == "tr" and args and const(args[0]):
                strings.add(const(args[0]))
            elif fname in ("add_tab", "group") and len(args) >= 2 and const(args[1]):
                strings.add(const(args[1]))
            elif fname == "_c" and len(args) >= 2 and const(args[1]):
                strings.add(const(args[1]))

strings = sorted(strings)
if len(sys.argv) > 2:
    have = json.load(open(sys.argv[2], encoding="utf-8"))
    missing = [s for s in strings if s not in have]
    extra = [k for k in have if k not in strings]
    print(f"{len(strings)} strings, {len(missing)} missing, {len(extra)} unused")
    for s in missing:
        print("MISSING", json.dumps(s, ensure_ascii=False))
    for s in extra:
        print("UNUSED ", json.dumps(s, ensure_ascii=False))
    sys.exit(1 if missing else 0)
print(json.dumps(strings, ensure_ascii=False, indent=0))
