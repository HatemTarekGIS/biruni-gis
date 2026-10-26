"""Copy the Tabler icons referenced by the Biruni plugin into its resources.

Every string literal in the plugin sources that names an existing Tabler
outline icon is copied (extra copies are harmless). Icon names used by the
command catalogue must exist - the script fails loudly if one is missing.

usage: python collect_icons.py <tabler package dir> <plugin dir>
"""
import ast
import os
import re
import shutil
import sys

tabler, plugin = sys.argv[1], sys.argv[2]
src_dir = os.path.join(tabler, "icons", "outline")
dst_dir = os.path.join(plugin, "resources", "icons")
os.makedirs(dst_dir, exist_ok=True)

names = set()
for dirpath, dirs, files in os.walk(plugin):
    dirs[:] = [d for d in dirs if d not in ("resources", "__pycache__")]
    for fn in [f for f in files if f.endswith(".py")]:
        tree = ast.parse(open(os.path.join(dirpath, fn), encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.fullmatch(r"[a-z0-9-]+", node.value):
                names.add(node.value)

catalog_src = open(os.path.join(plugin, "commands.py"), encoding="utf-8").read()
required = set(re.findall(r'_c\("[^"]+",\s*"[^"]*",\s*"([a-z0-9-]+)"', catalog_src))
missing = sorted(n for n in required if not os.path.exists(os.path.join(src_dir, n + ".svg")))
if missing:
    sys.exit("missing catalogue icons: " + ", ".join(missing))

copied = 0
for n in sorted(names):
    s = os.path.join(src_dir, n + ".svg")
    if os.path.exists(s):
        shutil.copy2(s, os.path.join(dst_dir, n + ".svg"))
        copied += 1
shutil.copy2(os.path.join(tabler, "LICENSE"), os.path.join(dst_dir, "LICENSE"))
print(f"copied {copied} icons ({len(required)} required by the catalogue)")
