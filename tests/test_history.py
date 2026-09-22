from dataclasses import replace

import pytest

from file_ez_mgr.config import Profile
from file_ez_mgr.history import DownloadHistory


def test_history_persists_deduplicates_and_isolates_connections(tmp_path):
    profile = Profile(host="server", username="alice")
    store = DownloadHistory(tmp_path, profile)
    store.record("/local/项目", "/remote/项目")
    store.record("/local/other", "/remote/other")
    store.record("/local/项目", "/remote/项目")
    items = DownloadHistory(tmp_path, profile).load()
    assert [i["remote_dir"] for i in items] == ["/remote/项目", "/remote/other"]
    assert items[0]["local_dir"] == "/local/项目"
    for changed in (replace(profile, host="different"), replace(profile, username="bob"), Profile(host="server")):
        assert DownloadHistory(tmp_path, changed).load() == []
    for n in range(60):
        store.record(f"/local/{n}", f"/remote/{n}")
    assert len(store.load()) == 50
    assert store.load()[0]["remote_dir"] == "/remote/59"


def test_corrupt_history_is_not_overwritten(tmp_path):
    store = DownloadHistory(tmp_path, Profile(host="server"))
    store.path.parent.mkdir(parents=True)
    store.path.write_text('broken')
    with pytest.raises(ValueError):
        store.record("/local", "/remote")
    assert store.path.read_text() == 'broken'


def test_legacy_history_deduplicates_pairs_and_delete_preserves_new_records(tmp_path):
    import json
    store = DownloadHistory(tmp_path, Profile(host='server'))
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps({'version': 1, 'items': [
        dict(name='file_a', local_dir='/local', remote_dir='/remote', time='2026-09-22'),
        dict(name='file_b', local_dir='/local', remote_dir='/remote', time='2026-09-21'),
        dict(name='dir', local_dir='/local/dir', remote_dir='/remote/dir', time='2026-09-20'),
    ]}))
    assert len(store.load()) == 2
    assert 'name' not in store.load()[0]
    selected = [('/local', '/remote')]
    store.record('/new', '/new-remote')
    store.delete(selected)
    assert [(i['local_dir'], i['remote_dir']) for i in store.load()] == [
        ('/new', '/new-remote'), ('/local/dir', '/remote/dir')]
    store.delete([('/new', '/new-remote'), ('/local/dir', '/remote/dir')])
    assert store.load() == []
    assert json.loads(store.path.read_text())['version'] == 2
