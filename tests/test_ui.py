import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from file_ez_mgr.app import MainWindow, STYLE
from file_ez_mgr.backends import Entry
from file_ez_mgr.widgets import FilePane
from file_ez_mgr.workers import WorkerQueue


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    application.setStyle("Fusion")
    application.setStyleSheet(STYLE)
    yield application


def test_window_startup_and_parent_sort(app, tmp_path):
    window = MainWindow(tmp_path / "config")
    tab = window.tabs.currentWidget()
    (tmp_path / "directory").mkdir()
    (tmp_path / "small.txt").write_bytes(b"x")
    (tmp_path / "large.txt").write_bytes(b"x" * 2048)
    tab.browse_local(str(tmp_path))
    window.show()
    app.processEvents()
    tree = tab.local.tree
    for column in range(5):
        for order in (Qt.AscendingOrder, Qt.DescendingOrder):
            tree.sortItems(column, order)
            assert tree.topLevelItem(0).entry.name == ".."
    tree.sortItems(3, Qt.DescendingOrder)
    files = [tree.topLevelItem(i).entry for i in range(tree.topLevelItemCount()) if not tree.topLevelItem(i).entry.is_dir]
    assert [e.size for e in files] == [2048, 1]
    window.close()
    app.processEvents()


def test_drag_has_real_file_and_directory_urls(app, tmp_path):
    directory, file = tmp_path / "中文 文件夹", tmp_path / "file.txt"
    directory.mkdir()
    file.write_text("data")
    pane = FilePane(local=True)
    pane.show_entries(str(tmp_path), [Entry(directory.name, str(directory), True), Entry(file.name, str(file), False)])
    for i in range(pane.tree.topLevelItemCount()):
        pane.tree.topLevelItem(i).setSelected(True)
    urls = pane.tree.selection_mime().urls()
    assert {url.toLocalFile() for url in urls} == {str(file), str(directory)}
    assert all(url.isLocalFile() for url in urls)


def test_serial_worker_and_cancel_queued_job(app):
    queue = WorkerQueue()
    results = []
    gate = __import__("threading").Event()
    queue.submit("first", lambda cancel, progress: gate.wait(2), lambda value: results.append("first"))
    cancelled = queue.submit("cancel", lambda cancel, progress: results.append("should not run"))
    cancelled.cancel.set()
    queue.submit("last", lambda cancel, progress: "last", lambda value: results.append(value))
    gate.set()
    deadline = time.monotonic() + 5
    while queue.jobs and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    assert not queue.jobs
    assert results == ["first", "last"]
    queue.shutdown()


def test_session_connect_upload_download_and_remote_edit(app, tmp_path, monkeypatch):
    from test_protocols import ftp_server
    from file_ez_mgr.config import Profile
    from file_ez_mgr.session import SessionTab
    from PyQt5.QtGui import QDesktopServices
    # Reuse the same real server fixture as a context-managed generator.
    server = ftp_server.__wrapped__(tmp_path)
    port, root = next(server)
    local = tmp_path / "local"
    local.mkdir()
    source = local / "document.txt"
    source.write_text("from local")
    failures = []
    monkeypatch.setattr(SessionTab, "show_error", lambda self, error: failures.append(str(error)))
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: True)
    tab = SessionTab(Profile(protocol="ftp", host="127.0.0.1", port=port, username="test", local_dir=str(local)),
                     "secret", tmp_path / "config")
    def drain():
        deadline = time.monotonic() + 6
        while tab.queue.jobs and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        app.processEvents()
        assert not tab.queue.jobs
        assert not failures
    try:
        drain()
        assert tab.connected
        tab.upload_paths([str(source)])
        drain()
        assert (root / source.name).read_text() == "from local"
        source.unlink()
        item = next(tab.remote.tree.topLevelItem(i) for i in range(tab.remote.tree.topLevelItemCount())
                    if tab.remote.tree.topLevelItem(i).entry.name == source.name)
        item.setSelected(True)
        tab.download_selected()
        drain()
        assert source.read_text() == "from local"
        tab.edit_remote(item.entry)
        drain()
        assert tab.edited["/document.txt"].read_text() == "from local"
        assert tab.edit_button.isEnabled()
        tab.edit_remote_direct(item.entry)
        drain()
        editor = tab.remote_editors['/document.txt']
        editor.text.selectAll()
        editor.text.insertPlainText('saved directly to remote')
        editor.save()
        drain()
        assert (root / source.name).read_text() == 'saved directly to remote'
        assert not editor.text.document().isModified()
        # A second save verifies that the baseline was advanced after the first.
        editor.text.insertPlainText(' again')
        editor.save()
        drain()
        assert (root / source.name).read_text() == 'saved directly to remote again'
        (root / source.name).write_text('modified elsewhere')
        editor.text.insertPlainText(' pending edit')
        editor.save()
        drain()
        assert (root / source.name).read_text() == 'modified elsewhere'
        assert editor.text.document().isModified()
        assert not editor.saving
        assert '其他操作' in editor.status.text()
        # A queued save cancellation must re-enable the editor and retain text.
        gate = __import__('threading').Event()
        tab.queue.submit('block', lambda cancel, progress: gate.wait(2))
        editor.save()
        next(job for job in tab.queue.jobs.values() if job.title.startswith('保存远端文件')).cancel.set()
        gate.set()
        drain()
        assert not editor.saving
        assert editor.text.document().isModified()
    finally:
        tab.shutdown()
        tab.queue.executor.shutdown(wait=True)
        try:
            next(server)
        except StopIteration:
            pass


@pytest.mark.parametrize('local', [True, False])
def test_default_sort_is_newest_first_and_survives_refresh(app, local):
    pane = FilePane(local=local)
    entries = [Entry('old-folder', '/old-folder', True, modified=100),
               Entry('new.txt', '/new.txt', False, modified=300),
               Entry('middle', '/middle', True, modified=200)]
    for _ in range(2):
        pane.show_entries('/', entries)
        assert pane.tree.sortColumn() == 4
        assert pane.tree.header().sortIndicatorOrder() == Qt.DescendingOrder
        assert [pane.tree.topLevelItem(i).entry.name for i in range(4)] == [
            '..', 'new.txt', 'middle', 'old-folder']


@pytest.mark.parametrize('protocol,key_file,expect_prompt', [
    ('sftp', '/tmp/key', False), ('sftp', '', True), ('ftp', '/tmp/key', True)])
def test_private_key_skips_password_prompt_on_open_and_reconnect(
        app, tmp_path, monkeypatch, protocol, key_file, expect_prompt):
    from file_ez_mgr.config import Profile
    from file_ez_mgr.session import SessionTab
    from PyQt5.QtWidgets import QInputDialog
    prompts, connections = [], []
    def prompt(*args, **kwargs):
        prompts.append(True)
        return '', True
    monkeypatch.setattr(QInputDialog, 'getText', prompt)
    monkeypatch.setattr(SessionTab, 'connect_remote', lambda self: connections.append(self))
    window = MainWindow(tmp_path / 'config')
    window.profiles = [Profile(protocol=protocol, key_file=key_file, host='localhost', local_dir=str(tmp_path))]
    window.refresh_profiles()
    try:
        window.open_connection()
        tab = window.tabs.currentWidget()
        assert len(prompts) == int(expect_prompt)
        tab.reconnect()
        assert len(prompts) == 2 * int(expect_prompt)
        assert connections == [tab, tab]
        assert tab.backend.password == ''
    finally:
        window.close()
        app.processEvents()


def test_remote_editor_encoding_failure_and_close_protection(app, monkeypatch):
    from file_ez_mgr.editor import RemoteEditor
    from PyQt5.QtWidgets import QMessageBox
    saved = []
    def save(data, finished):
        saved.append((data, finished))
    editor = RemoteEditor('/hello.txt', b'\xef\xbb\xbfhello\r\nworld\r\n', save)
    editor.text.selectAll()
    editor.text.insertPlainText('changed\nworld\n')
    editor.save()
    assert saved[0][0] == b'\xef\xbb\xbfchanged\r\nworld\r\n'
    assert editor.saving
    saved[0][1](OSError('failed'))
    assert not editor.saving
    assert editor.text.document().isModified()
    monkeypatch.setattr(QMessageBox, 'question', lambda *args: QMessageBox.Cancel)
    assert not editor.can_close()
    editor.save()
    saved[1][1]()
    assert not editor.text.document().isModified()
    assert editor.can_close()
    editor.close()
    for data in (b'\xff\xff', b'hello\x00binary'):
        with pytest.raises(ValueError):
            RemoteEditor('/binary', data, save)


def test_download_history_and_default_overwrite_without_confirmation(app, tmp_path, monkeypatch):
    from test_protocols import ftp_server
    from file_ez_mgr.config import Profile
    from file_ez_mgr.history import DownloadHistory
    from file_ez_mgr.session import SessionTab
    from PyQt5.QtWidgets import QMessageBox
    server = ftp_server.__wrapped__(tmp_path)
    port, root = next(server)
    project = root / "项目"
    project.mkdir()
    (project / "file.txt").write_text("remote new")
    local = tmp_path / "local"
    (local / "项目").mkdir(parents=True)
    (local / "项目" / "file.txt").write_text("local old")
    errors = []
    monkeypatch.setattr(SessionTab, "show_error", lambda self, error: errors.append(str(error)))
    def unexpected_question(*args, **kwargs):
        pytest.fail("Overwrite should not show a confirmation dialog")
    monkeypatch.setattr(QMessageBox, "question", unexpected_question)
    profile = Profile(protocol="ftp", host="127.0.0.1", port=port, username="test", local_dir=str(local))
    tab = SessionTab(profile, "secret", tmp_path / "config")
    def drain():
        deadline = time.monotonic() + 5
        while tab.queue.jobs and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        app.processEvents()
        assert not tab.queue.jobs
    def select(name):
        tab.remote.tree.clearSelection()
        next(tab.remote.tree.topLevelItem(i) for i in range(tab.remote.tree.topLevelItemCount())
             if tab.remote.tree.topLevelItem(i).entry.name == name).setSelected(True)
    try:
        drain()
        assert tab.conflict.currentData() == "overwrite"
        select("项目")
        tab.download_selected()
        # The saved destination must be the queued download's directory,
        # even if the user navigates elsewhere before completion.
        tab.browse_local(str(tmp_path))
        drain()
        assert not errors
        assert (local / "项目" / "file.txt").read_text() == "remote new"
        history = DownloadHistory(tmp_path / "config", profile).load()
        assert len(history) == 1
        assert history[0]["local_dir"] == str(local / "项目")
        assert history[0]["remote_dir"] == "/项目"
        tab.local.search.setText("no matches")
        tab.remote.search.setText("no matches")
        tab.restore_history(1)
        drain()
        assert tab.local.path == str(local / "项目")
        assert tab.remote.path == "/项目"
        assert tab.local.search.text() == tab.remote.search.text() == ""
        # Upload also overwrites silently by default.
        (local / "项目" / "file.txt").write_text("uploaded change")
        tab.upload_paths([str(local / "项目" / "file.txt")])
        drain()
        assert (project / "file.txt").read_text() == "uploaded change"
        select("file.txt")
        (project / "file.txt").unlink()
        tab.download_selected()
        drain()
        assert errors
        assert DownloadHistory(tmp_path / "config", profile).load() == history
    finally:
        tab.shutdown()
        tab.queue.executor.shutdown(wait=True)
        try:
            next(server)
        except StopIteration:
            pass


def test_workspace_restores_tab_order_selection_paths_and_auth(app, tmp_path, monkeypatch):
    from file_ez_mgr.config import Profile, ConfigStore
    from file_ez_mgr.session import SessionTab
    from PyQt5.QtWidgets import QInputDialog
    config = tmp_path / 'config'
    local = tmp_path / 'project'
    local.mkdir()
    key_profile = Profile(name='SSH', host='ssh.test', key_file='/keys/id', local_dir=str(local))
    password_profile = Profile(name='FTP', protocol='ftp', host='ftp.test', local_dir=str(local))
    ConfigStore(config).save([key_profile, password_profile])
    calls = []
    monkeypatch.setattr(SessionTab, 'connect_remote', lambda self: calls.append(self.profile.id))
    monkeypatch.setattr(QInputDialog, 'getText', lambda *args, **kwargs: pytest.fail('Unexpected startup password prompt'))
    window = MainWindow(config)
    window.tabs.currentWidget().browse_local(str(local))
    for profile, path in [(key_profile, '/srv/project'), (password_profile, '/data/project'), (key_profile, '/srv/other')]:
        tab = SessionTab(profile, config_dir=config, parent=window, auto_connect=False)
        tab.remote.path = path
        window.tabs.addTab(tab, profile.name)
    window.tabs.tabBar().moveTab(3, 0)
    window.tabs.setCurrentIndex(3)
    window.profile_combo.setCurrentIndex(window.profile_combo.findData(password_profile.id))
    window.tabs.currentWidget().conflict.setCurrentIndex(0)
    assert window.close()
    content = (config / 'workspace.json').read_text()
    assert 'password' not in content and '/keys/id' not in content
    restored = MainWindow(config)
    try:
        assert restored.tabs.count() == 4
        assert [restored.tabs.tabText(i) for i in range(4)] == ['SSH', '本地浏览', 'SSH', 'FTP']
        assert restored.tabs.currentIndex() == 3
        assert restored.tabs.currentWidget().remote.path == '/data/project'
        assert restored.tabs.currentWidget().local.path == str(local)
        assert restored.tabs.currentWidget().conflict.currentData() == 'skip'
        assert restored.tabs.widget(0).remote.path == '/srv/other'
        assert restored.tabs.widget(2).remote.path == '/srv/project'
        assert calls == [key_profile.id, key_profile.id]
        assert not restored.tabs.currentWidget().connected
        assert restored.selected_profile().id == password_profile.id
    finally:
        restored.close()


def test_workspace_missing_profile_and_directory_fallback(app, tmp_path):
    from file_ez_mgr.workspace_state import WorkspaceStateStore
    store = WorkspaceStateStore(tmp_path)
    store.save([dict(profile_id='deleted', local_dir='/missing', remote_dir='/remote'),
                dict(profile_id=None, local_dir=str(tmp_path / 'missing'), remote_dir='')], 0, None)
    window = MainWindow(tmp_path)
    try:
        assert window.tabs.count() == 1
        assert window.tabs.currentWidget().profile is None
        assert window.tabs.currentWidget().local.path == str(Path.home())
    finally:
        window.close()


def test_corrupt_workspace_preserved_and_cancelled_close_not_saved(app, tmp_path, monkeypatch):
    config = tmp_path / 'config'
    config.mkdir()
    path = config / 'workspace.json'
    path.write_text('broken')
    window = MainWindow(config)
    assert window.workspace_error is not None
    window.close()
    assert path.read_text() == 'broken'
    path.unlink()
    window = MainWindow(config)
    window.show()
    monkeypatch.setattr(window, 'confirm_close', lambda tabs: False)
    assert not window.close()
    assert not path.exists()
    monkeypatch.setattr(window, 'confirm_close', lambda tabs: True)
    window.close()
    assert path.exists()


def test_application_quit_restores_active_tab(tmp_path):
    import subprocess
    import sys
    from file_ez_mgr.workspace_state import WorkspaceStateStore

    # Exercise a real Qt event loop: app.quit() need not send a window close event.
    script = '''
import sys
from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication
from file_ez_mgr.app import MainWindow
app = QApplication([])
window = MainWindow(sys.argv[1])
app.aboutToQuit.connect(window.save_workspace_on_quit)
window.add_local_tab()
window.tabs.setCurrentIndex(1)
window.tabs.currentWidget().browse_local(sys.argv[1])
window.show()
QTimer.singleShot(0, app.quit)
sys.exit(app.exec_())
'''
    subprocess.run([sys.executable, '-c', script, str(tmp_path)], check=True,
                   timeout=20, env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen'})
    state = WorkspaceStateStore(tmp_path).load()
    assert state['active_index'] == 1
    assert len(state['tabs']) == 2
    assert state['tabs'][1]['local_dir'] == str(tmp_path)
