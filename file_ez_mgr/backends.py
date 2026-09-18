from __future__ import annotations

import base64
import errno
import ftplib
import hashlib
import os
import posixpath
import ssl
import stat
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import Profile

Progress = Callable[[int, int], None]


@dataclass
class Entry:
    name: str
    path: str
    is_dir: bool
    size: int = 0
    modified: float = 0
    is_link: bool = False

    @property
    def kind(self):
        return "符号链接" if self.is_link else ("文件夹" if self.is_dir else "文件")

    @property
    def extension(self):
        return "—" if self.is_dir else (posixpath.splitext(self.name)[1].lower() or "无扩展名")


def safe_name(name: str) -> str:
    # Remote names must be a single portable path component, on every OS.
    if (not name or name in {".", ".."} or any(c in name for c in '/\\\x00\r\n')
            or any(ord(c) < 32 for c in name)):
        raise ValueError(f"不安全的文件名：{name!r}")
    return name


def local_child(parent: Path, name: str) -> Path:
    safe_name(name)
    if os.name == "nt":
        stem = name.split(".")[0].upper()
        if (any(c in name for c in ':*?"<>|') or name.endswith((".", " "))
                or stem in {"CON", "PRN", "AUX", "NUL"}
                or stem in {f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)}):
            raise ValueError(f"Windows 不支持此文件名：{name}")
    child = parent / name
    if child.is_symlink():
        raise ValueError(f"目标不能是符号链接：{child}")
    return child


def remote_path(path: str) -> str:
    if any(c in path for c in '\x00\r\n'):
        raise ValueError("路径不能包含换行或空字符")
    return path


def local_entries(directory: str) -> list[Entry]:
    entries = []
    with os.scandir(directory) as iterator:
        for item in iterator:
            try:
                info = item.stat(follow_symlinks=False)
                entries.append(Entry(item.name, item.path, item.is_dir(), info.st_size,
                                     info.st_mtime, item.is_symlink()))
            except FileNotFoundError:
                continue
    return entries


class Cancelled(Exception):
    pass


def check_cancel(cancel: threading.Event):
    if cancel.is_set():
        raise Cancelled("任务已取消")


class HostKeyRequired(Exception):
    def __init__(self, hostname, key):
        self.hostname, self.key = hostname, key
        digest = hashlib.sha256(key.asbytes()).digest()
        self.fingerprint = "SHA256:" + base64.b64encode(digest).decode().rstrip("=")
        super().__init__(f"首次连接 {hostname}\n{key.get_name()}\n{self.fingerprint}")


class Backend:
    browsable = True

    def join(self, parent, name):
        return posixpath.join(parent, safe_name(name))

    def exists(self, path):
        return self.stat(path) is not None

    def close(self):
        pass


class SFTPBackend(Backend):
    def __init__(self, profile: Profile, password: str, known_hosts: Path):
        self.profile, self.password, self.known_hosts = profile, password, known_hosts
        self.client = self.sftp = None

    def connect(self):
        import paramiko

        class AskPolicy(paramiko.MissingHostKeyPolicy):
            def missing_host_key(self, client, hostname, key):
                raise HostKeyRequired(hostname, key)

        client = self.client = paramiko.SSHClient()
        client.load_system_host_keys()
        if self.known_hosts.exists():
            client.load_host_keys(str(self.known_hosts))
        client.set_missing_host_key_policy(AskPolicy())
        p = self.profile
        try:
            client.connect(p.host, port=p.port, username=p.username or None,
                           password=self.password or None, passphrase=self.password or None,
                           key_filename=os.path.expanduser(p.key_file) if p.key_file else None,
                           timeout=12, auth_timeout=15, banner_timeout=15,
                           look_for_keys=not bool(self.password), allow_agent=True)
            self.sftp = client.open_sftp()
            self.sftp.get_channel().settimeout(15)
            client.get_transport().set_keepalive(30)
            return self.normalize(p.remote_dir or ".")
        except Exception:
            client.close()
            raise

    def normalize(self, path):
        return self.sftp.normalize(remote_path(path))

    def _entry(self, path, info):
        return Entry(posixpath.basename(path), path, stat.S_ISDIR(info.st_mode),
                     info.st_size or 0, info.st_mtime or 0, stat.S_ISLNK(info.st_mode))

    def listdir(self, path):
        return [self._entry(self.join(path, x.filename), x) for x in self.sftp.listdir_attr(path)]

    def stat(self, path):
        try:
            return self._entry(path, self.sftp.lstat(remote_path(path)))
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return None
            raise

    def mkdir(self, path):
        self.sftp.mkdir(remote_path(path))

    def remove(self, path, directory=False):
        (self.sftp.rmdir if directory else self.sftp.remove)(remote_path(path))

    def upload(self, local, remote, progress: Progress, cancel):
        total = os.path.getsize(local)
        done = 0
        with open(local, "rb") as source, self.sftp.open(remote_path(remote), "wb") as target:
            while block := source.read(128 * 1024):
                check_cancel(cancel)
                target.write(block)
                done += len(block)
                progress(done, total)
        check_cancel(cancel)
        progress(total, total)

    def download(self, remote, local, progress: Progress, cancel):
        total = self.sftp.stat(remote_path(remote)).st_size
        done = 0
        with self.sftp.open(remote, "rb") as source, open(local, "wb") as target:
            while block := source.read(128 * 1024):
                check_cancel(cancel)
                target.write(block)
                done += len(block)
                progress(done, total)
        check_cancel(cancel)
        progress(total, total)

    def rename(self, source, target, overwrite=False):
        if overwrite:
            try:
                self.sftp.posix_rename(source, target)
            except OSError as exc:
                raise RuntimeError("服务端无法安全替换文件，原文件已保留。请手动处理目标文件。") from exc
        else:
            self.sftp.rename(source, target)

    def close(self):
        if self.client:
            self.client.close()


class FTPBackend(Backend):
    def __init__(self, profile: Profile, password: str):
        self.profile, self.password = profile, password
        self.ftp = None

    def connect(self):
        p = self.profile
        ftp = (ftplib.FTP_TLS(context=ssl.create_default_context(), timeout=15)
               if p.protocol == "ftps" else ftplib.FTP(timeout=15))
        self.ftp = ftp
        try:
            ftp.connect(p.host, p.port)
            ftp.login(p.username or "anonymous", self.password)
            if p.protocol == "ftps":
                ftp.prot_p()
            ftp.set_pasv(p.passive)
            return self.normalize(p.remote_dir or ".")
        except Exception:
            ftp.close()
            raise

    def normalize(self, path):
        self.ftp.cwd(remote_path(path))
        return self.ftp.pwd()

    def listdir(self, path):
        remote_path(path)
        result = []
        try:
            rows = list(self.ftp.mlsd(path, facts=["type", "size", "modify"]))
        except ftplib.error_perm as exc:
            if not str(exc).startswith(("500", "501", "502", "504")):
                raise
            raise RuntimeError("FTP 服务端不支持 MLSD 目录列表，请启用 MLSD 或改用 SFTP。") from exc
        for name, facts in rows:
            if facts.get("type") in {"cdir", "pdir"} or name in {".", ".."}:
                continue
            full = self.join(path, name)
            modified = 0
            try:
                modified = datetime.strptime(facts.get("modify", "").split(".")[0],
                                             "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                pass
            kind = facts.get("type", "file").lower()
            # Unrecognized server types must never be traversed recursively.
            result.append(Entry(name, full, kind == "dir", int(facts.get("size", 0)),
                                modified, kind not in {"dir", "file"}))
        return result

    def stat(self, path):
        path = posixpath.normpath(remote_path(path))
        if path == "/":
            return Entry("/", "/", True)
        return next((e for e in self.listdir(posixpath.dirname(path) or ".")
                     if e.name == posixpath.basename(path)), None)

    def mkdir(self, path):
        self.ftp.mkd(remote_path(path))

    def remove(self, path, directory=False):
        (self.ftp.rmd if directory else self.ftp.delete)(remote_path(path))

    def _transfer(self, operation, cancel):
        try:
            operation()
            check_cancel(cancel)
        except Exception:
            # An aborted data transfer leaves FTP replies out of sync. Reconnect
            # before the next queued operation; never reuse that control stream.
            self.close()
            try:
                self.connect()
            except Exception:
                pass
            raise

    def upload(self, local, remote, progress: Progress, cancel):
        total, done = os.path.getsize(local), 0
        def callback(block):
            nonlocal done
            check_cancel(cancel)
            done += len(block)
            progress(done, total)
        with open(local, "rb") as stream:
            self._transfer(lambda: self.ftp.storbinary("STOR " + remote_path(remote), stream,
                                                       128 * 1024, callback), cancel)
        progress(total, total)

    def download(self, remote, local, progress: Progress, cancel):
        entry = self.stat(remote)
        total, done = (entry.size if entry else 0), 0
        with open(local, "wb") as stream:
            def callback(block):
                nonlocal done
                check_cancel(cancel)
                stream.write(block)
                done += len(block)
                progress(done, total)
            self._transfer(lambda: self.ftp.retrbinary("RETR " + remote_path(remote), callback,
                                                       128 * 1024), cancel)
        progress(done, done)

    def rename(self, source, target, overwrite=False):
        self.ftp.rename(remote_path(source), remote_path(target))

    def close(self):
        if self.ftp:
            self.ftp.close()


class TFTPBackend(Backend):
    browsable = False

    def __init__(self, profile: Profile):
        self.profile = profile

    def connect(self):
        # TFTP has no session handshake. Reachability is checked on transfer.
        return self.profile.remote_dir or "."

    def upload(self, local, remote, progress: Progress, cancel):
        import tftpy
        check_cancel(cancel)
        def hook(packet):
            check_cancel(cancel)
        tftpy.TftpClient(self.profile.host, self.profile.port).upload(
            remote_path(remote), str(local), packethook=hook, timeout=3, retries=3)
        check_cancel(cancel)
        size = os.path.getsize(local)
        progress(size, size)

    def download(self, remote, local, progress: Progress, cancel):
        import tftpy
        check_cancel(cancel)
        def hook(packet):
            check_cancel(cancel)
        tftpy.TftpClient(self.profile.host, self.profile.port).download(
            remote_path(remote), str(local), packethook=hook, timeout=3, retries=3)
        check_cancel(cancel)
        size = os.path.getsize(local)
        progress(size, size)


def create_backend(profile, password, known_hosts):
    if profile.protocol == "sftp":
        return SFTPBackend(profile, password, known_hosts)
    if profile.protocol in {"ftp", "ftps"}:
        return FTPBackend(profile, password)
    if profile.protocol == "tftp":
        return TFTPBackend(profile)
    raise ValueError("不支持的协议")
