"""Backup and PC sync (no-loss P9): restic dumps of the office's folders to a disk of its own, checked and restore-tested.

9a: tools (pinned download), secret (one restic password per office), restic (the child process), engine, schedule,
health_rows, dialog.  9b is field_photos/backup_intake.py (a backup PC reads the office's Google Drive folder by itself).
TODO(9c): syncthing.py (peer sync, never counts as `backup`).  TODO(9d): offsite.py (rclone copy --immutable).
Nothing here imports Qt or QGIS at module level, so the pure parts run in plain Python.
"""
SETTINGS = "biruni/backup/"
