"""Slow work off the main thread: ``run(work, done)`` calls ``work()`` in a thread and ``done(result, error)``
back on the main thread (a timer looks every 100 ms). For network calls and big file copies, so the window
never freezes while a server is slow or a database is copied.

``work`` must not touch widgets, layers or the project; ``done`` may.
"""
import threading

from qgis.PyQt.QtCore import QTimer

_RUNNING = set()   # timers kept alive until their work is done


def run(work, done=None):
    box = {}

    def target():
        try:
            box["result"] = work()
        except Exception as e:  # noqa: BLE001 - handed to done()
            box["error"] = e

    th = threading.Thread(target=target, daemon=True)
    th.start()
    poll = QTimer()
    poll.setInterval(100)

    def check():
        if th.is_alive():
            return
        poll.stop()
        _RUNNING.discard(poll)
        if done is not None:
            done(box.get("result"), box.get("error"))

    poll.timeout.connect(check)
    poll.start()
    _RUNNING.add(poll)
    return th


def busy():
    """True while some work is still running (tests wait for it)."""
    return bool(_RUNNING)
