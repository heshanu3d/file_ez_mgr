"""Real Paramiko SFTP server; no external credentials or network required."""
import os
import socket
import threading
from pathlib import Path

import paramiko
import pytest

from file_ez_mgr.backends import HostKeyRequired, SFTPBackend
from file_ez_mgr.config import Profile
from file_ez_mgr.transfers import TransferEngine, delete_remote


@pytest.fixture
def sftp_server(tmp_path):
    root = tmp_path / "server"
    root.mkdir()
    key = paramiko.RSAKey.generate(2048)

    class Auth(paramiko.ServerInterface):
        def check_auth_password(self, username, password):
            return paramiko.AUTH_SUCCESSFUL if (username, password) == ("test", "secret") else paramiko.AUTH_FAILED

        def get_allowed_auths(self, username):
            return "password"

        def check_channel_request(self, kind, channel_id):
            return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    class Files(paramiko.SFTPServerInterface):
        def local(self, path):
            result = (root / path.lstrip("/")).resolve()
            if not result.is_relative_to(root):
                raise PermissionError(path)
            return result

        def list_folder(self, path):
            try:
                result = []
                for child in self.local(path).iterdir():
                    info = paramiko.SFTPAttributes.from_stat(child.lstat())
                    info.filename = child.name
                    result.append(info)
                return result
            except OSError as exc:
                return paramiko.SFTPServer.convert_errno(exc.errno)

        def stat(self, path):
            try:
                return paramiko.SFTPAttributes.from_stat(self.local(path).stat())
            except OSError as exc:
                return paramiko.SFTPServer.convert_errno(exc.errno)

        lstat = stat

        def open(self, path, flags, attr):
            try:
                fd = os.open(self.local(path), flags, 0o600)
                mode = "wb" if flags & os.O_WRONLY else "r+b" if flags & os.O_RDWR else "rb"
                stream = os.fdopen(fd, mode)
                handle = paramiko.SFTPHandle(flags)
                handle.readfile = handle.writefile = stream
                return handle
            except OSError as exc:
                return paramiko.SFTPServer.convert_errno(exc.errno)

        def mkdir(self, path, attr):
            self.local(path).mkdir()
            return paramiko.SFTP_OK

        def remove(self, path):
            self.local(path).unlink()
            return paramiko.SFTP_OK

        def rmdir(self, path):
            self.local(path).rmdir()
            return paramiko.SFTP_OK

        def rename(self, oldpath, newpath):
            self.local(oldpath).rename(self.local(newpath))
            return paramiko.SFTP_OK

        def posix_rename(self, oldpath, newpath):
            os.replace(self.local(oldpath), self.local(newpath))
            return paramiko.SFTP_OK

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    listener.settimeout(0.1)
    port = listener.getsockname()[1]
    stopped = threading.Event()
    transports = []
    def serve():
        while not stopped.is_set():
            try:
                client, _ = listener.accept()
            except socket.timeout:
                continue
            transport = paramiko.Transport(client)
            transports.append(transport)
            transport.add_server_key(key)
            transport.set_subsystem_handler("sftp", paramiko.SFTPServer, Files)
            try:
                transport.start_server(server=Auth())
            except (EOFError, paramiko.SSHException):
                transport.close()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield port, root, key
    finally:
        stopped.set()
        thread.join(timeout=3)
        listener.close()
        for transport in transports:
            transport.close()


def test_sftp_trust_roundtrip_overwrite_and_delete(sftp_server, tmp_path):
    port, root, key = sftp_server
    known = tmp_path / "known_hosts"
    profile = Profile(host="127.0.0.1", port=port, username="test")
    backend = SFTPBackend(profile, "secret", known)
    with pytest.raises(HostKeyRequired) as required:
        backend.connect()
    assert required.value.fingerprint.startswith("SHA256:")
    hosts = paramiko.HostKeys()
    hosts.add(f"[127.0.0.1]:{port}", key.get_name(), key)
    hosts.save(str(known))
    source = tmp_path / "source"
    source.mkdir()
    (source / "空文件夹").mkdir()
    (source / "中文.txt").write_text("你好 SFTP")
    def make():
        return TransferEngine(backend, threading.Event(), lambda *_: None, "overwrite")
    try:
        assert backend.connect() == "/"
        make().upload(source, "/uploaded")
        assert (root / "uploaded" / "中文.txt").read_text() == "你好 SFTP"
        make().download("/uploaded", tmp_path / "download")
        assert (tmp_path / "download" / "中文.txt").read_text() == "你好 SFTP"
        (source / "中文.txt").write_text("updated")
        make().upload(source, "/uploaded")
        assert (root / "uploaded" / "中文.txt").read_text() == "updated"
        delete_remote(backend, backend.stat("/uploaded"), threading.Event())
        assert not (root / "uploaded").exists()
    finally:
        backend.close()
    # Changed host keys must never be silently accepted.
    hosts.add(f"[127.0.0.1]:{port}", key.get_name(), paramiko.RSAKey.generate(2048))
    hosts.save(str(known))
    with pytest.raises(paramiko.BadHostKeyException):
        backend.connect()
