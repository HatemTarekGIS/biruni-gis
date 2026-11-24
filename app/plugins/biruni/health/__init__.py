"""System health, account owners, and printed emergency runbook (no-loss P4)."""
from .checks import Ctx, register, run, worst
from .owners import (departures, is_personal, load as load_owners, mark_left,
                     save as save_owners, set_key_backup, set_owner)
from .runbook import runbook_model, write_runbook


try:   # P9: the backup's rows (they register themselves)
    from ..backup import health_rows as _backup_rows  # noqa: F401
except Exception:  # noqa: BLE001 - a broken backup module must not take the health screen down
    pass


def open_dialog(plugin):
    from .dialog import open_health_dialog

    return open_health_dialog(plugin)


__all__ = [
    "Ctx",
    "departures",
    "is_personal",
    "load_owners",
    "mark_left",
    "open_dialog",
    "register",
    "run",
    "runbook_model",
    "save_owners",
    "set_key_backup",
    "set_owner",
    "worst",
    "write_runbook",
]
