import json
import os
import threading
from pathlib import Path

import pytest

from file_ez_mgr.backends import Cancelled, Entry, local_child, safe_name
from file_ez_mgr.config import ConfigStore, Profile
from file_ez_mgr.transfers import TransferEngine, delete_remote


class DiskBackend:
    browsable = True

    def stat(self, path):
        path = Path(path)
        if not path.exists() and not path.is_symlink():
            return None
        return Entry(path.name, str(path), path.is_dir(), path.lstat().st_size, is_link=path.is_symlink())

    def listdir(self, path):
        return [self.stat(p) for p in Path(path).iterdir()]

    def join(self, parent, name):
        return str(Path(parent) / safe_name(name))

    def mkdir(self, path):
        Path(path).mkdir()

    def remove(self, path, directory=False):
        (Path(path).rmdir if directory else Path(path).unlink)()

    def rename(self, source, target, overwrite=False):
        os.replace(source, target)

    def upload(self, source, target, progress, cancel):
        Path(target).write_bytes(Path(source).read_bytes())
        progress(Path(source).stat().st_size, Path(source).stat().st_size)

    download = upload


def engine(backend=None, conflict="skip", cancel=None):
    return TransferEngine(backend or DiskBackend(), cancel or threading.Event(), lambda *_: None, conflict)


def test_config_roundtrip_and_no_password(tmp_path):
    store = ConfigStore(tmp_path)
    profiles = [Profile(name="远端开发机", host="localhost", local_dir="/tmp/中文", remote_dir="/srv/data")]
    store.save(profiles)
    assert store.load() == profiles
    assert "password" not in store.path.read_text()
    store.path.write_text('{"version": 2, "connections": []}')
    with pytest.raises(ValueError):
        store.load()


@pytest.mark.parametrize("name", ["..", ".", "../secret", "/tmp", "a/b", "a\\b", "a\nDELE x", "\x00"])
def test_untrusted_remote_names(name):
    with pytest.raises(ValueError):
        safe_name(name)


def test_roundtrip_nested_empty_and_unicode(tmp_path):
    source, remote, destination = [tmp_path / name for name in ("source", "remote", "destination")]
    source.mkdir()
    remote.mkdir()
    (source / "空目录").mkdir()
    (source / "子目录").mkdir()
    (source / "子目录" / "你好.txt").write_text("你好 world")
    (source / "empty").write_bytes(b"")
    upload = engine()
    upload.upload(source, str(remote / "source"))
    assert upload.result.files == 2
    download = engine()
    download.download(str(remote / "source"), destination)
    assert (destination / "子目录" / "你好.txt").read_text() == "你好 world"
    assert (destination / "空目录").is_dir()
    delete_remote(DiskBackend(), DiskBackend().stat(remote / "source"), threading.Event())
    assert not (remote / "source").exists()


def test_conflict_skip_and_replace(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_text("new")
    target.write_text("old")
    worker = engine()
    worker.download(str(source), target)
    assert target.read_text() == "old"
    assert worker.result.skipped == 1
    engine(conflict="overwrite").download(str(source), target)
    assert target.read_text() == "new"
    target.write_text("old")
    engine(conflict="overwrite").upload(source, str(target))
    assert target.read_text() == "new"


@pytest.mark.parametrize("direction", ["upload", "download"])
def test_failure_preserves_original_and_cleans_temp(tmp_path, direction):
    class Broken(DiskBackend):
        def upload(self, source, target, progress, cancel):
            Path(target).write_bytes(b"partial")
            raise OSError("network failed")
        download = upload
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_text("new")
    target.write_text("original")
    with pytest.raises(OSError):
        getattr(engine(Broken(), "overwrite"), direction)(source, target)
    assert target.read_text() == "original"
    assert not list(tmp_path.glob(".file-ez-*"))


def test_cancel_and_symlink_protection(tmp_path):
    event = threading.Event()
    event.set()
    with pytest.raises(Cancelled):
        engine(cancel=event).download("anything", tmp_path / "target")
    source = tmp_path / "source"
    source.write_text("data")
    symlink = tmp_path / "link"
    try:
        symlink.symlink_to(source)
    except OSError:
        pytest.skip("symlinks unavailable")
    worker = engine()
    worker.upload(symlink, str(tmp_path / "target"))
    assert worker.result.files == 0
    assert worker.result.skipped_links == [str(symlink)]
    assert not (tmp_path / "target").exists()
    with pytest.raises(ValueError):
        local_child(tmp_path, "link")


@pytest.mark.parametrize("direction", ["upload", "download"])
def test_directory_transfer_skips_links_and_continues(tmp_path, direction):
    source = tmp_path / "project"
    source.mkdir()
    bin_dir = source / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("must not be transferred")
    links = [bin_dir / "python", source / "cycle", source / "outside", source / "broken"]
    try:
        links[0].symlink_to("python3.13")
        links[1].symlink_to(source, target_is_directory=True)
        links[2].symlink_to(outside, target_is_directory=True)
        links[3].symlink_to(tmp_path / "missing")
    except OSError:
        pytest.skip("symlinks unavailable")
    (source / "main.py").write_text("print('hello')")
    (source / "z-last.txt").write_text("still transferred after links")
    target = tmp_path / "received"
    worker = engine()
    getattr(worker, direction)(source, target)
    assert (target / "main.py").read_text() == "print('hello')"
    assert (target / "z-last.txt").read_text() == "still transferred after links"
    assert worker.result.files == 2
    assert worker.result.skipped == 4
    assert set(worker.result.skipped_links) == {str(link) for link in links}
    assert "符号链接 4 项" in str(worker.result)
    for link in links:
        assert str(link) in worker.result.details
        destination = target / link.relative_to(source)
        assert not destination.exists() and not destination.is_symlink()


@pytest.mark.parametrize("direction", ["upload", "download"])
def test_destination_link_still_protected(tmp_path, direction):
    source, outside, target = [tmp_path / name for name in ("source", "outside", "target")]
    source.write_text("new")
    outside.write_text("original")
    try:
        target.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError, match="符号链接"):
        getattr(engine(conflict="overwrite"), direction)(source, target)
    assert outside.read_text() == "original"
    assert target.is_symlink()
