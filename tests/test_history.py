from dataclasses import replace

import pytest

from file_ez_mgr.config import Profile
from file_ez_mgr.history import DownloadHistory


def test_history_persists_deduplicates_and_isolates_connections(tmp_path):
    profile = Profile(host="server", username="alice")
    store = DownloadHistory(tmp_path, profile)
    store.record("项目", "/local/项目", "/remote/项目")
    store.record("other", "/local/other", "/remote/other")
    store.record("项目", "/local/项目", "/remote/项目")
    items = DownloadHistory(tmp_path, profile).load()
    assert [i["name"] for i in items] == ["项目", "other"]
    assert items[0]["local_dir"] == "/local/项目"
    for changed in (replace(profile, host="different"), replace(profile, username="bob"), Profile(host="server")):
        assert DownloadHistory(tmp_path, changed).load() == []
    for n in range(60):
        store.record(str(n), f"/local/{n}", f"/remote/{n}")
    assert len(store.load()) == 50
    assert store.load()[0]["name"] == "59"


def test_corrupt_history_is_not_overwritten(tmp_path):
    store = DownloadHistory(tmp_path, Profile(host="server"))
    store.path.parent.mkdir(parents=True)
    store.path.write_text('broken')
    with pytest.raises(ValueError):
        store.record("file", "/local", "/remote")
    assert store.path.read_text() == 'broken'
