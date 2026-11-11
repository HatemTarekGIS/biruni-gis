"""Translation round trip for Biruni's own strings (re-runnable: only missing keys are exported).

  python i18n_tool.py todo <work dir> [--chunk 150] [--langs es,pt_BR,fr,tr,fa]
      writes <work dir>/keys.json (id -> English) and src/cNNN.json chunks with every string that is
      missing from at least one of the languages. The source of truth is i18n/ar.json (its keys are the
      English strings, complete and checked by extract_strings.py).
  python i18n_tool.py merge <work dir> [--force]
      reads every <work dir>/out/*.json  {"<id>": ["es", "pt_BR", "fr", "tr", "fa"]}  (order = "langs" in
      keys.json), validates each translation and writes the good ones into i18n/<lang>.json (sorted keys,
      one space indent, UTF-8). Existing translations are kept unless --force.

Checks: not empty; the same {placeholders}; the same number of line breaks for short button labels; the
same HTML tags; "Biruni" kept. Persian gets ی/ک (not Arabic ي/ك); digits follow the Arabic file's choice.
"""
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
I18N = os.path.join(HERE, "..", "app", "plugins", "biruni", "i18n")
DEFAULT_LANGS = ["es", "pt_BR", "fr", "tr", "fa"]
PLACE = re.compile(r"\{[^{}]*\}")
TAG = re.compile(r"</?([a-zA-Z0-9]+)[^>]*>")
ARABIC_INDIC = "٠١٢٣٤٥٦٧٨٩"
PERSIAN = "۰۱۲۳۴۵۶۷۸۹"


def _load(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save(path, data):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(dict(sorted(data.items())), ensure_ascii=False, indent=1) + "\n")


def source_strings():
    return sorted(_load(os.path.join(I18N, "ar.json")))


def problems(english, text, lang):
    """Why a translation is not acceptable ([] when fine)."""
    out = []
    if not isinstance(text, str) or not text.strip():
        return ["empty"]
    if sorted(PLACE.findall(english)) != sorted(PLACE.findall(text)):
        out.append("placeholders")
    if "\n" in english and len(english) <= 60 and english.count("\n") != text.count("\n"):
        out.append("line breaks")
    if sorted(t.lower() for t in TAG.findall(english)) != sorted(t.lower() for t in TAG.findall(text)):
        out.append("html tags")
    if "Biruni" in english and "Biruni" not in text:
        out.append("brand")
    return out


def polish(english, text, lang, arabic):
    """Small language rules applied before saving."""
    if lang == "fa":
        text = text.replace("ي", "ی").replace("ك", "ک")
        if not any(ch in (arabic or "") for ch in ARABIC_INDIC):
            text = text.translate(str.maketrans(PERSIAN + ARABIC_INDIC, "0123456789" * 2))
        else:
            text = text.translate(str.maketrans(ARABIC_INDIC, PERSIAN))
    return text


def todo(work, chunk=150, langs=DEFAULT_LANGS):
    keys = source_strings()
    have = {lang: _load(os.path.join(I18N, f"{lang}.json")) for lang in langs}
    missing = [k for k in keys if any(k not in have[lang] for lang in langs)]
    os.makedirs(os.path.join(work, "src"), exist_ok=True)
    os.makedirs(os.path.join(work, "out"), exist_ok=True)
    ids = {str(i): k for i, k in enumerate(missing)}
    with open(os.path.join(work, "keys.json"), "w", encoding="utf-8") as f:
        json.dump({"langs": langs, "keys": ids}, f, ensure_ascii=False, indent=0)
    for old in glob.glob(os.path.join(work, "src", "c*.json")):
        os.remove(old)
    for n, start in enumerate(range(0, len(missing), chunk)):
        part = {str(i): missing[i] for i in range(start, min(start + chunk, len(missing)))}
        with open(os.path.join(work, "src", f"c{n:03d}.json"), "w", encoding="utf-8") as f:
            json.dump(part, f, ensure_ascii=False, indent=0)
    print(f"{len(missing)} strings to translate into {', '.join(langs)} in "
          f"{(len(missing) + chunk - 1) // chunk} chunks")


def merge(work, force=False):
    meta = _load(os.path.join(work, "keys.json"))
    langs, ids = meta["langs"], meta["keys"]
    arabic = _load(os.path.join(I18N, "ar.json"))
    files = {lang: _load(os.path.join(I18N, f"{lang}.json")) for lang in langs}
    added, rejected = {lang: 0 for lang in langs}, []
    for path in sorted(glob.glob(os.path.join(work, "out", "*.json"))):
        for sid, values in _load(path).items():
            english = ids.get(sid)
            if english is None or not isinstance(values, list) or len(values) != len(langs):
                rejected.append((os.path.basename(path), sid, "-", "bad entry"))
                continue
            for lang, text in zip(langs, values):
                why = problems(english, text, lang)
                if why:
                    rejected.append((os.path.basename(path), sid, lang, ", ".join(why)))
                    continue
                if force or english not in files[lang]:
                    files[lang][english] = polish(english, text, lang, arabic.get(english))
                    added[lang] += 1
    valid = set(source_strings())
    for lang in langs:
        files[lang] = {k: v for k, v in files[lang].items() if k in valid}  # drop strings no longer used
        _save(os.path.join(I18N, f"{lang}.json"), files[lang])
    for lang in langs:
        print(f"{lang}: +{added[lang]} → {len(files[lang])}/{len(valid)}")
    for r in rejected:
        print("REJECTED", *r)
    return not rejected


if __name__ == "__main__":
    cmd, work = sys.argv[1], sys.argv[2]
    args = sys.argv[3:]
    if cmd == "todo":
        size = int(args[args.index("--chunk") + 1]) if "--chunk" in args else 150
        langs = args[args.index("--langs") + 1].split(",") if "--langs" in args else DEFAULT_LANGS
        todo(work, size, langs)
    elif cmd == "merge":
        sys.exit(0 if merge(work, "--force" in args) else 1)
    else:
        sys.exit(__doc__)
