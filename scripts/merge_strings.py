"""Merge {lang: {english: translation}} JSON files into the plugin's i18n files (new keys only)."""
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
D = r"D:\Biruni-GIS\app\plugins\biruni\i18n"
PLACE = re.compile(r"\{[^{}]*\}")
COLL = re.compile(r"(?<![\u0621-\u064A])(مش|اللي|ده|دي|عشان|علشان|بتاع|كمان|دلوقتي|إيه|ليه|فولدر|التليفون)(?![\u0621-\u064A])")
for fp in sys.argv[1:]:
    src = json.load(open(fp, encoding="utf-8"))
    for lang, add in src.items():
        p = os.path.join(D, lang + ".json")
        d = json.load(open(p, encoding="utf-8"))
        n = 0
        for k, v in add.items():
            if sorted(PLACE.findall(k)) != sorted(PLACE.findall(v)):
                print("PLACEHOLDER MISMATCH", lang, k[:60])
                continue
            if lang == "ar" and COLL.search(v):
                print("COLLOQUIAL", k[:60], "->", v[:80])
            if k not in d:
                d[k] = v
                n += 1
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(d, ensure_ascii=False, indent=1) + "\n")
        print(os.path.basename(fp), lang, "+", n)
