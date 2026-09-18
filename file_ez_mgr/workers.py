from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from PyQt5.QtCore import QObject, pyqtSignal

from .backends import Cancelled, check_cancel


@dataclass
class Job:
    title: str
    function: object
    callback: object = None
    failure: object = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    cancel: threading.Event = field(default_factory=threading.Event)
    visible: bool = True


class WorkerQueue(QObject):
    started = pyqtSignal(str)
    progressed = pyqtSignal(str, int, int)
    finished = pyqtSignal(str, object, object)
    idle = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="file-ez")
        self.jobs: dict[str, Job] = {}
        self.closed = False
        self.finished.connect(self._finish)

    def submit(self, title, function, callback=None, failure=None, visible=True):
        if self.closed:
            raise RuntimeError("会话已关闭")
        job = Job(title, function, callback, failure, visible=visible)
        self.jobs[job.id] = job
        self.executor.submit(self._run, job)
        return job

    def _run(self, job):
        self.started.emit(job.id)
        last = 0
        def progress(done, total):
            nonlocal last
            now = time.monotonic()
            if now - last >= 0.1 or done == total:
                # Python object integers avoid Qt's signed 32-bit byte limit.
                self.progressed.emit(job.id, min(10000, int(done / total * 10000)) if total else 0, 10000)
                last = now
        try:
            check_cancel(job.cancel)
            value = job.function(job.cancel, progress)
            self.finished.emit(job.id, value, None)
        except Exception as exc:
            self.finished.emit(job.id, None, exc)

    def _finish(self, job_id, value, error):
        job = self.jobs.pop(job_id, None)
        if job:
            if error is None and job.callback:
                job.callback(value)
            elif error is not None and job.failure:
                job.failure(error)
        if not self.jobs:
            self.idle.emit()

    def cancel_all(self):
        for job in list(self.jobs.values()):
            job.cancel.set()

    def shutdown(self, cleanup=None):
        if self.closed:
            return
        self.closed = True
        self.cancel_all()
        if cleanup:
            self.executor.submit(cleanup)
        self.executor.shutdown(wait=False)
