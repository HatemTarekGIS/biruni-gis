"""The cloud mailbox's rows on the System Health screen (no-loss P6).

Reads `mailbox_state.json` and settings only (no network, no QGIS import).
Registered with `health.checks` at order 27 when imported in `health/__init__.py`.
"""
import json
import os

from ..health import checks
from ..i18n import tr

GROUP = "Cloud mailbox"
DAY = 86400.0
FREE_BYTES = 10 * 1000 * 1000 * 1000   # 10 GB free on Cloudflare R2
FREE_MONTH_PUTS = 1_000_000            # 1,000,000 Class A operations/month free on R2


def _row(rid, title, level, text, fix=None):
    return {"id": rid, "group": tr(GROUP), "title": tr(title), "level": level, "text": text, "fix": fix}


def _days(now, at):
    return max(0, int((now - float(at)) / DAY))


def _bool_setting(ctx, key):
    v = ctx.setting(key, False)
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _load_state(cert_folder):
    if not cert_folder:
        return {}
    try:
        with open(os.path.join(cert_folder, "mailbox_state.json"), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _fmt_bytes(b):
    b = max(0, int(b or 0))
    if b >= 1000 * 1000 * 1000:
        return f"{b / (1000 * 1000 * 1000):.1f} GB"
    if b >= 1000 * 1000:
        return f"{b / (1000 * 1000):.1f} MB"
    if b >= 1000:
        return f"{b / 1000:.1f} KB"
    return f"{b} B"


def _check_mailbox(ctx):
    host = str(ctx.setting(checks.ORG_RELAY_SETTING, "") or "").strip()
    mb_on = _bool_setting(ctx, "biruni/mailbox/on")
    has_secret = bool(str(ctx.setting("biruni/mailbox/secret", "") or "").strip())
    st = _load_state(ctx.cert_folder)
    if not host and not mb_on and not has_secret and not st:
        return []

    now = ctx.now
    rows = []

    # ---- 1. mailbox_relay -----------------------------------------------------------------------------------------
    rel = st.get("rel")
    mb_flag = st.get("mb")
    if "rel" in st and rel is None:
        rows.append(_row(
            "mailbox_relay",
            "Cloud mailbox",
            "warn",
            tr("This relay is an older version. Press Deploy again to update it."),
            "org_relay",
        ))
        return rows
    if ("mb" in st and not mb_flag) or (host and not mb_on and not st):
        rows.append(_row(
            "mailbox_relay",
            "Cloud mailbox",
            "na",
            tr("The organization relay has no cloud mailbox enabled. Google Drive remains the first mailbox."),
            None,
        ))
        return rows

    rows.append(_row(
        "mailbox_relay",
        "Cloud mailbox",
        "ok",
        tr("Cloud mailbox is active on {host}.").format(host=host or st.get("host") or "Cloudflare"),
        None,
    ))

    # ---- 2. mailbox_pull ------------------------------------------------------------------------------------------
    at = st.get("at")
    err = str(st.get("err") or "").strip()
    ok = st.get("ok") is not False and not err
    if not at:
        rows.append(_row(
            "mailbox_pull",
            "Mailbox intake",
            "warn",
            tr("The cloud mailbox has not been checked yet. It runs while Field Photos is started."),
            None,
        ))
    else:
        age_d = (now - float(at)) / DAY
        n_days = _days(now, at)
        if not ok and age_d >= 7.0:
            rows.append(_row(
                "mailbox_pull",
                "Mailbox intake",
                "bad",
                tr("The cloud mailbox could not be checked for {n} day(s) ({e}).").format(n=n_days, e=err or "error"),
                "org_relay",
            ))
        elif not ok or age_d >= 2.0:
            txt = (
                tr("The last cloud mailbox check failed ({e}); last checked {n} day(s) ago.").format(e=err, n=n_days)
                if not ok
                else tr("The cloud mailbox was last checked {n} day(s) ago.").format(n=n_days)
            )
            rows.append(_row("mailbox_pull", "Mailbox intake", "warn", txt, None))
        else:
            rows.append(_row(
                "mailbox_pull",
                "Mailbox intake",
                "ok",
                tr("Cloud mailbox: {n} new, {r} refused").format(
                    n=int(st.get("new") or 0), r=int(st.get("refused") or 0)
                ),
                None,
            ))

    # ---- 3. mailbox_bytes -----------------------------------------------------------------------------------------
    used_b = max(0, int(st.get("bytes") or 0))
    ratio_b = used_b / float(FREE_BYTES)
    lvl_b = "bad" if ratio_b >= 0.90 else "warn" if ratio_b >= 0.70 else "ok"
    used_str = _fmt_bytes(used_b)
    bytes_txt = tr("{used} of 10 GB free used").format(used=used_str)
    if lvl_b != "ok":
        bytes_txt += " — " + tr(
            "Above the 10 GB free tier, Cloudflare bills R2 storage to the organisation's Cloudflare account."
        )
    rows.append(_row("mailbox_bytes", "Mailbox storage", lvl_b, bytes_txt, "org_relay" if lvl_b != "ok" else None))

    # ---- 4. mailbox_quota -----------------------------------------------------------------------------------------
    puts = max(0, int(st.get("month_puts") or 0))
    ratio_q = puts / float(FREE_MONTH_PUTS)
    lvl_q = "bad" if ratio_q >= 0.90 else "warn" if ratio_q >= 0.70 else "ok"
    quota_txt = tr(
        "{n} of 1,000,000 free monthly R2 writes used (Workers free tier also allows 100,000 requests/day). "
        "The $5 Workers plan is optional and billed to the organisation, not to Biruni."
    ).format(n=puts)
    rows.append(_row("mailbox_quota", "Mailbox quota", lvl_q, quota_txt, None))

    return rows


_check_mailbox.check_id = "mailbox"
_check_mailbox.check_group = GROUP
_check_mailbox.check_title = "Cloud mailbox"

checks.register(_check_mailbox, order=27)
