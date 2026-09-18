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


class TransferEngine:
    """Runs only on the session worker; a session serializes every backend call."""
    def __init__(self, backend, cancel: threading.Event, progress, conflict="overwrite"):
        self.backend, self.cancel, self.progress, self.conflict = backend, cancel, progress, conflict
        self.result = TransferResult()

    def upload(self, source: Path, target: str):
        check_cancel(self.cancel)
        source = Path(source)
        if source.is_symlink():
            self.result.skip_link(source)
            return
        if not source.exists():
            raise FileNotFoundError(source)
        if not self.backend.browsable:
            if not source.is_file():
                raise ValueError("TFTP 只支持指定路径的单文件传输")
            self.backend.upload(source, target, self.progress, self.cancel)
            self.result.files += 1
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
                return
        temporary = posixpath.join(posixpath.dirname(target), f".file-ez-{uuid.uuid4().hex}.part")
        try:
            self.backend.upload(source, temporary, self.progress, self.cancel)
            check_cancel(self.cancel)
            self.backend.rename(temporary, target, overwrite=bool(existing))
        finally:
            try:
                if self.backend.stat(temporary):
                    self.backend.remove(temporary)
            except Exception:
                pass
        self.result.files += 1

    def download(self, source: str, target: Path):
        check_cancel(self.cancel)
        target = Path(target)
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
                return
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".file-ez-", suffix=".part", dir=target.parent)
        os.close(fd)
        try:
            self.backend.download(source, temporary, self.progress, self.cancel)
            check_cancel(self.cancel)
            # Recheck after the transfer in case another application created it.
            if target.is_symlink():
                raise ValueError(f"目标变为符号链接：{target}")
            if self.conflict == "skip" and target.exists():
                self.result.skipped += 1
                return
            os.replace(temporary, target)
            self.result.files += 1
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
