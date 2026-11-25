"""The backup's rows on the System Health screen. They read state.json and a few settings only: no process is started,
so the screen opens at once. Registered with health.checks when this module is imported (health/__init__.py)."""
import os

from ..health import checks
from ..i18n import tr
from . import engine, secret

GROUP = "Backup"
DAY = 86400.0
PACK_FILE = "office-pack.bin"


def _row(rid, title, level, text, fix=None):
    return {"id": rid, "group": tr(GROUP), "title": tr(title), "level": level, "text": text, "fix": fix}


def _days(now, at):
    return max(0, int((now - float(at)) / DAY))


def _stale(now, at, warn_d, bad_d):
    """ok / warn / bad by age in days."""
    d = (now - float(at)) / DAY
    return "bad" if d >= bad_d else "warn" if d >= warn_d else "ok"


def _check_backup(ctx):
    return _backup_rows(ctx) + _side_rows(ctx)


def _backup_rows(ctx):
    get = lambda k, d=None: ctx.setting(k, d)   # noqa: E731
    cfg = engine.config(get)
    st = engine.load_state(ctx.cert_folder)
    now = ctx.now
    mirror = str(ctx.setting(engine.MIRROR_SETTING, "") or "").strip()
    rows = []

    if not cfg["enabled"] or not cfg["repo"]:
        if mirror:
            rows.append(_row("backup_last", "Backup", "warn", tr(
                "No backup is set up. Photos have a second copy folder only; a disk of its own for a real backup is "
                "needed."), "backup"))
        else:
            rows.append(_row("backup_last", "Backup", "bad", tr(
                "No backup is set up and no second copy folder is set. Choose a backup disk now."), "backup"))
        if not cfg["enabled"] and not cfg["repo"]:
            return rows

    # ---- backup_last --------------------------------------------------------------------------------------------------
    lb = st.get("last_backup") or {}
    err = st.get("last_error_code")
    if cfg["repo"]:
        if not lb.get("at"):
            rows.append(_row("backup_last", "Backup", "bad" if err is not None else "warn",
                             tr("The backup has not run yet (last error code: {c}).").format(c=err) if err is not None
                             else tr("The backup has not run yet. It runs while Biruni is open."), "backup"))
        else:
            age = _days(now, lb["at"])
            names = [os.path.basename(str(p)) or str(p) for p in (lb.get("unreadable") or [])]
            lvl = _stale(now, lb["at"], 2, 7)
            text = tr("Last backup {n} day(s) ago.").format(n=age)
            if err is not None:
                lvl, text = "bad", tr("The last attempt failed (code {c}); the last good backup was {n} day(s) ago.").format(
                    c=err, n=age)
            elif int(lb.get("code") or 0) == 3:
                lvl = "bad" if lvl == "bad" else "warn"
                text = tr("The last backup skipped {n} unreadable file(s): {names}.").format(
                    n=len(names), names="، ".join(names[:5]))
            rows.append(_row("backup_last", "Backup", lvl, text, "backup"))

        # ---- backup_check ---------------------------------------------------------------------------------------------
        lc = st.get("last_check") or {}
        ref = lc.get("at") or st.get("first_backup")
        if lc.get("at") and not lc.get("ok"):
            rows.append(_row("backup_check", "Backup check", "bad", tr("The last check of the backup failed."), "backup"))
        elif ref:
            lvl = _stale(now, ref, 10, 21)
            if not lc.get("at") and lvl == "ok":
                rows.append(_row("backup_check", "Backup check", "na", tr("The backup has not been checked yet."), "backup"))
            else:
                rows.append(_row("backup_check", "Backup check", lvl,
                                 tr("The backup was checked {n} day(s) ago.").format(n=_days(now, ref)), "backup"))

        # ---- backup_restore -------------------------------------------------------------------------------------------
        lr = st.get("last_restore") or {}
        ref = lr.get("at") or st.get("first_backup")
        if lr.get("at") and not lr.get("ok"):
            rows.append(_row("backup_restore", "Restore test", "bad", tr(
                "A test restore did not match the originals ({n} file(s)). Check the backup disk."
            ).format(n=len(lr.get("mismatches") or [])), "backup"))
        elif ref:
            lvl = _stale(now, ref, 40, 70)
            if not lr.get("at") and lvl == "ok":
                rows.append(_row("backup_restore", "Restore test", "na", tr("No test restore has run yet."), "backup"))
            else:
                rows.append(_row("backup_restore", "Restore test", lvl,
                                 tr("A restore was tested {n} day(s) ago.").format(n=_days(now, ref)), "backup"))

    # ---- backup_target --------------------------------------------------------------------------------------------------
    if not cfg["repo"]:
        rows.append(_row("backup_target", "Backup disk", "warn", tr("No backup disk is chosen."), "backup"))
    else:
        from ..field_photos.copies import different_volume

        if ctx.photos_folder and not different_volume(ctx.photos_folder, cfg["repo"]):
            rows.append(_row("backup_target", "Backup disk", "bad", tr(
                "The backup is on the same disk as the photos. Put it on another disk."), "backup"))
        elif mirror and not different_volume(mirror, cfg["repo"]):
            rows.append(_row("backup_target", "Backup disk", "warn", tr(
                "The backup is on the same disk as the second copy folder. Put it on another disk."), "backup"))
        elif not engine.target_present(cfg["repo"]):
            rows.append(_row("backup_target", "Backup disk", "warn", tr(
                "The backup disk is not connected. The backup waits for it."), "backup"))
        else:
            rows.append(_row("backup_target", "Backup disk", "ok", tr("The backup is on its own disk."), None))

    # ---- backup_password ------------------------------------------------------------------------------------------------
    if st.get("repo_init"):
        if st.get("password_bad") or not secret.load_password(ctx.cert_folder):
            rows.append(_row("backup_password", "Backup password", "bad", tr(
                "The backup exists but its password cannot be read on this computer. Restore the key backup."), "key_backup"))
        else:
            rows.append(_row("backup_password", "Backup password", "ok", tr("The backup password is kept safely."), None))

    return rows


def _side_rows(ctx):
    """Rows that do not depend on a backup disk: a backup PC's intake, and the keys snapshot."""
    get = lambda k, d=None: ctx.setting(k, d)   # noqa: E731
    cfg = engine.config(get)
    st = engine.load_state(ctx.cert_folder)
    now = ctx.now
    rows = []
    # ---- backup_intake (a backup PC reading the office's Drive folder) ---------------------------------------------------
    if cfg["auto-intake"] and ctx.cert_folder and os.path.isfile(os.path.join(ctx.cert_folder, PACK_FILE)):
        it = _intake_state(ctx.cert_folder)
        if not it.get("at") or now - float(it["at"]) > 3600:
            rows.append(_row("backup_intake", "Automatic intake", "warn", tr(
                "The automatic intake has not scanned for over an hour."), "backup"))
        elif int(it.get("refused") or 0) > 0:
            rows.append(_row("backup_intake", "Automatic intake", "warn", tr(
                "The automatic intake refused {n} file(s).").format(n=int(it["refused"])), "backup"))
        else:
            rows.append(_row("backup_intake", "Automatic intake", "ok", tr("The automatic intake is running."), None))

    # ---- vault_auto -------------------------------------------------------------------------------------------------------
    lv = st.get("last_vault") or {}
    if lv.get("at"):
        n = int(lv.get("unplaced") or 0)
        rows.append(_row("vault_auto", "Keys in the vault", "warn" if n else "ok", tr(
            "{n} shard(s) of the keys snapshot are not with a holder yet.").format(n=n) if n
            else tr("The keys snapshot is with its holders."), "backup" if n else None))

    # ---- sync:<peer> (9c: amber below 100% for 24 h; red when unseen for 7 days) ------------------------------------------
    peers = st.get("sync_peers") or {}
    if isinstance(peers, dict):
        for pid in sorted(peers):
            p = peers[pid]
            if not isinstance(p, dict):
                continue
            name = str(p.get("name") or pid[:7])
            pct = int(round(float(p.get("completion") or 0.0)))
            seen = p.get("seen_at")
            below = p.get("below_100_since")
            if isinstance(seen, (int, float)) and (now - float(seen)) >= 7 * DAY:
                rows.append(_row(f"sync:{pid}", "PC sync", "bad", tr(
                    "Computer {name} has not been seen for {n} day(s).").format(name=name, n=_days(now, seen)), "backup"))
            elif pct < 100 and isinstance(below, (int, float)) and (now - float(below)) >= DAY:
                rows.append(_row(f"sync:{pid}", "PC sync", "warn", tr(
                    "Sync with {name} has stayed below 100% ({pct}%) for over 24 hours.").format(name=name, pct=pct),
                    "backup"))
            elif pct < 100:
                rows.append(_row(f"sync:{pid}", "PC sync", "ok", tr(
                    "Syncing with {name} ({pct}%).").format(name=name, pct=pct), None))
            else:
                rows.append(_row(f"sync:{pid}", "PC sync", "ok", tr(
                    "In sync with {name} (100%).").format(name=name), None))

    # ---- offsite (9d: amber after 7 days; red after 30 days or on a failure) ----------------------------------------------
    lo = st.get("last_offsite") or {}
    if cfg.get("offsite-remote") or lo:
        if lo.get("at") and lo.get("ok") is False:
            rows.append(_row("offsite", "Off-site copy", "bad", tr(
                "The last off-site copy failed. Check the remote connection."), "backup"))
        elif lo.get("at"):
            ref = lo.get("last_ok_at") or lo["at"]
            lvl = _stale(now, ref, 7, 30)
            rows.append(_row("offsite", "Off-site copy", lvl, tr(
                "Last off-site copy {n} day(s) ago.").format(n=_days(now, ref)), "backup" if lvl != "ok" else None))
        else:
            rows.append(_row("offsite", "Off-site copy", "warn", tr(
                "No off-site copy has run yet."), "backup"))

    return rows


def _intake_state(cert_folder):
    import json

    try:
        with open(os.path.join(engine.backup_dir(cert_folder), "intake.json"), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


_check_backup.check_id = "backup"
_check_backup.check_group = "Backup"
_check_backup.check_title = "Backup"

checks.register(_check_backup, order=25)
