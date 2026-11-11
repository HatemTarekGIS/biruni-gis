"""The Vault's background thread: folder scans, file facts, pictures and duplicate searches run here, one job at a
time, so the pane never stops the interface. A job is any plain function; its result comes back on the main thread
through the ``done`` signal. Jobs of an older generation (before a refresh) are dropped unread."""
import itertools
import queue
import threading

from qgis.PyQt.QtCore import QThread, pyqtSignal

URGENT, NORMAL, LOW = 0, 5, 9


class Worker(QThread):
    done = pyqtSignal(str, object, int)      # job id, result (or the exception), generation

    def __init__(self, parent=None):
        super().__init__(parent)
        self._q = queue.PriorityQueue()
        self._n = itertools.count()
        self._lock = threading.Lock()
        self._outstanding = 0                 # asked for and not answered yet (waiting or running)
        self.generation = 0
        self.ran = 0
        self.done.connect(self._answered)     # first slot: runs on the main thread before the pane's

    def _count(self, k):
        with self._lock:
            self._outstanding = max(0, self._outstanding + k)

    def _answered(self, *_):
        self._count(-1)

    def submit(self, job_id, fn, *args, priority=NORMAL):
        self._count(1)
        self._q.put((priority, next(self._n), job_id, fn, args, self.generation))
        if not self.isRunning():
            self.start(QThread.Priority.LowPriority)

    def new_generation(self):
        """Forget the waiting jobs (a refresh asks again)."""
        self.generation += 1
        try:
            while True:
                item = self._q.get_nowait()
                if item[3] is not None:
                    self._count(-1)
        except queue.Empty:
            pass

    def pending(self):
        """Jobs waiting, running, or whose answer has not reached the main thread yet."""
        with self._lock:
            return self._outstanding

    def run(self):
        while True:
            _p, _n, job_id, fn, args, gen = self._q.get()  # waits without using the processor
            if fn is None:
                return
            if gen != self.generation:
                self._count(-1)
                continue
            try:
                result = fn(*args)
            except Exception as e:  # noqa: BLE001 - the error is the job's answer
                result = e
            self.ran += 1
            self.done.emit(job_id, result, gen)

    def stop(self):
        self.new_generation()
        if self.isRunning():
            self._q.put((-1, -1, "", None, (), self.generation))
            self.wait(4000)
