import os
import threading
import time
from pathlib import Path

import pytest

from file_ez_mgr.backends import FTPBackend, TFTPBackend
from file_ez_mgr.config import Profile
from file_ez_mgr.transfers import TransferEngine, delete_remote


@pytest.fixture
def ftp_server(tmp_path):
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer
    root = tmp_path / "ftp"
    root.mkdir()
    authorizer = DummyAuthorizer()
    authorizer.add_user("test", "secret", str(root), perm="elradfmwMT")
    class Handler(FTPHandler):
        pass
    Handler.authorizer = authorizer
    server = FTPServer(("127.0.0.1", 0), Handler)
    port = server.socket.getsockname()[1]
    stopped = threading.Event()
    def serve():
        while not stopped.is_set():
            server.serve_forever(timeout=0.02, blocking=False, handle_exit=False)
        server.close_all()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield port, root
    finally:
        stopped.set()
        thread.join(timeout=3)


def test_real_ftp_recursive_roundtrip(ftp_server, tmp_path):
    port, root = ftp_server
    backend = FTPBackend(Profile(protocol="ftp", host="127.0.0.1", port=port, username="test"), "secret")
    assert backend.connect() == "/"
    local = tmp_path / "本地文件"
    local.mkdir()
    (local / "nested").mkdir()
    (local / "empty").mkdir()
    payload = os.urandom(310000)
    (local / "nested" / "测试.bin").write_bytes(payload)
    progress = []
    def make(conflict="skip"):
        return TransferEngine(backend, threading.Event(), lambda done, total: progress.append((done, total)), conflict)
    try:
        make().upload(local, "/folder")
        assert (root / "folder" / "nested" / "测试.bin").read_bytes() == payload
        make().download("/folder", tmp_path / "download")
        assert (tmp_path / "download" / "nested" / "测试.bin").read_bytes() == payload
        (local / "nested" / "测试.bin").write_bytes(b"updated")
        make("overwrite").upload(local, "/folder")
        assert (root / "folder" / "nested" / "测试.bin").read_bytes() == b"updated"
        assert progress
        delete_remote(backend, backend.stat("/folder"), threading.Event())
        assert not (root / "folder").exists()
    finally:
        backend.close()


def test_real_ftp_cancel_recovers_session(ftp_server, tmp_path):
    from file_ez_mgr.backends import Cancelled
    port, root = ftp_server
    (root / "large.bin").write_bytes(os.urandom(300000))
    backend = FTPBackend(Profile(protocol="ftp", host="127.0.0.1", port=port, username="test"), "secret")
    backend.connect()
    event = threading.Event()
    def cancel_on_progress(done, total):
        event.set()
    try:
        with pytest.raises(Cancelled):
            TransferEngine(backend, event, cancel_on_progress).download("/large.bin", tmp_path / "result.bin")
        assert not (tmp_path / "result.bin").exists()
        assert backend.listdir("/")[0].name == "large.bin"
    finally:
        backend.close()


def test_real_tftp_roundtrip(tmp_path):
    import socket
    import tftpy
    root = tmp_path / "tftp"
    root.mkdir()
    server = tftpy.TftpServer(str(root))
    # Bind an ephemeral UDP port, then start the isolated test server.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    thread = threading.Thread(target=server.listen, kwargs={"listenip": "127.0.0.1", "listenport": port, "timeout": 0.2}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 3
    while not server.is_running.is_set() and time.monotonic() < deadline:
        time.sleep(0.01)
    backend = TFTPBackend(Profile(protocol="tftp", host="127.0.0.1", port=port))
    source = tmp_path / "payload.bin"
    source.write_bytes(os.urandom(2048))
    try:
        engine = TransferEngine(backend, threading.Event(), lambda *_: None)
        engine.upload(source, "payload.bin")
        engine.download("payload.bin", tmp_path / "received.bin")
        assert (tmp_path / "received.bin").read_bytes() == source.read_bytes()
    finally:
        server.stop(now=True)
        thread.join(timeout=5)
