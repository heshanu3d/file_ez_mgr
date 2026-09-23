from __future__ import annotations

import sys
from pathlib import Path

from PyQt5.QtCore import QEvent, QStandardPaths, Qt, QTimer
from PyQt5.QtGui import QFont, QKeySequence
from PyQt5.QtWidgets import (QApplication, QComboBox, QDialog, QHBoxLayout,
                             QInputDialog, QLabel, QLineEdit, QMainWindow,
                             QMessageBox, QPushButton, QShortcut, QTabWidget,
                             QVBoxLayout, QWidget)

from .config import ConfigStore
from .branding import application_icon
from .dialogs import ConnectionDialog
from .session import SessionTab
from .workspace_state import WorkspaceStateStore

STYLE = """
QWidget { color: #20304a; font-size: 13px; }
QMainWindow, QDialog { background: #f3f5f9; }
QLabel { background: transparent; }
QLabel#brand { font-size: 27px; font-weight: 700; color: #16233b; }
QLabel#pageTitle { font-size: 22px; font-weight: 600; margin-bottom: 15px; }
QLabel#paneTitle { font-size: 15px; font-weight: 600; }
QLabel#muted { color: #768298; font-size: 12px; }
QLabel#badge { color: #4b67cb; background: #edf1ff; border-radius: 5px; padding: 4px 8px; font-size: 10px; font-weight: 600; }
QFrame#filePane { background: white; border: 1px solid #e2e7f0; border-radius: 10px; }
QPushButton { background: white; border: 1px solid #dce2ed; border-radius: 6px; padding: 7px 12px; }
QPushButton:hover { background: #edf2ff; border-color: #a6b8f6; }
QPushButton:pressed { background: #dce5ff; }
QPushButton:disabled { color: #aab3c2; background: #f4f6fa; }
QPushButton#primary { background: #4969df; border-color: #4969df; color: white; font-weight: 600; }
QPushButton#primary:hover { background: #3857c9; }
QPushButton#primary:disabled { background: #aebce9; border-color: #aebce9; }
QLineEdit, QSpinBox, QComboBox { background: #fff; border: 1px solid #dfe5ef; border-radius: 6px; padding: 7px; selection-background-color: #4969df; }
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border-color: #6f88e7; }
QLineEdit:disabled { background: #f4f6fa; color: #aab3c2; }
QComboBox { min-width: 75px; }
QTreeWidget { border: none; background: white; alternate-background-color: #fafbfe; outline: none; }
QTreeWidget::item { height: 32px; padding: 0 3px; border: none; }
QTreeWidget::item:selected { background: #e7edff; color: #274cbb; }
QTreeWidget::item:hover:!selected { background: #f0f4fc; }
QHeaderView::section { background: #f7f9fc; border: none; border-bottom: 1px solid #e7ecf4; padding: 9px 5px; color: #768298; font-size: 11px; }
QTabWidget::pane { border: none; }
QTabBar::tab { background: transparent; border-bottom: 2px solid transparent; padding: 12px 18px; color: #78859a; }
QTabBar::tab:selected { color: #3d5fcf; border-bottom: 2px solid #4969df; font-weight: 600; }
QTabBar::tab:hover { background: #eaf0fc; }
QSplitter::handle { background: #f3f5f9; width: 10px; height: 10px; }
QMenu { background: white; border: 1px solid #e0e5ee; padding: 5px; }
QMenu::item { padding: 8px 24px; }
QMenu::item:selected { background: #e7edff; }
QStatusBar { background: transparent; color: #8893a6; }
"""


class MainWindow(QMainWindow):
    def __init__(self, config_dir=None):
        super().__init__()
        self.setWindowTitle("File EZ · 文件传输")
        self.resize(1280, 850)
        self.setMinimumSize(980, 650)
        directory = Path(config_dir) if config_dir else Path(QStandardPaths.writableLocation(QStandardPaths.AppConfigLocation))
        self.store = ConfigStore(directory)
        self.workspace_store = WorkspaceStateStore(directory)
        self.workspace_error = None
        self._geometry_save = QTimer(self)
        self._geometry_save.setSingleShot(True)
        self._geometry_save.setInterval(400)
        self._geometry_save.timeout.connect(self.save_workspace)
        self._restored_maximized = False
        self._closed = False
        self.passwords = {}
        self.retired_tabs = []
        self.config_error = None
        try:
            self.profiles = self.store.load()
        except Exception as exc:
            self.profiles = []
            self.config_error = exc
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(26, 22, 26, 12)
        layout.setSpacing(16)
        heading = QHBoxLayout()
        title = QLabel("File EZ")
        title.setObjectName("brand")
        heading.addWidget(title)
        subtitle = QLabel("文件传输，简单一点。")
        subtitle.setObjectName("muted")
        heading.addWidget(subtitle)
        heading.addStretch()
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(210)
        heading.addWidget(self.profile_combo)
        for text, callback, primary in [("连接", self.open_connection, True),
                                        ("新建连接", self.new_profile, False),
                                        ("设置", self.edit_profile, False),
                                        ("删除配置", self.delete_profile, False)]:
            button = QPushButton(text)
            if primary:
                button.setObjectName("primary")
            button.clicked.connect(callback)
            heading.addWidget(button)
        layout.addLayout(heading)
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        layout.addWidget(self.tabs, 1)
        self.setCentralWidget(body)
        self.statusBar().showMessage("SFTP / FTP / FTPS / TFTP    ·    双击文件夹进入    ·    点击列标题排序    ·    Ctrl+T 新建连接")
        self.refresh_profiles()
        self.restore_workspace()
        QShortcut(QKeySequence("Ctrl+T"), self, activated=self.new_profile)
        QShortcut(QKeySequence.Refresh, self, activated=self.refresh_current)
        if self.config_error:
            QTimer.singleShot(0, lambda: QMessageBox.warning(self, "配置读取失败",
                f"{self.store.path}\n{self.config_error}\n\n原文件已保留。请修复配置并重启，当前禁止覆盖保存。"))

    def refresh_current(self):
        tab = self.tabs.currentWidget()
        if tab:
            tab.local.refresh.emit()
            tab.remote.refresh.emit()

    def refresh_profiles(self, selected=None):
        self.profile_combo.clear()
        for profile in self.profiles:
            self.profile_combo.addItem(f"{profile.name} · {profile.protocol.upper()}", profile.id)
        if selected:
            self.profile_combo.setCurrentIndex(self.profile_combo.findData(selected))
        if not self.profiles:
            self.profile_combo.addItem("尚未保存连接", None)

    def selected_profile(self):
        selected = self.profile_combo.currentData()
        return next((p for p in self.profiles if p.id == selected), None)

    def persist(self, profiles):
        if self.config_error:
            QMessageBox.warning(self, "配置未保存", "请先修复配置文件并重启，避免覆盖原始配置。")
            return False
        try:
            self.store.save(profiles)
            self.profiles = profiles
            return True
        except Exception as exc:
            QMessageBox.warning(self, "配置未保存", str(exc))
            return False

    def new_profile(self):
        self.configure(None)

    def edit_profile(self):
        self.configure(self.selected_profile())

    def configure(self, profile):
        dialog = ConnectionDialog(profile, self, config_dir=self.store.directory)
        result = dialog.exec_()
        for index in range(self.tabs.count()):
            self.tabs.widget(index).refresh_history()
        if result != QDialog.Accepted:
            return
        new_profile = dialog.profile
        profiles = [new_profile if p.id == new_profile.id else p for p in self.profiles]
        if not profile:
            profiles.append(new_profile)
        if self.persist(profiles):
            if dialog.password.text():
                self.passwords[new_profile.id] = dialog.password.text()
            else:
                self.passwords.pop(new_profile.id, None)
            self.refresh_profiles(new_profile.id)
            self.statusBar().showMessage(f"配置已保存 · {self.store.path} · 点击「连接」打开新标签页")

    def delete_profile(self):
        profile = self.selected_profile()
        if profile and QMessageBox.question(self, "删除配置", f"删除「{profile.name}」的已保存配置？",
                                             QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
            if self.persist([p for p in self.profiles if p.id != profile.id]):
                self.passwords.pop(profile.id, None)
                self.refresh_profiles()

    def add_local_tab(self):
        self.tabs.addTab(SessionTab(config_dir=self.store.directory, parent=self), "本地浏览")

    def restore_workspace(self):
        try:
            state = self.workspace_store.load()
        except Exception as exc:
            self.workspace_error = exc
            self.statusBar().showMessage(f"无法恢复上次窗口，原记录已保留：{exc}")
            state = None
        if state and not self.config_error:
            geometry = state.get("geometry")
            if geometry:
                self.resize(geometry["width"], geometry["height"])
                self._restored_maximized = geometry["maximized"]
                if self._restored_maximized:
                    self.setWindowState(self.windowState() | Qt.WindowMaximized)
            profiles = {profile.id: profile for profile in self.profiles}
            restored = {}
            for old_index, item in enumerate(state["tabs"]):
                profile = profiles.get(item["profile_id"]) if item.get("profile_id") else None
                # Deleted configurations must not be revived from window state.
                if item.get("profile_id") and profile is None:
                    continue
                auto_connect = bool(profile and (profile.protocol == "tftp" or
                                    (profile.protocol == "sftp" and profile.key_file.strip())))
                tab = SessionTab(profile=profile, config_dir=self.store.directory, parent=self,
                                 auto_connect=auto_connect, local_dir=item["local_dir"],
                                 remote_dir=item["remote_dir"])
                tab.conflict.setCurrentIndex(tab.conflict.findData(item.get("conflict", "overwrite")))
                index = self.tabs.addTab(tab, profile.name if profile else "本地浏览")
                restored[old_index] = index
                if profile:
                    self.tabs.setTabToolTip(index, f"{profile.protocol}://{profile.host}:{profile.port}")
            if restored:
                self.tabs.setCurrentIndex(restored.get(state["active_index"], 0))
            if state.get("selected_profile") in profiles:
                self.refresh_profiles(state["selected_profile"])
        if not self.tabs.count():
            self.add_local_tab()

    def save_workspace(self):
        if self.workspace_error or self.config_error:
            return
        tabs = []
        for index in range(self.tabs.count()):
            tab = self.tabs.widget(index)
            tabs.append(dict(profile_id=tab.profile.id if tab.profile else None,
                             local_dir=tab.local.path,
                             remote_dir=tab.remote.path or (tab.profile.remote_dir if tab.profile else ""),
                             conflict=tab.conflict.currentData()))
        bounds = self.normalGeometry() if self.isMaximized() else self.geometry()
        geometry = dict(width=max(self.minimumWidth(), bounds.width()),
                        height=max(self.minimumHeight(), bounds.height()),
                        maximized=self.isMaximized())
        self.workspace_store.save(tabs, self.tabs.currentIndex(),
                                  self.profile_combo.currentData(), geometry)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.isVisible() and not self._closed:
            self._geometry_save.start()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange and self.isVisible() and not self._closed:
            self._geometry_save.start()

    def open_connection(self):
        profile = self.selected_profile()
        if not profile:
            self.new_profile()
            return
        password = self.passwords.get(profile.id, "")
        uses_private_key = profile.protocol == "sftp" and bool(profile.key_file.strip())
        if not password and profile.protocol != "tftp" and not uses_private_key:
            password, ok = QInputDialog.getText(self, "连接 " + profile.name,
                "密码 / 私钥口令（可留空使用私钥或 SSH Agent）", QLineEdit.Password)
            if not ok:
                return
        tab = SessionTab(profile, password, self.store.directory, self)
        index = self.tabs.addTab(tab, profile.name)
        self.tabs.setCurrentIndex(index)
        self.tabs.setTabToolTip(index, f"{profile.protocol}://{profile.host}:{profile.port}")

    def close_tab(self, index):
        tab = self.tabs.widget(index)
        if not self.confirm_close([tab]):
            return
        tab.shutdown()
        self.tabs.removeTab(index)
        self.retired_tabs.append(tab)
        if not self.tabs.count():
            self.add_local_tab()

    def confirm_close(self, tabs):
        for tab in tabs:
            for editor in list(tab.remote_editors.values()):
                if not editor.can_close():
                    return False
        active = any(tab.queue.jobs for tab in tabs)
        edited = any(tab.edited for tab in tabs)
        if active or edited:
            message = ("关闭将取消仍在进行或等待的任务。\n" if active else "")
            message += ("远端编辑的本地临时副本将被清理，请确认修改已回传。\n" if edited else "")
            return QMessageBox.question(self, "关闭连接", message + "确定关闭？",
                                        QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes
        return True

    def closeEvent(self, event):
        tabs = [self.tabs.widget(i) for i in range(self.tabs.count())]
        if not self.confirm_close(tabs):
            event.ignore()
            return
        try:
            self.save_workspace()
        except Exception as exc:
            QMessageBox.warning(self, "窗口记录保存失败", f"无法记录本次窗口状态：{exc}")
        self._geometry_save.stop()
        self._closed = True
        for tab in tabs:
            tab.shutdown()
        event.accept()

    def save_workspace_on_quit(self):
        # Qt can exit its event loop without delivering a window close event.
        if self._closed:
            return
        try:
            self.save_workspace()
        except Exception as exc:
            # The event loop is shutting down; do not open a modal dialog here.
            print(f"窗口记录保存失败：{exc}", file=sys.stderr)


def main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setApplicationName("FileEZ")
    app.setOrganizationName("FileEZ")
    app.setWindowIcon(application_icon())
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    window = MainWindow()
    app.aboutToQuit.connect(window.save_workspace_on_quit)
    window.show()
    sys.exit(app.exec_())
