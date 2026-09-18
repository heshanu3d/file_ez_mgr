from __future__ import annotations

import os
import posixpath
import shutil
import tempfile
import weakref
from pathlib import Path

from PyQt5.QtCore import Qt, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QInputDialog, QLabel,
                             QLineEdit, QMenu, QMessageBox, QPushButton, QSplitter,
                             QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .backends import Cancelled, HostKeyRequired, create_backend, local_child, local_entries, safe_name
from .transfers import TransferEngine, TransferResult, delete_remote
from .editor import MAX_EDIT_BYTES, RemoteEditor
from .widgets import FilePane, HistoryComboBox
from .history import DownloadHistory
from .workers import WorkerQueue


class SessionTab(QWidget):
    stateChanged = pyqtSignal(str)

    def __init__(self, profile=None, password="", config_dir=None, parent=None,
                 *, auto_connect=True, local_dir=None, remote_dir=None):
        super().__init__(parent)
        self.profile = profile
        self.config_dir = Path(config_dir or tempfile.gettempdir())
        self.history_store = DownloadHistory(self.config_dir, profile) if profile else None
        self.backend = create_backend(profile, password, self.config_dir / "known_hosts") if profile else None
        self.connected = False
        self.closing = False
        self._listing_sequence = 0
        self.edited = {}
        self.remote_editors = {}
        self.loading_editors = set()
        self.edit_temp = tempfile.TemporaryDirectory(prefix="file-ez-edit-")
        self.queue = WorkerQueue(self)
        self.rows = {}
        self.queue.started.connect(self.job_started)
        self.queue.progressed.connect(self.job_progress)
        self.queue.finished.connect(self.job_finished)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        tools = QHBoxLayout()
        self.connection_status = QLabel("本地浏览 · 新建连接即可开始传输")
        self.connection_status.setObjectName("muted")
        tools.addWidget(self.connection_status)
        tools.addStretch()
        tools.addWidget(QLabel("下载历史"))
        self.history = HistoryComboBox()
        self.history.setFixedWidth(230)
        self.history.setToolTip("选择下载历史，同时打开本地和远端目录")
        self.history.opening.connect(self.refresh_history)
        self.history.activated[int].connect(self.restore_history)
        tools.addWidget(self.history)
        tools.addWidget(QLabel("同名文件"))
        self.conflict = QComboBox()
        self.conflict.addItem("跳过", "skip")
        self.conflict.addItem("覆盖", "overwrite")
        self.conflict.setCurrentIndex(self.conflict.findData("overwrite"))
        tools.addWidget(self.conflict)
        self.reconnect_button = QPushButton("重新连接")
        self.reconnect_button.clicked.connect(self.reconnect)
        self.reconnect_button.setVisible(profile is not None)
        tools.addWidget(self.reconnect_button)
        layout.addLayout(tools)
        vertical = QSplitter(Qt.Vertical)
        browsers = QWidget()
        browser_layout = QHBoxLayout(browsers)
        browser_layout.setContentsMargins(0, 0, 0, 0)
        self.local = FilePane(local=True)
        self.remote = FilePane(local=False)
        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.local)
        split.addWidget(self.remote)
        split.setSizes([600, 600])
        browser_layout.addWidget(split)
        vertical.addWidget(browsers)
        queue_panel = QFrame()
        queue_panel.setObjectName("filePane")
        qlayout = QVBoxLayout(queue_panel)
        toolbar = QHBoxLayout()
        title = QLabel("传输与操作")
        title.setObjectName("paneTitle")
        toolbar.addWidget(title)
        toolbar.addStretch()
        self.upload_button = QPushButton("上传 →")
        self.upload_button.setObjectName("primary")
        self.upload_button.clicked.connect(lambda: self.upload_paths([e.path for e in self.local.tree.selected_entries()]))
        self.download_button = QPushButton("← 下载")
        self.download_button.clicked.connect(self.download_selected)
        self.edit_button = QPushButton("回传已编辑文件")
        self.edit_button.clicked.connect(self.upload_edited)
        self.edit_button.setEnabled(False)
        self.cancel_button = QPushButton("取消选中任务")
        self.cancel_button.clicked.connect(self.cancel_selected)
        clear = QPushButton("清除已完成")
        clear.clicked.connect(self.clear_finished)
        for button in (self.upload_button, self.download_button, self.edit_button, self.cancel_button, clear):
            toolbar.addWidget(button)
        qlayout.addLayout(toolbar)
        self.jobs_view = QTreeWidget()
        self.jobs_view.setHeaderLabels(["任务", "进度", "状态 / 结果"])
        self.jobs_view.setRootIsDecorated(False)
        self.jobs_view.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.jobs_view.setColumnWidth(0, 430)
        self.jobs_view.setColumnWidth(1, 90)
        qlayout.addWidget(self.jobs_view)
        vertical.addWidget(queue_panel)
        vertical.setSizes([510, 190])
        vertical.setStretchFactor(0, 1)
        layout.addWidget(vertical, 1)
        self.local.navigate.connect(self.browse_local)
        self.local.refresh.connect(lambda: self.browse_local(self.local.path))
        self.remote.navigate.connect(self.browse_remote)
        self.remote.refresh.connect(lambda: self.browse_remote(self.remote.path))
        self.local.activated.connect(lambda entry: self.open_local(entry.path))
        self.remote.activated.connect(self.edit_remote)
        self.local.tree.customContextMenuRequested.connect(lambda point: self.context_menu(self.local, point))
        self.remote.tree.customContextMenuRequested.connect(lambda point: self.context_menu(self.remote, point))
        self.remote.tree.filesDropped.connect(self.upload_paths)
        self.set_connected(False)
        initial = os.path.expanduser(local_dir or (profile.local_dir if profile else str(Path.home())))
        if not Path(initial).is_dir():
            initial = str(Path.home())
            self.connection_status.setText("默认本地目录不存在，已打开用户目录")
        self.browse_local(initial)
        self.refresh_history()
        if profile:
            self.remote.path = remote_dir or ""
            self.remote.address.setText(remote_dir or profile.remote_dir)
            if auto_connect:
                self.connect_remote()
            else:
                self.connection_status.setText(f"已恢复 {profile.name} · 点击「重新连接」输入凭据")
        else:
            self.remote.note.setText("点击顶部「新建连接」，保存服务器地址和默认目录。\n每个连接将在独立标签页中打开。")
            self.remote.note.show()

    def show_error(self, error):
        if not self.closing and not isinstance(error, Cancelled):
            QMessageBox.warning(self, "操作未完成", str(error) or type(error).__name__)

    def refresh_history(self):
        selected = self.history.currentData()
        self.history.clear()
        self.history.addItem("选择下载记录…", None)
        if not self.history_store:
            return
        try:
            for item in self.history_store.load():
                self.history.addItem(f"{item['name']} · {item['remote_dir']}", item)
                index = self.history.count() - 1
                self.history.setItemData(index, f"本地：{item['local_dir']}\n远端：{item['remote_dir']}\n下载时间：{item['time']}", Qt.ToolTipRole)
                if selected == item:
                    self.history.setCurrentIndex(index)
        except Exception as exc:
            self.connection_status.setText(f"下载历史读取失败：{exc}")

    def record_download(self, result, source, target, is_dir):
        if self.closing or not self.history_store or not self.backend.browsable:
            return
        if result.files == 0 and result.skipped:
            return
        try:
            self.history_store.record(posixpath.basename(source),
                                      target if is_dir else target.parent,
                                      source if is_dir else posixpath.dirname(source) or "/")
            self.refresh_history()
        except Exception as exc:
            self.connection_status.setText(f"下载已完成，但历史保存失败：{exc}")

    def restore_history(self, index):
        item = self.history.itemData(index)
        if not item or not self.connected:
            return
        self.local.search.clear()
        self.remote.search.clear()
        self.browse_local(item["local_dir"])
        self.browse_remote(item["remote_dir"])

    def set_connected(self, connected):
        self.connected = connected
        self.upload_button.setEnabled(connected)
        self.download_button.setEnabled(connected)
        self.remote.setEnabled(connected)
        self.history.setEnabled(bool(connected and self.backend and self.backend.browsable))
        if self.backend and not self.backend.browsable:
            self.download_button.setText("按路径下载…")

    def submit(self, title, function, callback=None, failure=None, visible=True):
        job = self.queue.submit(title, function, callback, failure or self.show_error, visible)
        if visible:
            item = QTreeWidgetItem([title, "—", "等待中"])
            item.setData(0, Qt.UserRole, job.id)
            item.setToolTip(0, title)
            self.jobs_view.addTopLevelItem(item)
            self.rows[job.id] = item
        return job

    def job_started(self, job_id):
        if job_id in self.rows:
            self.rows[job_id].setText(2, "进行中")

    def job_progress(self, job_id, done, total):
        if job_id in self.rows:
            self.rows[job_id].setText(1, f"{done / total:.0%}" if total else "…")

    def job_finished(self, job_id, value, error):
        if job_id in self.rows:
            item = self.rows[job_id]
            if error:
                item.setText(2, "已取消" if isinstance(error, Cancelled) else "失败：" + str(error))
                item.setToolTip(2, str(error))
            else:
                item.setText(1, "100%")
                item.setText(2, str(value) if value else "完成")
                if isinstance(value, TransferResult):
                    item.setToolTip(2, value.details)
        if not self.queue.jobs and not self.closing:
            self.browse_local(self.local.path)

    def cancel_selected(self):
        for item in self.jobs_view.selectedItems():
            job = self.queue.jobs.get(item.data(0, Qt.UserRole))
            if job:
                job.cancel.set()
                item.setText(2, "正在取消…")

    def clear_finished(self):
        for job_id in list(self.rows):
            if job_id not in self.queue.jobs:
                item = self.rows.pop(job_id)
                self.jobs_view.takeTopLevelItem(self.jobs_view.indexOfTopLevelItem(item))

    def connect_remote(self):
        self.set_connected(False)
        self.reconnect_button.setEnabled(False)
        self.connection_status.setText(f"正在连接 {self.profile.host}:{self.profile.port}…")
        def connect(cancel, progress):
            self.backend.close()
            return self.backend.connect()
        def ready(path):
            if self.closing:
                return
            self.reconnect_button.setEnabled(True)
            self.set_connected(True)
            protocol = self.profile.protocol.upper()
            state = "待传输验证" if not self.backend.browsable else "已连接"
            self.connection_status.setText(f"● {state}  ·  {protocol}  ·  {self.profile.host}:{self.profile.port}")
            self.remote.badge.setText(protocol)
            self.stateChanged.emit(state)
            path = self.remote.path or path
            self.remote.path = path
            self.remote.address.setText(path)
            if self.backend.browsable:
                self.browse_remote(path)
            else:
                self.remote.note.setText("TFTP 无目录浏览功能。上传使用本地选中文件；下载请填写远端文件路径。\n连接可达性在实际传输时验证。")
                self.remote.note.show()
                self.remote.search.setEnabled(False)
        self.submit("连接 " + self.profile.name, connect, ready, self.connection_failed, visible=False)

    def connection_failed(self, error):
        self.reconnect_button.setEnabled(True)
        self.connection_status.setText("连接失败 · 可修改配置或重新连接")
        self.stateChanged.emit("连接失败")
        if self.closing:
            return
        if isinstance(error, HostKeyRequired):
            answer = QMessageBox.question(self, "核对 SSH 主机指纹",
                f"{error}\n\n请与服务器管理员提供的指纹核对。信任并保存此主机密钥？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer == QMessageBox.Yes:
                try:
                    import paramiko
                    self.config_dir.mkdir(parents=True, exist_ok=True)
                    path = self.config_dir / "known_hosts"
                    keys = paramiko.HostKeys(str(path)) if path.exists() else paramiko.HostKeys()
                    keys.add(error.hostname, error.key.get_name(), error.key)
                    keys.save(str(path))
                    self.connect_remote()
                except Exception as exc:
                    self.show_error(exc)
        else:
            self.show_error(error)

    def reconnect(self):
        if self.queue.jobs:
            self.show_error("请等待当前任务完成后重新连接。")
            return
        uses_private_key = self.profile.protocol == "sftp" and bool(self.profile.key_file.strip())
        if self.profile.protocol != "tftp" and not uses_private_key:
            password, ok = QInputDialog.getText(self, "重新连接", "密码 / 私钥口令（可留空使用 SSH Agent）", QLineEdit.Password)
            if not ok:
                return
            self.backend.password = password
        self.connect_remote()

    def browse_local(self, path):
        try:
            destination = str(Path(path).expanduser().absolute())
            entries = local_entries(destination)
            self.local.show_entries(destination, entries)
        except Exception as exc:
            self.local.address.setText(self.local.path)
            self.show_error(exc)

    def browse_remote(self, path):
        if not self.connected or self.closing:
            return
        if not self.backend.browsable:
            self.remote.path = path
            return
        self._listing_sequence += 1
        sequence = self._listing_sequence
        requested = path if path.startswith("/") else posixpath.join(self.remote.path or ".", path)
        def listing(cancel, progress):
            normalized = self.backend.normalize(requested)
            return normalized, self.backend.listdir(normalized)
        def apply(result):
            if sequence == self._listing_sequence and not self.closing:
                self.remote.show_entries(*result)
        def failed(error):
            if sequence == self._listing_sequence:
                self.remote.address.setText(self.remote.path)
                self.show_error(error)
        self.submit("读取远端目录", listing, apply, failed, visible=False)

    def upload_paths(self, paths):
        if not self.connected or not paths:
            return
        if not self.backend.browsable:
            if len(paths) != 1 or not Path(paths[0]).is_file():
                self.show_error("TFTP 每次只支持一个文件，不支持文件夹。")
                return
            target, ok = QInputDialog.getText(self, "TFTP 上传", "远端文件路径（服务端可能覆盖同名文件）",
                text=posixpath.join(self.remote.address.text(), Path(paths[0]).name))
            if not ok or not target:
                return
            destinations = [target]
        else:
            try:
                destinations = [self.backend.join(self.remote.path, Path(path).name) for path in paths]
            except ValueError as exc:
                self.show_error(exc)
                return
        conflict = self.conflict.currentData()
        for path, target in zip(paths, destinations):
            def work(cancel, progress, source=Path(path), destination=target):
                engine = TransferEngine(self.backend, cancel, progress, conflict)
                engine.upload(source, destination)
                return engine.result
            self.submit(f"上传 · {Path(path).name} → {target}", work,
                        lambda result: self.browse_remote(self.remote.path))

    def download_selected(self):
        if not self.connected:
            return
        if not self.backend.browsable:
            source, ok = QInputDialog.getText(self, "TFTP 下载", "远端文件路径", text=self.remote.address.text().rstrip("/") + "/")
            if not ok or not source:
                return
            sources = [(source, posixpath.basename(source), False)]
        else:
            sources = [(entry.path, entry.name, entry.is_dir) for entry in self.remote.tree.selected_entries()]
        if not sources:
            return
        conflict = self.conflict.currentData()
        for source, name, is_dir in sources:
            try:
                target = local_child(Path(self.local.path), name)
            except ValueError as exc:
                self.show_error(exc)
                return
            def work(cancel, progress, source=source, target=target):
                engine = TransferEngine(self.backend, cancel, progress, conflict)
                engine.download(source, target)
                return engine.result
            self.submit(f"下载 · {name} → {target.parent}", work,
                        lambda result, source=source, target=target, is_dir=is_dir:
                        self.record_download(result, source, target, is_dir))

    def open_local(self, path):
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            self.show_error("系统未能打开文件，请设置此文件类型的默认应用。")

    def edit_remote(self, entry):
        if entry.is_dir or entry.is_link:
            self.show_error("请选择普通文件进行编辑。")
            return
        if entry.path in self.edited:
            self.open_local(self.edited[entry.path])
            return
        target = Path(tempfile.mkdtemp(dir=self.edit_temp.name)) / safe_name(entry.name)
        def work(cancel, progress):
            engine = TransferEngine(self.backend, cancel, progress)
            engine.download(entry.path, target)
            return engine.result
        def ready(result):
            if self.closing:
                return
            self.edited[entry.path] = target
            self.edit_button.setEnabled(True)
            self.open_local(target)
            self.connection_status.setText("编辑后请在编辑器中保存，再点击「回传已编辑文件」")
        self.submit("下载待编辑文件 · " + entry.name, work, ready)

    def upload_edited(self):
        if not self.connected or not self.edited:
            return
        source, ok = QInputDialog.getItem(self, "回传编辑", "选择已在本地保存的文件", list(self.edited), 0, False)
        if not ok:
            return
        path = self.edited[source]
        def work(cancel, progress):
            engine = TransferEngine(self.backend, cancel, progress, "overwrite")
            engine.upload(path, source)
            return engine.result
        self.submit("回传编辑 · " + source, work, lambda result: self.browse_remote(self.remote.path))

    def edit_remote_direct(self, entry):
        if not self.connected or not self.backend.browsable:
            return
        if entry.is_dir or entry.is_link:
            self.show_error("请选择普通文本文件进行编辑。")
            return
        if entry.path in self.remote_editors:
            editor = self.remote_editors[entry.path]
            editor.showNormal()
            editor.raise_()
            editor.activateWindow()
            return
        if entry.path in self.loading_editors:
            return
        self.loading_editors.add(entry.path)
        directory = Path(tempfile.mkdtemp(dir=self.edit_temp.name))
        snapshot = directory / "snapshot"
        current = directory / "current"
        upload = directory / "upload"

        def read_remote(cancel, progress, destination):
            info = self.backend.stat(entry.path)
            if not info or info.is_dir or info.is_link:
                raise ValueError("远端文件不存在或已不再是普通文件。")
            if info.size > MAX_EDIT_BYTES:
                raise ValueError("内置编辑器支持不超过 8 MB 的 UTF-8 文本文件。")
            def bounded_progress(done, total):
                if done > MAX_EDIT_BYTES:
                    raise ValueError("文件超过 8 MB 编辑限制。")
                progress(done, total)
            engine = TransferEngine(self.backend, cancel, bounded_progress, "overwrite")
            engine.download(entry.path, destination)
            return destination.read_bytes()

        def loaded(data):
            self.loading_editors.discard(entry.path)
            if self.closing:
                return

            def save(data, finished):
                def work(cancel, progress):
                    latest = read_remote(cancel, progress, current)
                    if latest != snapshot.read_bytes():
                        raise ValueError("远端内容已被其他操作修改，请复制当前修改并重新打开文件后合并。")
                    upload.write_bytes(data)
                    engine = TransferEngine(self.backend, cancel, progress, "overwrite")
                    engine.upload(upload, entry.path)
                    snapshot.write_bytes(data)
                def saved(result):
                    finished()
                    self.browse_remote(self.remote.path)
                self.submit("保存远端文件 · " + entry.path, work, saved, finished)
            try:
                editor = RemoteEditor(entry.path, data, save, self)
            except ValueError as exc:
                self.show_error(exc)
                return
            self.remote_editors[entry.path] = editor
            def forget_editor(_object=None, owner=weakref.ref(self), path=entry.path):
                tab = owner()
                if tab is not None:
                    tab.remote_editors.pop(path, None)
            editor.destroyed.connect(forget_editor)
            editor.show()

        def failed(error):
            self.loading_editors.discard(entry.path)
            self.show_error(error)
        self.submit("打开远端编辑 · " + entry.path,
                    lambda cancel, progress: read_remote(cancel, progress, snapshot), loaded, failed)

    def context_menu(self, pane, point):
        entries = pane.tree.selected_entries()
        if not pane.local and (not self.connected or not self.backend.browsable):
            return
        menu = QMenu(self)
        if entries:
            transfer = menu.addAction("上传到远端" if pane.local else "下载到本地")
            transfer.setEnabled(self.connected)
            transfer.triggered.connect(lambda: self.upload_paths([e.path for e in entries]) if pane.local else self.download_selected())
            if len(entries) == 1 and not entries[0].is_dir:
                if not pane.local:
                    edit = menu.addAction("编辑", lambda: self.edit_remote_direct(entries[0]))
                    edit.setEnabled(not entries[0].is_link)
                menu.addAction("编辑 / 使用默认应用打开" if pane.local else "编辑（下载后打开）",
                    lambda: self.open_local(entries[0].path) if pane.local else self.edit_remote(entries[0]))
            menu.addSeparator()
            menu.addAction("删除…", lambda: self.delete_entries(pane, entries))
            menu.addSeparator()
        menu.addAction("新建文件夹…", lambda: self.make_directory(pane))
        menu.addAction("刷新", pane.refresh.emit)
        menu.exec_(pane.tree.viewport().mapToGlobal(point))

    def delete_entries(self, pane, entries):
        names = "\n".join(e.name for e in entries[:6])
        if QMessageBox.warning(self, "永久删除", f"永久删除选中的 {len(entries)} 项及文件夹内容？\n{names}\n\n此操作不经过回收站。",
                               QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        def work(cancel, progress):
            for entry in entries:
                if cancel.is_set():
                    raise Cancelled()
                if pane.local:
                    if entry.is_dir and not Path(entry.path).is_symlink():
                        shutil.rmtree(entry.path)
                    else:
                        Path(entry.path).unlink()
                else:
                    delete_remote(self.backend, entry, cancel)
            return f"已删除 {len(entries)} 项"
        self.submit("删除 · " + ", ".join(e.name for e in entries), work,
                    lambda result: pane.refresh.emit())

    def make_directory(self, pane):
        name, ok = QInputDialog.getText(self, "新建文件夹", "文件夹名称")
        if not ok:
            return
        try:
            safe_name(name)
            target = str(local_child(Path(pane.path), name)) if pane.local else self.backend.join(pane.path, name)
        except ValueError as exc:
            self.show_error(exc)
            return
        def work(cancel, progress):
            if pane.local:
                Path(target).mkdir()
            else:
                self.backend.mkdir(target)
        self.submit("新建文件夹 · " + name, work, lambda result: pane.refresh.emit())

    def shutdown(self):
        self.closing = True
        for editor in list(self.remote_editors.values()):
            editor.force_close = True
            editor.close()
        # Queued Qt signals may arrive while closing. Keep this widget alive until
        # all workers finish, and suppress UI callbacks that could submit new work.
        for job in self.queue.jobs.values():
            job.callback = job.failure = None
        backend, temporary = self.backend, self.edit_temp
        def cleanup():
            try:
                if backend:
                    backend.close()
            finally:
                temporary.cleanup()
        self.queue.shutdown(cleanup)
