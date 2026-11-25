"""Dialog to deploy or clear an organization's own Cloudflare Worker relay in one click."""
import os
import queue
import threading

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                                 QPushButton, QVBoxLayout)

from ..health import owners
from ..i18n import tr
from . import bridge, mailbox, org_relay


class OrgRelayDialog(QDialog):
    """Guides the user through creating a scoped Cloudflare token and deploys biruni-relay."""

    def __init__(self, parent=None, on_changed=None, cert_folder=None):
        super().__init__(parent)
        self.setWindowTitle(tr("My organization's relay"))
        self.resize(520, 440)
        self._on_changed = on_changed
        self._cert_folder = cert_folder or os.path.join(QgsApplication.qgisSettingsDirPath(), "biruni")
        self._q = queue.Queue()
        self._busy = False
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._poll)

        v = QVBoxLayout(self)
        warn = QLabel("<b>" + tr("Use the organisation's email for this account, not a personal one.") + "</b>")
        warn.setWordWrap(True)
        v.addWidget(warn)
        steps = [
            tr("Sign in at <a href='https://dash.cloudflare.com'>dash.cloudflare.com</a> (a free account is enough)."),
            tr("My Profile → API Tokens → Create Token → Create Custom Token."),
            tr("Permissions: Account · Workers Scripts · Edit, and Account · Account Settings · Read."),
            tr("For the mailbox, add the permission: Account · Workers R2 Storage · Edit."),
            tr("Account Resources: your account."),
            tr("Create it and paste it here."),
            tr("Delete the token afterwards; Biruni does not keep it."),
        ]
        guide = QLabel("<ol>" + "".join(f"<li style='margin-bottom:4px'>{s}</li>" for s in steps) + "</ol>")
        guide.setTextFormat(Qt.TextFormat.RichText)
        guide.setWordWrap(True)
        guide.setOpenExternalLinks(True)
        guide.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        v.addWidget(guide)

        self.mb_check = QCheckBox(tr("Also create the cloud mailbox (Cloudflare R2)"))
        self.mb_check.setChecked(mailbox.enabled())
        v.addWidget(self.mb_check)
        drow = QHBoxLayout()
        drow.addWidget(QLabel(tr("Keep files in the mailbox for:")))
        self.mb_days = QComboBox()
        for n in org_relay.MB_DAYS:
            self.mb_days.addItem(tr("{n} days").format(n=n), n)
        cur_days = mailbox.load_days()
        self.mb_days.setCurrentIndex(max(0, self.mb_days.findData(cur_days)))
        self.mb_days.setEnabled(self.mb_check.isChecked())
        self.mb_check.toggled.connect(self.mb_days.setEnabled)
        drow.addWidget(self.mb_days)
        drow.addStretch(1)
        v.addLayout(drow)

        self.addr_lbl = QLabel()
        self.addr_lbl.setWordWrap(True)
        self.addr_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.addr_lbl)

        cur_cf = (owners.load(self._cert_folder).get("services") or {}).get("cloudflare") or {}
        orow = QHBoxLayout()
        self.owner_name_edit = QLineEdit(cur_cf.get("owner") or "")
        self.owner_name_edit.setPlaceholderText(tr("Account owner's name"))
        self.owner_email_edit = QLineEdit(cur_cf.get("email") or "")
        self.owner_email_edit.setPlaceholderText(tr("Organisation email (e.g. it@company.com)"))
        orow.addWidget(self.owner_name_edit, 1)
        orow.addWidget(self.owner_email_edit, 1)
        v.addLayout(orow)

        self.token_edit = QLineEdit()
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setPlaceholderText(tr("Cloudflare API token"))
        v.addWidget(self.token_edit)

        self.status_lbl = QLabel()
        self.status_lbl.setObjectName("GpsHint")
        self.status_lbl.setWordWrap(True)
        v.addWidget(self.status_lbl)

        row = QHBoxLayout()
        self.deploy_btn = QPushButton(tr("Deploy"))
        self.deploy_btn.setObjectName("GpsPrimary")
        self.deploy_btn.clicked.connect(self.start_deploy)
        self.stop_btn = QPushButton(tr("Stop using it"))
        self.stop_btn.clicked.connect(self.stop_using)
        self.close_btn = QPushButton(tr("Close"))
        self.close_btn.clicked.connect(self.close)
        row.addWidget(self.deploy_btn)
        row.addWidget(self.stop_btn)
        row.addStretch(1)
        row.addWidget(self.close_btn)
        v.addLayout(row)

        self._refresh_addr()

    def _refresh_addr(self):
        host = bridge.org_host()
        if host:
            self.addr_lbl.setText(tr("Active relay: {host}").format(host=host))
            self.stop_btn.setEnabled(not self._busy)
        else:
            self.addr_lbl.setText(tr("No organization relay configured."))
            self.stop_btn.setEnabled(False)

    def _set_busy(self, busy):
        self._busy = busy
        self.deploy_btn.setEnabled(not busy)
        self.token_edit.setEnabled(not busy)
        self.mb_check.setEnabled(not busy)
        self.mb_days.setEnabled((not busy) and self.mb_check.isChecked())
        self.stop_btn.setEnabled((not busy) and bool(bridge.org_host()))

    def _mailbox_arg(self):
        if not self.mb_check.isChecked():
            return None
        days = int(self.mb_days.currentData() or 7)
        return {"days": days, "secret": mailbox.load_secret() or None}

    def start_deploy(self):
        """Clear the token input immediately and start deployment on a background thread."""
        if self._busy:
            return
        tok = self.token_edit.text().strip()
        self.token_edit.clear()
        if not tok:
            self.status_lbl.setText(tr("Paste your Cloudflare API token first."))
            return
        self._save_owner()
        self._run_worker(tok, account_id=None, mb=self._mailbox_arg())

    def _save_owner(self):
        name = self.owner_name_edit.text().strip()
        email = self.owner_email_edit.text().strip()
        if name or email:
            owners.set_owner(self._cert_folder, "cloudflare", name, email)

    def _run_worker(self, token, account_id=None, mb=None):
        self._set_busy(True)
        self.status_lbl.setText(tr("Deploying relay…"))
        q = self._q
        if not self._timer.isActive():
            self._timer.start()

        def work(secret=token, acc_id=account_id, mb_cfg=mb):
            try:
                res = org_relay.deploy(
                    secret, account_id=acc_id, progress=lambda m: q.put(("progress", m)), mailbox=mb_cfg
                )
                q.put(("done", res))
            except org_relay.ChooseAccount as ca:
                q.put(("choose", ca.accounts, lambda picked: self._run_worker(secret, account_id=picked, mb=mb_cfg)))
            except org_relay.OrgRelayError as e:
                q.put(("error", str(e)))
            except Exception:  # noqa: BLE001
                q.put(("error", tr("Deployment failed.")))

        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        while not self._q.empty():
            item = self._q.get_nowait()
            kind = item[0]
            if kind == "progress":
                self.status_lbl.setText(tr(item[1]))
            elif kind == "done":
                res = item[1]
                self._timer.stop()
                self._set_busy(False)
                bridge.set_org_host(res["host"])
                self._save_owner()
                if res.get("mb"):
                    mailbox.save_config(res["mb_secret"], res.get("mb_days") or 7)
                else:
                    mailbox.set_enabled(False)
                self._refresh_addr()
                msg = tr("Relay deployed and active: {host}").format(host=res["host"])
                mb_err = res.get("mb_error") or ""
                if mb_err == "r2_off":
                    msg += "\n" + tr(
                        "The relay was deployed, but the cloud mailbox was not created: R2 is not enabled on this "
                        "Cloudflare account. Open R2 once in the Cloudflare dashboard and enable it (Cloudflare may "
                        "ask for a payment method even on the free plan), then press Deploy again. Google Drive "
                        "remains the first mailbox."
                    )
                elif mb_err == "r2_perm":
                    msg += "\n" + tr(
                        "The relay was deployed, but the token lacks the permission Workers R2 Storage · Edit, so "
                        "the cloud mailbox was not created. Create a new token with this permission and deploy again."
                    )
                elif mb_err in ("r2_fail", "r2_bind"):
                    msg += "\n" + tr(
                        "The cloud mailbox could not be set up ({e}). The relay works, and Google Drive remains the "
                        "first mailbox."
                    ).format(e=mb_err)
                self.status_lbl.setText(msg)
                if self._on_changed is not None:
                    self._on_changed()
            elif kind == "choose":
                accounts, retry_fn = item[1], item[2]
                self._timer.stop()
                self._set_busy(False)
                labels = [f"{a['name']} ({a['id']})" if a.get("name") and a["name"] != a["id"] else a["id"]
                          for a in accounts]
                choice, ok = QInputDialog.getItem(
                    self, tr("Choose Cloudflare account"), tr("Account:"), labels, 0, False
                )
                if ok and choice in labels:
                    picked_id = accounts[labels.index(choice)]["id"]
                    retry_fn(picked_id)
                else:
                    self.status_lbl.setText(tr("Deployment cancelled."))
            elif kind == "error":
                self._timer.stop()
                self._set_busy(False)
                self.status_lbl.setText(item[1])

    def stop_using(self):
        """Clear the organization relay setting and refresh any active QR code."""
        if self._busy:
            return
        bridge.set_org_host("")
        mailbox.set_enabled(False)
        self._refresh_addr()
        self.status_lbl.setText(
            tr("Stopped using your organization's relay. You can delete the Worker at dash.cloudflare.com.")
        )
        if self._on_changed is not None:
            self._on_changed()

    def closeEvent(self, event):  # noqa: N802
        self.token_edit.clear()
        self._timer.stop()
        super().closeEvent(event)

    def reject(self):
        self.token_edit.clear()
        self._timer.stop()
        super().reject()
