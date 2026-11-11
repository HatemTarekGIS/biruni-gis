"""A light Python 2 -> 3 fix-up for old ArcGIS 10.x scripts, without lib2to3: print statements, `except E, e`,
`<>`, `ur''` strings, `exec code` and octal/long literals. Anything harder is left as it is (the script then
reports a syntax error)."""
import re

PY2_BUILTINS = {"unicode": str, "basestring": str, "long": int, "xrange": range, "raw_input": input,
                "unichr": chr}


def _fix_print_line(line):
    m = re.match(r"^(\s*)print(\s+(.*?))?\s*(#.*)?$", line)
    if not m:
        return line
    indent, rest, comment = m.group(1), (m.group(3) or ""), m.group(4) or ""
    if rest.startswith("(") and rest.endswith(")") and _balanced(rest[1:-1]):
        return line  # already a call
    if rest.startswith(">>"):
        target, _sep, args = rest[2:].partition(",")
        call = f"print({args.strip()}, file={target.strip()})" if args.strip() else f"print(file={target.strip()})"
    elif rest.endswith(","):
        call = f"print({rest[:-1].rstrip()}, end=' ')"
    else:
        call = f"print({rest})"
    return f"{indent}{call}" + (f"  {comment}" if comment else "")


def _balanced(text):
    depth = 0
    quote = None
    for c in text:
        if quote:
            if c == quote:
                quote = None
            continue
        if c in "'\"":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def fix_python2(text):
    """-> (fixed text, changed?)."""
    original = text
    text = re.sub(r"\bur(['\"])", r"r\1", text)
    text = re.sub(r"\s<>\s", " != ", text)
    text = re.sub(r"^(\s*except\s+[\w.]+(?:\s*\([^)]*\))?)\s*,\s*(\w+)\s*:", r"\1 as \2:", text, flags=re.M)
    text = re.sub(r"^(\s*except\s*\(([^)]*)\))\s*,\s*(\w+)\s*:", r"\1 as \3:", text, flags=re.M)
    text = re.sub(r"^(\s*)exec\s+(?!\()(.+)$", r"\1exec(\2)", text, flags=re.M)
    text = re.sub(r"^(\s*)raise\s+([\w.]+)\s*,\s*(.+)$", r"\1raise \2(\3)", text, flags=re.M)
    lines = text.splitlines(keepends=True)
    out = list(lines)
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if re.match(r"print(\s|$|>>)", stripped) and not re.match(r"print\s*\(", stripped):
            # join continuation lines ending with a backslash
            nl = "\n" if line.endswith("\n") else ""
            out[i] = _fix_print_line(line.rstrip("\r\n")) + nl
        elif re.match(r"print\s*\(.*\)\s*,\s*$", stripped):
            nl = "\n" if line.endswith("\n") else ""
            out[i] = line.rstrip("\r\n").rstrip().rstrip(",") + nl
    text = "".join(out)
    text = re.sub(r"\.has_key\(([^()]+)\)", r".__contains__(\1)", text)
    text = re.sub(r"\.iteritems\(\)", ".items()", text)
    text = re.sub(r"\.itervalues\(\)", ".values()", text)
    text = re.sub(r"\.iterkeys\(\)", ".keys()", text)
    return text, text != original
