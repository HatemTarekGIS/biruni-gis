"""«اللوحة بتحكي»: a few sentences about what changed since the viewer's last visit, from rules on the numbers -
no AI, nothing leaves the PC but the sentences.

story(prev, cur, titles, hist, lang, now, prev_ts) -> [sentence]
* prev / cur: showkit summaries (what that card sees: zone cut, k_min and hidden fields already applied)
* titles: {widget id: title} of the widgets the card may see
* hist: [(day, ts, summary)] for records and unusual days

Arabic is Modern Standard Arabic with the number agreement of the common cases (يوم واحد، يومان، ثلاثة أيام،
أحد عشر يومًا…) and phrasings that do not depend on the gender of a person's or a place's name.
"""
import math
import statistics

MAX_SENTENCES = 7


# ---- numbers and durations ------------------------------------------------------------------------------------------
def num(v, lang="en"):
    if v is None:
        return ""
    if isinstance(v, float) and not v.is_integer():
        a = abs(v)
        s = f"{v:,.2f}" if a < 100 else f"{v:,.1f}" if a < 10000 else f"{v:,.0f}"
        s = s.rstrip("0").rstrip(".") if "." in s else s
    else:
        s = f"{int(round(v)):,}"
    if lang in ("ar", "fa"):
        s = s.replace(",", "٬")
    return s


def ar_count(n, one, two, plural, acc):
    """«n + noun» with Arabic agreement: one = singular (يوم), two = dual (يومان), plural = 3-10 (أيام),
    acc = 11-99 (يومًا)."""
    n = int(n)
    if n == 1:
        return f"{one} واحد" if not one.endswith("ة") else f"{one} واحدة"
    if n == 2:
        return two
    r = n % 100
    if 3 <= r <= 10:
        return f"{n} {plural}"
    if 11 <= r <= 99:
        return f"{n} {acc}"
    return f"{n} {one}"


def span(seconds, lang="en"):
    s = max(0, int(seconds))
    m, h, d = s // 60, s // 3600, s // 86400
    if lang == "ar":
        if d >= 1:
            return ar_count(d, "يوم", "يومين", "أيام", "يومًا")
        if h >= 1:
            return ar_count(h, "ساعة", "ساعتين", "ساعات", "ساعة")
        if m >= 1:
            return ar_count(m, "دقيقة", "دقيقتين", "دقائق", "دقيقة")
        return "لحظات"
    if d >= 1:
        return f"{d} day" + ("s" if d != 1 else "")
    if h >= 1:
        return f"{h} hour" + ("s" if h != 1 else "")
    if m >= 1:
        return f"{m} minute" + ("s" if m != 1 else "")
    return "a moment"


# ---- the sentences --------------------------------------------------------------------------------------------------
def _q(t, lang):
    return f"«{t}»" if lang == "ar" else f"“{t}”"


def _rel(a, b):
    return abs(a - b) / max(abs(b), 1e-9) if b else (1.0 if a else 0.0)


def story(prev, cur, titles, hist=None, lang="en", now=None, prev_ts=None):
    ar = lang == "ar"
    L = (lambda a, e: a if ar else e)
    out = []
    titles = titles or {}
    cur = {k: v for k, v in (cur or {}).items() if k in titles}
    prev = {k: v for k, v in (prev or {}).items() if k in titles} if prev else None
    if prev is None:
        out.append(L("هذه زيارتك الأولى لهذه اللوحة. إليك أبرز ما فيها الآن:",
                     "Your first visit to this dashboard. Here is what stands out now:"))
    else:
        ago = span((now or 0) - (prev_ts or 0), lang) if now and prev_ts else ""
        out.append(L(f"منذ زيارتك الأخيرة قبل {ago}:" if ago else "منذ زيارتك الأخيرة:",
                     f"Since your last visit {ago} ago:" if ago else "Since your last visit:"))

    # 1. the main numbers
    kpis = [(wid, s) for wid, s in cur.items() if s.get("type") in ("kpi", "gauge")]
    if prev is None:
        for wid, s in kpis[:3]:
            out.append(f"{_q(titles[wid], lang)}: {num(s['v'], lang)}.")
    else:
        moved = []
        for wid, s in kpis:
            p = (prev.get(wid) or {}).get("v")
            if isinstance(p, (int, float)) and s["v"] != p:
                moved.append((_rel(s["v"], p), wid, p, s["v"]))
        moved.sort(reverse=True)
        for _r, wid, p, v in moved[:3]:
            d = num(abs(v - p), lang)
            if v > p:
                out.append(L(f"ارتفع {_q(titles[wid], lang)} بمقدار {d} ليبلغ {num(v, lang)}.",
                             f"{_q(titles[wid], lang)} rose by {d} to {num(v, lang)}."))
            else:
                out.append(L(f"انخفض {_q(titles[wid], lang)} بمقدار {d} إلى {num(v, lang)}.",
                             f"{_q(titles[wid], lang)} fell by {d} to {num(v, lang)}."))
        if kpis and not moved:
            out.append(L("لم تتغيّر الأرقام الرئيسية.", "The main numbers have not changed."))

    # 2. progress by zone: the one that moved most, the ones finished, the one behind
    for wid, s in cur.items():
        if s.get("type") != "progress":
            continue
        prog, labels = s["prog"], s.get("labels") or {}
        pp = ((prev or {}).get(wid) or {}).get("prog") or {}
        if prev is not None:
            gains = sorted(((prog[k][0] - pp[k][0], k) for k in prog if k in pp and prog[k][0] > pp[k][0]),
                           reverse=True)
            if gains:
                g, k = gains[0]
                out.append(L(f"أكثر المناطق تقدّمًا: {labels.get(k, k)}، إذ زاد المُنجَز فيها بمقدار {num(g, lang)}.",
                             f"Most progress: {labels.get(k, k)} (+{num(g, lang)})."))
            for k in prog:
                done, total = prog[k]
                if total and done >= total and k in pp and pp[k][0] < pp[k][1]:
                    out.append(L(f"اكتمل العمل في {labels.get(k, k)}.", f"{labels.get(k, k)} is complete."))
        pcts = {k: 100.0 * v[0] / v[1] for k, v in prog.items() if v[1]}
        if len(pcts) >= 3:
            avg = statistics.mean(pcts.values())
            k = min(pcts, key=pcts.get)
            if pcts[k] < avg - 10:
                out.append(L(f"نسبة الإنجاز في {labels.get(k, k)} {num(round(pcts[k]), lang)}٪ فقط، وهي أقل من "
                             f"متوسط المناطق ({num(round(avg), lang)}٪).",
                             f"{labels.get(k, k)} is at {num(round(pcts[k]), lang)}% only, below the average of "
                             f"{num(round(avg), lang)}%."))
        break

    # 3. leaders
    for wid, s in cur.items():
        if s.get("type") != "leaderboard" or not s.get("items"):
            continue
        items, labels = s["items"], s.get("labels") or {}
        top = max(items, key=items.get)
        old = None
        pi = ((prev or {}).get(wid) or {}).get("items") or {}
        if pi:
            old = max(pi, key=pi.get)
        if old is not None and old != top and old in labels:
            out.append(L(f"صدارة {_q(titles[wid], lang)} الآن لـ{labels.get(top, top)} بعد أن كانت لـ{labels[old]}.",
                         f"{labels.get(top, top)} now leads {_q(titles[wid], lang)}, ahead of {labels[old]}."))
        else:
            out.append(L(f"في صدارة {_q(titles[wid], lang)}: {labels.get(top, top)} ({num(items[top], lang)}).",
                         f"Leading {_q(titles[wid], lang)}: {labels.get(top, top)} ({num(items[top], lang)})."))
        break

    # 4. records and unusual days, from the history
    hist = hist or []
    for wid, s in kpis:
        series = [(day, h.get(wid, {}).get("v")) for day, _ts, h in hist]
        series = [(d, v) for d, v in series if isinstance(v, (int, float))]
        if len(series) >= 4:
            before = [v for _d, v in series[:-1]]
            if s["v"] > max(before) and (prev is None or (prev.get(wid) or {}).get("v", s["v"]) < s["v"]):
                out.append(L(f"{_q(titles[wid], lang)} عند أعلى قيمة مسجّلة حتى الآن: {num(s['v'], lang)}.",
                             f"{_q(titles[wid], lang)} is at its highest so far: {num(s['v'], lang)}."))
        if len(series) >= 6:
            deltas = [b[1] - a[1] for a, b in zip(series, series[1:])]
            past, today = deltas[:-1], deltas[-1]
            if len(past) >= 5:
                m, sd = statistics.mean(past), statistics.pstdev(past)
                if sd > 0 and abs(today - m) / sd > 2.5 and abs(today) > abs(m):
                    out.append(L(f"تغيّر {_q(titles[wid], lang)} في آخر يوم بمقدار {num(abs(today), lang)}، وهو أكثر "
                                 f"بكثير من المعتاد (نحو {num(round(abs(m), 1), lang)} يوميًا).",
                                 f"{_q(titles[wid], lang)} moved by {num(abs(today), lang)} in the last day - far "
                                 f"more than usual (about {num(round(abs(m), 1), lang)} a day)."))
    if len(out) == 1:
        out.append(L("لا جديد يستحق الذكر.", "Nothing new worth mentioning."))
    return out[:MAX_SENTENCES]


def titles_of(def_for_card):
    return {w.get("id"): str(w.get("title") or "") or w.get("id") for w in (def_for_card or {}).get("widgets") or []}


def _finite(v):
    return isinstance(v, (int, float)) and math.isfinite(v)
