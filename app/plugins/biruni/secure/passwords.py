"""Password strength, shown while a password is chosen. Offline there is no attempt counter that means
anything (the file can be copied and tried elsewhere), so the only protection against guessing is a
password that is not guessable plus the slow key derivation (crypto.derive)."""
import os
import re

MIN_ADMIN = 10
MIN_USER = 8
_common = None
_SEQUENCES = ("abcdefghijklmnopqrstuvwxyz", "0123456789", "qwertyuiop", "asdfghjkl", "zxcvbnm",
              "1qaz2wsx3edc", "qazwsxedc")
_LEET = str.maketrans("@4310$5!7", "aaeiosstt")


def common_words():
    """The common-password list (~1,000 entries); variants (word + digits or symbols, l33t spelling, years)
    are caught by is_common()."""
    global _common
    if _common is None:
        with open(os.path.join(os.path.dirname(__file__), "common_passwords.txt"), encoding="utf-8") as f:
            _common = {line.strip().casefold() for line in f if line.strip()}
    return _common


def _core(pw):
    """The password without trailing digits and symbols, in lower case, l33t letters read as letters."""
    s = pw.casefold()
    s2 = re.sub(r"[\d\W_]+$", "", s)
    s2 = re.sub(r"^[\d\W_]+", "", s2)
    return s, s2, s2.translate(_LEET), s.translate(_LEET)


def is_common(pw):
    words = common_words()
    s, core, leet, leet_all = _core(pw)
    if any(x and x in words for x in (s, core, leet, leet_all)):
        return True
    if len(set(s)) <= 2:
        return True  # aaaaaaaa, abababab
    for seq in _SEQUENCES:  # 12345678, qwertyui, zyxwvuts
        if s in seq or s in seq[::-1]:
            return True
    return False


def score(pw):
    """0 (very weak) to 4 (strong)."""
    pw = str(pw or "")
    if not pw:
        return 0
    if is_common(pw):
        return 0
    classes = sum(bool(re.search(p, pw)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^\w\s]|_", r"[^\x00-\x7f]"))
    n = len(pw)
    pts = 0
    pts += 1 if n >= 8 else 0
    pts += 1 if n >= 12 else 0
    pts += 1 if n >= 16 else 0
    pts += 1 if classes >= 3 else 0
    pts += 1 if classes >= 4 or (n >= 20 and classes >= 2) else 0
    if re.search(r"(.)\1\1", pw):
        pts -= 1
    if len(pw.split()) >= 4 and n >= 20:  # a pass-phrase of several words
        pts += 1
    return max(0, min(4, pts))


def problem(pw, minimum=MIN_USER):
    """Why a password is refused (an English message for tr()), or '' when it is accepted."""
    pw = str(pw or "")
    if len(pw) < minimum:
        return "At least {n} characters."
    if is_common(pw):
        return "This password is too common: it would be among the first guessed."
    if score(pw) < 2:
        return "Too easy to guess: make it longer, or mix letters, digits and symbols."
    return ""
