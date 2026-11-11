"""List every user-visible English string of the Biruni plugin and report which
ones are missing from a translation file.

Collected: tr("…") calls, ribbon tab/group titles, command labels, algorithm
NAME/GROUP/HELP attributes, option tables (ORDERS, FORMATS, *_UNITS) and the
toolbox catalogue (CATEGORIES titles, COMMANDS label/help, LABELS).

usage: python extract_strings.py <plugin dir> [i18n/ar.json ... | --all]
       (several files or --all print one coverage line per language)
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
         "MODES", "DIRECTIONS", "OUTPUTS", "METHODS", "COLS", "ACTIONS", "KINDS", "AUTO", "LABELS", "RULES",
         "KIND_LABELS", "GEOM_LABELS", "DIR_NAMES", "METHOD_NAMES", "KIND_NAMES", "VERDICTS",
         "BASES", "FIX_LEVELS", "FIX_NAMES", "NOTATION_LABELS",
         "ENCODING_CHOICES", "FONT_CHOICES", "STYLES", "ZONE_MODES", "COMMODITIES",
         "THEMES", "PRODUCTS", "PROOFS", "STANDARD_NAMES", "TERRAIN_PRESETS", "GROUPS"}
SECOND = {"CATEGORIES", "ENCODINGS", "OSM_SETS", "WORLDCOVER_CLASSES", "TABLES", "DRAWN", "POIS"}
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
            elif fname == "Intent" and len(args) >= 4:  # map_studio purposes: label and help text
                strings.update(s for s in (const(args[1]), const(args[3])) if s)

strings = sorted(strings)
PLACE = re.compile(r"\{[^{}]*\}")


def check(path, verbose=True):
    """Missing / unused / placeholder-mismatched entries of one translation file; True when complete."""
    have = json.load(open(path, encoding="utf-8"))
    missing = [s for s in strings if s not in have]
    extra = [k for k in have if k not in strings]
    bad = [k for k, v in have.items() if k in strings and sorted(PLACE.findall(k)) != sorted(PLACE.findall(v))]
    name = os.path.splitext(os.path.basename(path))[0]
    done = 100.0 * (len(strings) - len(missing)) / max(len(strings), 1)
    print(f"{name}: {len(strings)} strings, {len(missing)} missing, {len(extra)} unused, "
          f"{len(bad)} placeholder mismatches ({done:.1f}% translated)")
    if verbose:
        for s in missing:
            print("MISSING", json.dumps(s, ensure_ascii=False))
        for s in extra:
            print("UNUSED ", json.dumps(s, ensure_ascii=False))
        for s in bad:
            print("PLACEHOLDERS", json.dumps(s, ensure_ascii=False))
    return not missing and not bad


if len(sys.argv) > 2:
    # one or more language files, or --all for every i18n/*.json
    paths = sys.argv[2:]
    if paths == ["--all"]:
        folder = os.path.join(plugin, "i18n")
        paths = sorted(os.path.join(folder, f) for f in os.listdir(folder) if f.endswith(".json"))
    ok = [check(p, verbose=len(paths) == 1) for p in paths]
    sys.exit(0 if all(ok) else 1)
print(json.dumps(strings, ensure_ascii=False, indent=0))
