from __future__ import annotations

import os
import posixpath
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .backends import Cancelled, check_cancel, local_child, safe_name


@dataclass
class TransferResult:
    files: int = 0
    skipped: int = 0
    skipped_links: list[str] = field(default_factory=list)

    def skip_link(self, path):
        self.skipped += 1
        self.skipped_links.append(str(path))

    @property
    def details(self):
        if not self.skipped_links:
            return str(self)
        return str(self) + "\n\n已跳过以下符号链接（未跟随链接目标）：\n" + "\n".join(self.skipped_links)

    def __str__(self):
        summary = f"完成 {self.files} 个文件" + (f"，跳过 {self.skipped} 项" if self.skipped else "")
        if self.skipped_links:
            summary += f"（其中符号链接 {len(self.skipped_links)} 项）"
        return summary


@dataclass(frozen=True)
class TransferProgress:
    stage: str
    path: str = ""
    file_size: int = 0
    file_done: int = 0
    files_done: int = 0
    files_total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0


class TransferEngine:
    """Runs only on the session worker; a session serializes every backend call."""
    def __init__(self, backend, cancel: threading.Event, progress, conflict="overwrite", detail=None):
        self.backend, self.cancel, self.progress, self.conflict = backend, cancel, progress, conflict
        self.detail = detail
        self.result = TransferResult()
        self._prepared = False
        self._files_total = self._files_done = 0
        self._bytes_total = self._bytes_done = 0
        self._planned_sizes = {}
        self._current_size = self._current_done = 0

    def _report(self, stage, path=""):
        if self.detail:
            self.detail(TransferProgress(stage, str(path), self._current_size, self._current_done,
                                         self._files_done, self._files_total,
                                         self._bytes_done, self._bytes_total))

    def _scan_upload(self, source):
        check_cancel(self.cancel)
        if source.is_symlink():
            return
        if source.is_dir():
            for child in sorted(source.iterdir()):
                self._scan_upload(child)
        elif source.is_file():
            self._count(source, source.stat().st_size)
        else:
            raise ValueError(f"不支持此文件类型：{source}")

    def _scan_download(self, source):
        check_cancel(self.cancel)
        if not self.backend.browsable:
            return
        entry = self.backend.stat(source)
        if entry is None:
            raise FileNotFoundError(source)
        self._scan_entry(entry)

    def _scan_entry(self, entry):
        check_cancel(self.cancel)
        if entry.is_link:
            return
        if entry.is_dir:
            for child in self.backend.listdir(entry.path):
                self._scan_entry(child)
        else:
            self._count(entry.path, entry.size)

    def _count(self, path, size):
        self._planned_sizes[str(path)] = max(0, size)
        self._files_total += 1
        self._bytes_total += max(0, size)
        self._report("scanning", path)

    def _prepare(self, source, direction):
        if self._prepared or not self.detail:
            return
        self._report("scanning", source)
        if direction == "upload":
            self._scan_upload(source)
        else:
            self._scan_download(source)
        self._prepared = True
        self._report("transferring")

    def _start_file(self, path, size):
        if not self.detail:
            return self.progress
        path = str(path)
        planned = self._planned_sizes.get(path)
        if planned is None:
            self._files_total += 1
            planned = 0
        size = max(0, size)
        self._bytes_total += size - planned
        self._current_size, self._current_done = size, 0
        self._report("transferring", path)

        def update(done, total):
            self.progress(done, total)
            if total != self._current_size:
                self._bytes_total += max(0, total) - self._current_size
                self._current_size = max(0, total)
            self._current_done = min(max(0, done), self._current_size)
            self._report("transferring", path)
        return update

    def _complete_file(self, path, skipped=False):
        if self.detail:
            self._bytes_done += self._current_size
            self._files_done += 1
            self._current_done = self._current_size
            self._report("skipped" if skipped else "transferring", path)

    def upload(self, source: Path, target: str):
        check_cancel(self.cancel)
        source = Path(source)
        self._prepare(source, "upload")
        if source.is_symlink():
            self.result.skip_link(source)
            return
        if not source.exists():
            raise FileNotFoundError(source)
        if not self.backend.browsable:
            if not source.is_file():
                raise ValueError("TFTP 只支持指定路径的单文件传输")
            progress = self._start_file(source, source.stat().st_size)
            self.backend.upload(source, target, progress, self.cancel)
            self.result.files += 1
            self._complete_file(source)
            return
        existing = self.backend.stat(target)
        if existing and existing.is_link:
            raise ValueError(f"目标是符号链接，已停止：{target}")
        if source.is_dir():
            if existing and not existing.is_dir:
                raise ValueError(f"目标存在同名文件：{target}")
            if not existing:
                self.backend.mkdir(target)
            for child in sorted(source.iterdir()):
                self.upload(child, self.backend.join(target, child.name))
            return
        if not source.is_file():
            raise ValueError(f"不支持此文件类型：{source}")
        if existing:
            if existing.is_dir:
                raise ValueError(f"目标存在同名文件夹：{target}")
            if self.conflict == "skip":
                self.result.skipped += 1
                self._start_file(source, source.stat().st_size)
                self._complete_file(source, skipped=True)
                return
        temporary = posixpath.join(posixpath.dirname(target), f".file-ez-{uuid.uuid4().hex}.part")
        progress = self._start_file(source, source.stat().st_size)
        try:
            self.backend.upload(source, temporary, progress, self.cancel)
            check_cancel(self.cancel)
            self.backend.rename(temporary, target, overwrite=bool(existing))
        finally:
            try:
                if self.backend.stat(temporary):
                    self.backend.remove(temporary)
            except Exception:
                pass
        self.result.files += 1
        self._complete_file(source)

    def download(self, source: str, target: Path):
        check_cancel(self.cancel)
        target = Path(target)
        self._prepare(source, "download")
        if target.is_symlink():
            raise ValueError(f"目标是符号链接：{target}")
        entry = self.backend.stat(source) if self.backend.browsable else None
        if self.backend.browsable and entry is None:
            raise FileNotFoundError(source)
        if entry and entry.is_link:
            self.result.skip_link(source)
            return
        if entry and entry.is_dir:
            target.mkdir(parents=True, exist_ok=True)
            for child in self.backend.listdir(source):
                self.download(child.path, local_child(target, child.name))
            return
        if target.exists():
            if target.is_dir():
                raise ValueError(f"目标存在同名文件夹：{target}")
            if self.conflict == "skip":
                self.result.skipped += 1
                self._start_file(source, entry.size if entry else 0)
                self._complete_file(source, skipped=True)
                return
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".file-ez-", suffix=".part", dir=target.parent)
        os.close(fd)
        progress = self._start_file(source, entry.size if entry else 0)
        try:
            self.backend.download(source, temporary, progress, self.cancel)
            check_cancel(self.cancel)
            # Recheck after the transfer in case another application created it.
            if target.is_symlink():
                raise ValueError(f"目标变为符号链接：{target}")
            if self.conflict == "skip" and target.exists():
                self.result.skipped += 1
                self._complete_file(source, skipped=True)
                return
            os.replace(temporary, target)
            self.result.files += 1
            self._complete_file(source)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def delete_remote(backend, entry, cancel):
    check_cancel(cancel)
    if entry.is_dir and not entry.is_link:
        for child in backend.listdir(entry.path):
            delete_remote(backend, child, cancel)
        backend.remove(entry.path, directory=True)
    else:
        backend.remove(entry.path)
