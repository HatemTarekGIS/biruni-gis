"""Static check of an arcpy script: which arcpy names it uses and whether Biruni has them.

    analyse_script(text) -> {"calls": [{"name", "line", "status"}], "supported": n, "partial": n, "missing": n,
                             "score": 0-100, "missing_names": [...], ...}

Counts and the score are over the distinct names a script uses (a tool called 30 times counts once); "calls" lists
every use with its line. Python 2 scripts are read after the light print/except fix-up (see py2fix) and flagged
with "python2": True."""
import ast

from . import registry
from .py2fix import fix_python2

_SA_NAMES = None


def _sa_names():
    global _SA_NAMES
    if _SA_NAMES is None:
        from . import sa
        _SA_NAMES = set(sa.__all__)
    return _SA_NAMES


def _normalise(parts):
    """['arcpy', 'management', 'CopyFeatures'] / ['arcpy', 'CopyFeatures_management'] -> registry key."""
    if not parts or parts[0] != "arcpy":
        return None
    rest = parts[1:]
    if not rest:
        return None
    head = rest[0]
    if head == "gp" and len(rest) > 1:
        rest = rest[1:]
        head = rest[0]
    if head in registry.TOOLBOX_MODULES and len(rest) > 1:
        return f"{head}.{rest[1]}"
    if head in registry.TOOLBOX_MODULES and len(rest) == 1:
        return None  # the module itself (e.g. `from arcpy import da`)
    if "_" in head:
        base, _s, box = head.rpartition("_")
        alias = registry.TOOLBOX_ALIASES.get(box.lower())
        if alias and base:
            if alias == "sa" and base.lower() in ("slope", "aspect", "hillshade"):
                return f"ddd.{'HillShade' if base.lower() == 'hillshade' else base}"
            return f"{alias}.{base}"
    return head


class _Visitor(ast.NodeVisitor):
    def __init__(self):
        self.aliases = {}       # local name -> ['arcpy', ...]
        self.star_modules = []  # ['arcpy', 'sa'] from `from arcpy.sa import *`
        self.uses = []          # (key, line, is_call)
        self.imports_arcpy = False

    # imports ------------------------------------------------------------------
    def visit_Import(self, node):  # noqa: N802
        for a in node.names:
            if a.name == "arcpy" or a.name.startswith("arcpy."):
                self.imports_arcpy = True
                if a.asname:
                    self.aliases[a.asname] = a.name.split(".")
                else:
                    self.aliases["arcpy"] = ["arcpy"]
        self.generic_visit(node)

    def visit_ImportFrom(self, node):  # noqa: N802
        mod = node.module or ""
        if mod == "arcpy" or mod.startswith("arcpy."):
            self.imports_arcpy = True
            base = mod.split(".")
            for a in node.names:
                if a.name == "*":
                    self.star_modules.append(base)
                    continue
                self.aliases[a.asname or a.name] = base + [a.name]
                key = _normalise(base + [a.name])
                if key is not None and not (len(base) == 1 and a.name in registry.TOOLBOX_MODULES):
                    self.uses.append((key, node.lineno, False))
        self.generic_visit(node)

    # uses ---------------------------------------------------------------------
    def _chain(self, node):
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
            return list(reversed(parts))
        return None

    def _resolve(self, chain):
        if not chain:
            return None
        head = chain[0]
        if head in self.aliases:
            return self.aliases[head] + chain[1:]
        for star in self.star_modules:
            if len(chain) == 1 and star == ["arcpy", "sa"] and head in _sa_names():
                return ["arcpy", "sa", head]
            if len(chain) == 1 and star == ["arcpy"] and head in registry.COVERAGE:
                return ["arcpy", head]
        return None

    def _record(self, node, is_call):
        full = self._resolve(self._chain(node))
        if not full or full[0] != "arcpy":
            return False
        # env.X: keep env + setting; tools: module + tool; top-level: the name
        if len(full) >= 3 and full[1] == "env":
            key = f"env.{full[2]}"
        elif len(full) >= 2 and full[1] == "env":
            return True
        else:
            key = _normalise(full[:3] if len(full) >= 3 and full[1] in registry.TOOLBOX_MODULES else full[:2])
        if key:
            self.uses.append((key, node.lineno, is_call))
        return True

    def visit_Call(self, node):  # noqa: N802
        if isinstance(node.func, (ast.Attribute, ast.Name)) and self._record(node.func, True):
            for a in node.args:
                self.visit(a)
            for k in node.keywords:
                self.visit(k.value)
            return
        self.generic_visit(node)

    def visit_Attribute(self, node):  # noqa: N802
        chain = self._chain(node)
        full = self._resolve(chain) if chain else None
        if full and full[0] == "arcpy":
            if len(full) >= 3 and full[1] == "env":
                self.uses.append((f"env.{full[2]}", node.lineno, False))
                return
            if len(full) == 2 and full[1] in ("ExecuteError", "ExecuteWarning"):
                self.uses.append((full[1], node.lineno, False))
                return
            if len(full) == 2 and full[1] not in registry.TOOLBOX_MODULES:
                self.uses.append((_normalise(full), node.lineno, False))
                return
            if len(full) >= 3 and full[1] in registry.TOOLBOX_MODULES and full[1] != "env":
                self.uses.append((_normalise(full[:3]), node.lineno, False))
                return
        self.generic_visit(node)

    def visit_Name(self, node):  # noqa: N802
        if isinstance(node.ctx, ast.Load):
            full = self._resolve([node.id])
            if full and full[0] == "arcpy" and len(full) >= 2 and full[1] != "env" and \
                    full[1] not in registry.TOOLBOX_MODULES:
                self.uses.append((_normalise(full), node.lineno, False))
            elif full and full[:2] == ["arcpy", "sa"] and len(full) == 3:
                self.uses.append((f"sa.{full[2]}", node.lineno, False))


def analyse_script(text):
    """See the module docstring. Never raises on bad input; a script that cannot be parsed gets score 0."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    text = text or ""
    python2 = False
    error = None
    import warnings

    try:
        with warnings.catch_warnings():  # "\<" and the like in user scripts: harmless, keep stderr quiet
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(text)
    except SyntaxError:
        fixed, changed = fix_python2(text)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(fixed)
            python2 = changed
        except SyntaxError as e:
            tree = None
            error = f"line {e.lineno}: {e.msg}"
    result = {"calls": [], "supported": 0, "partial": 0, "missing": 0, "score": 0, "missing_names": [],
              "partial_names": [], "python2": python2, "needs_2to3": python2, "imports_arcpy": False,
              "parse_error": error, "total_calls": 0}
    if tree is None:
        return result
    v = _Visitor()
    v.visit(tree)
    result["imports_arcpy"] = v.imports_arcpy
    seen = {}
    for key, line, _is_call in v.uses:
        if not key:
            continue
        st = registry.status(key)
        result["calls"].append({"name": key, "line": line, "status": st})
        seen.setdefault(key, st)
    result["calls"].sort(key=lambda c: (c["line"], c["name"]))
    result["total_calls"] = len(result["calls"])
    for key, st in seen.items():
        result[st] += 1
    result["missing_names"] = sorted(k for k, st in seen.items() if st == "missing")
    result["partial_names"] = sorted(k for k, st in seen.items() if st == "partial")
    n = len(seen)
    result["score"] = 100 if n == 0 else int(round(100.0 * (result["supported"] + 0.5 * result["partial"]) / n))
    return result


def analyse_file(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    for enc in ("utf-8-sig", "cp1256", "latin-1"):
        try:
            return analyse_script(raw.decode(enc))
        except UnicodeDecodeError:
            continue
    return analyse_script(raw.decode("utf-8", errors="replace"))
