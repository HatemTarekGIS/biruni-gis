"""Printed emergency runbook: one-page A4 Arabic PDF with zero secrets (no-loss P4)."""
import os
import socket
import time

from qgis.PyQt.QtCore import QMarginsF
from qgis.PyQt.QtGui import QColor, QImage, QPageSize, QPainter, QPdfWriter

from ..i18n import tr
from ..survey.report_pdf import A4_H, A4_W, ACCENT, GRID, INK, LIGHT, MARGIN, _Pen, _table
from . import owners

LRI = "\u2066"
PDI = "\u2069"
WARN_BG = QColor("#fff7e6")
WARN_INK = QColor("#9a5b00")


def _lri(text):
    s = str(text or "").strip()
    return f"{LRI}{s}{PDI}" if s else "—"


def _org_name(ctx, own_doc):
    org = str((own_doc or {}).get("org") or "").strip()
    if org:
        return org
    from_setting = str(ctx.setting("biruni/seal/office", "") or "").strip()
    if from_setting:
        return from_setting
    try:
        from ..seal.core import office as seal_office

        s = str(seal_office() or "").strip()
        if s:
            return s
    except Exception:  # noqa: BLE001
        pass
    return tr("Engineering Office")


def _format_owner(entry):
    if not isinstance(entry, dict):
        return "—"
    name = str(entry.get("owner") or "").strip()
    email = str(entry.get("email") or "").strip()
    if name and email:
        return f"{name} ({_lri(email)})"
    if name:
        return name
    if email:
        return _lri(email)
    return tr("Not recorded")


def _emergency_holders(cert_folder):
    if not cert_folder:
        return []
    try:
        from ..field_photos import recipients

        d = recipients.load(cert_folder)
        em = d.get("emergency") if isinstance(d.get("emergency"), dict) else {}
        return [str(h or "").strip() for h in (em.get("holders") or []) if str(h or "").strip()][:5]
    except Exception:  # noqa: BLE001
        return []


def runbook_model(ctx):
    """Build the pure-data model for the 1-page emergency runbook (contains zero secrets)."""
    own_doc = owners.load(ctx.cert_folder)
    org = _org_name(ctx, own_doc)
    made = time.strftime("%Y-%m-%d", time.localtime(ctx.now if getattr(ctx, "now", None) else time.time()))
    pc = os.environ.get("COMPUTERNAME") or socket.gethostname() or "Office-PC"
    kb = own_doc.get("key_backup") or {}
    backup_where = str(kb.get("where") or "").strip() or tr("Not recorded (record where the .biruni-key file is kept)")
    backup_holder = str(kb.get("holder") or "").strip() or tr("Not recorded")
    holders = _emergency_holders(ctx.cert_folder)
    srvs = own_doc.get("services") or {}
    drive_owner = _format_owner(srvs.get("drive"))
    cf_owner = _format_owner(srvs.get("cloudflare"))
    domain_owner = _format_owner(srvs.get("domain"))
    steps = [
        tr("1. Install Biruni GIS on the replacement office computer."),
        tr("2. Open Field Photos → Restore key… and choose the key backup file (.biruni-key), then enter its password. The replacement PC now has the same office identity and QR codes, so surveyors do not need to scan a new code."),
        tr("3. Connect the second copy disk (or sync the office Google Drive folder) and set the photos folder and second copy folder in Field Photos."),
        tr("4. If both the office computer and the key backup password are lost, gather any 3 of the 5 emergency key holders below with their .bek share files, then click Emergency key… → Open photos with 3 parts…."),
        tr("5. Open System Health in Biruni GIS to verify that all checks are green before resuming field work."),
    ]
    return {
        "org": org,
        "made": made,
        "pc": pc,
        "backup_where": backup_where,
        "backup_holder": backup_holder,
        "holders": holders,
        "k": 3,
        "of": 5,
        "drive_owner": drive_owner,
        "cf_owner": cf_owner,
        "domain_owner": domain_owner,
        "steps": steps,
    }


def draw_runbook(painter, model, dpmm):
    """Draw the one-page A4 emergency sheet using `survey.report_pdf._Pen`."""
    pen = _Pen(painter, dpmm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    x0, w = MARGIN, A4_W - 2 * MARGIN
    y = MARGIN

    # Header block
    pen.box(x0, y, w, 22, fill=LIGHT, color=ACCENT, width=0.45)
    pen.text(x0 + 4, y + 2, w - 8, 9, tr("Emergency Recovery Sheet — Biruni GIS"), 14.0, True, "center", ACCENT)
    pen.text(x0 + 4, y + 12, w - 8, 7,
             f"{model.get('org') or ''}  ·  {tr('Date')}: {_lri(model.get('made'))}  ·  {tr('Computer')}: {_lri(model.get('pc'))}",
             8.5, False, "center", INK)
    y += 26

    # Notice box (no secrets)
    pen.box(x0, y, w, 12, fill=WARN_BG, color=WARN_INK, width=0.3)
    pen.text(
        x0 + 3, y + 1, w - 6, 10,
        tr("Keep this printed sheet in the office safe. It contains no passwords, no private keys and no QR codes."),
        8.5, True, "right", WARN_INK,
    )
    y += 16

    # Section 1: Where the key backup is
    pen.text(x0, y, w, 6, tr("1. Office Keys Backup (.biruni-key)"), 10.5, True, "right", ACCENT)
    y += 7
    kb_rows = [
        (tr("Who holds the key backup"), model.get("backup_holder") or "—"),
        (tr("Where the key backup is kept"), model.get("backup_where") or "—"),
    ]
    y = _table(pen, x0, y, w, [tr("Item"), tr("Details")], kb_rows, [0.34, 0.66], row_h=7.0, size=8.5, rtl_cols=[True, True]) + 5

    # Section 2: 3-of-5 Emergency Key Holders
    k_val = int(model.get("k") or 3)
    of_val = int(model.get("of") or 5)
    pen.text(
        x0, y, w, 6,
        tr("2. Emergency Key Holders ({k} of {of} required to open sealed files)").format(k=k_val, of=of_val),
        10.5, True, "right", ACCENT,
    )
    y += 7
    holders = list(model.get("holders") or [])
    holder_rows = []
    for i in range(of_val):
        h_name = holders[i] if i < len(holders) and holders[i] else tr("Not assigned")
        holder_rows.append((f"{i + 1} / {of_val}", h_name, "biruni-emergency-%d-….bek" % (i + 1)))
    y = _table(
        pen, x0, y, w,
        [tr("Share"), tr("Holder name"), tr("Share file (.bek)")],
        holder_rows, [0.16, 0.48, 0.36], row_h=6.8, size=8.5, rtl_cols=[False, True, False],
    ) + 5

    # Section 3: Service Account Owners
    pen.text(x0, y, w, 6, tr("3. Service Account Owners"), 10.5, True, "right", ACCENT)
    y += 7
    srv_rows = [
        (tr("Google Drive"), model.get("drive_owner") or "—"),
        (tr("Cloudflare (Worker / Tunnel)"), model.get("cf_owner") or "—"),
        (tr("Domain"), model.get("domain_owner") or "—"),
    ]
    y = _table(
        pen, x0, y, w,
        [tr("Service"), tr("Owner & organisation email")],
        srv_rows, [0.34, 0.66], row_h=6.8, size=8.5, rtl_cols=[True, True],
    ) + 5

    # Section 4: Step-by-step recovery
    pen.text(x0, y, w, 6, tr("4. What to Do If the Office Computer Fails"), 10.5, True, "right", ACCENT)
    y += 7
    steps = list(model.get("steps") or [])
    box_h = max(48.0, min(86.0, A4_H - MARGIN - 10 - y))
    pen.box(x0, y, w, box_h, fill= QColor("#fafcfe"), color=GRID, width=0.3)
    sy = y + 2.5
    step_h = (box_h - 5.0) / max(len(steps), 1)
    for st in steps:
        pen.text(x0 + 3, sy, w - 6, step_h, st, 8.5, False, "right", INK)
        sy += step_h

    # Footer
    pen.line(x0, A4_H - MARGIN - 4, x0 + w, A4_H - MARGIN - 4)
    pen.text(x0, A4_H - MARGIN - 4, w / 2, 4, "1 / 1", 7.0, False, "left", GRID, rtl=False)
    pen.text(x0 + w / 2, A4_H - MARGIN - 4, w / 2, 4, tr("Prepared with Biruni GIS"), 7.0, False, "right", GRID)


def write_runbook(path, model):
    """Write the 1-page A4 emergency runbook PDF to `path`."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    writer = QPdfWriter(path)
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    writer.setPageMargins(QMarginsF(0, 0, 0, 0))
    writer.setResolution(300)
    writer.setTitle(tr("Emergency Recovery Sheet — Biruni GIS"))
    writer.setCreator("Biruni GIS")
    painter = QPainter(writer)
    try:
        dpmm = writer.width() / A4_W
        draw_runbook(painter, model, dpmm)
    finally:
        painter.end()
    return path


def render_runbook_png(path, model, dpi=110):
    """Render the runbook page to a PNG image (for visual checks)."""
    dpmm = dpi / 25.4
    img = QImage(int(A4_W * dpmm), int(A4_H * dpmm), QImage.Format.Format_RGB32)
    img.setDotsPerMeterX(int(dpmm * 1000))
    img.setDotsPerMeterY(int(dpmm * 1000))
    img.fill(QColor("white"))
    painter = QPainter(img)
    try:
        draw_runbook(painter, model, dpmm)
    finally:
        painter.end()
    img.save(path)
    return path
