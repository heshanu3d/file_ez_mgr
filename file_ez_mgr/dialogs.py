from __future__ import annotations

from dataclasses import replace

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QAbstractItemView, QHeaderView, QTreeWidget, QTreeWidgetItem, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                             QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                             QLineEdit, QMessageBox, QPushButton, QSpinBox,
                             QVBoxLayout, QWidget)

from .config import Profile
from .history import DownloadHistory


class ConnectionDialog(QDialog):
    def __init__(self, profile=None, parent=None, *, config_dir=None):
        super().__init__(parent)
        self.profile = replace(profile) if profile else Profile()
        self.setWindowTitle("连接设置")
        self.setMinimumWidth(540)
        layout = QVBoxLayout(self)
        title = QLabel("连接设置")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        form = QFormLayout()
        form.setSpacing(14)
        self.name = QLineEdit(self.profile.name)
        self.protocol = QComboBox()
        for label, value in [("SSH / SFTP", "sftp"), ("FTP", "ftp"),
                             ("FTPS · 显式 TLS", "ftps"), ("TFTP", "tftp")]:
            self.protocol.addItem(label, value)
        self.protocol.setCurrentIndex(self.protocol.findData(self.profile.protocol))
        self.host = QLineEdit(self.profile.host)
        self.host.setPlaceholderText("192.168.1.10 或 example.com")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(self.profile.port)
        self.username = QLineEdit(self.profile.username)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("仅用于本次连接，不写入配置；也可填写私钥口令")
        self.key_file = QLineEdit(self.profile.key_file)
        self.local_dir = QLineEdit(self.profile.local_dir)
        self.remote_dir = QLineEdit(self.profile.remote_dir)
        self.passive = QCheckBox("被动模式（推荐）")
        self.passive.setChecked(self.profile.passive)
        for label, widget in [("连接名称", self.name), ("连接方式", self.protocol),
                              ("服务器", self.host), ("端口", self.port),
                              ("用户名", self.username), ("密码 / 私钥口令", self.password)]:
            form.addRow(label, widget)
        form.addRow("SSH 私钥", self.browse_field(self.key_file, False))
        form.addRow("默认本地目录", self.browse_field(self.local_dir, True))
        form.addRow("默认远端目录", self.remote_dir)
        form.addRow("FTP 选项", self.passive)
        layout.addLayout(form)
        self.hint = QLabel()
        self.hint.setObjectName("muted")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self.history_button = QPushButton("编辑下载历史…")
        self.history_button.setEnabled(profile is not None and config_dir is not None)
        self.history_button.clicked.connect(
            lambda: DownloadHistoryDialog(DownloadHistory(config_dir, profile), self).exec_())
        layout.addWidget(self.history_button)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("保存配置")
        buttons.button(QDialogButtonBox.Save).setObjectName("primary")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.protocol.currentIndexChanged.connect(lambda: self.update_protocol(True))
        self.update_protocol(False)

    def browse_field(self, field, directory):
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(field)
        button = QPushButton("选择…")
        def browse():
            value = (QFileDialog.getExistingDirectory(self, "选择默认目录", field.text()) if directory
                     else QFileDialog.getOpenFileName(self, "选择 SSH 私钥", field.text())[0])
            if value:
                field.setText(value)
        button.clicked.connect(browse)
        row.addWidget(button)
        return widget

    def update_protocol(self, reset_port):
        protocol = self.protocol.currentData()
        if reset_port:
            self.port.setValue({"sftp": 22, "ftp": 21, "ftps": 21, "tftp": 69}[protocol])
        self.key_file.setEnabled(protocol == "sftp")
        self.passive.setEnabled(protocol in {"ftp", "ftps"})
        self.username.setEnabled(protocol != "tftp")
        self.password.setEnabled(protocol != "tftp")
        self.hint.setText({
            "sftp": "通过 SSH 的 SFTP 子系统传输文件，支持密码、私钥和 SSH Agent。首次连接需核对主机指纹。",
            "ftp": "FTP 使用明文传输；支持 MLSD 的服务器可浏览目录。",
            "ftps": "使用显式 TLS，验证服务器证书，并加密数据连接。",
            "tftp": "TFTP 不提供目录列表、删除、认证或文件夹传输。请使用指定路径的单文件上传 / 下载。"
        }[protocol])

    def accept(self):
        if not self.name.text().strip() or not self.host.text().strip():
            QMessageBox.warning(self, "请完善配置", "连接名称和服务器不能为空。")
            return
        self.profile = replace(self.profile, name=self.name.text().strip(),
                               protocol=self.protocol.currentData(), host=self.host.text().strip(),
                               port=self.port.value(), username=self.username.text().strip(),
                               key_file=self.key_file.text().strip(), local_dir=self.local_dir.text().strip(),
                               remote_dir=self.remote_dir.text().strip() or ".", passive=self.passive.isChecked())
        super().accept()


class DownloadHistoryDialog(QDialog):
    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("编辑下载历史")
        self.resize(820, 440)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("选择要删除的目录记录（可多选）。删除立即生效，不会删除实际文件。"))
        self.records = QTreeWidget()
        self.records.setHeaderLabels(["本地目录", "远端目录", "最近下载时间"])
        self.records.setRootIsDecorated(False)
        self.records.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.records.header().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.records)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.delete_button = QPushButton("删除选中记录")
        self.delete_button.clicked.connect(self.delete_selected)
        self.records.itemSelectionChanged.connect(
            lambda: self.delete_button.setEnabled(bool(self.records.selectedItems())))
        layout.addWidget(self.delete_button)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.refresh()

    def refresh(self):
        self.records.clear()
        self.delete_button.setEnabled(False)
        try:
            items = self.store.load()
        except Exception as exc:
            self.status.setText(f"读取失败：{exc}")
            return
        for item in items:
            row = QTreeWidgetItem([item["local_dir"], item["remote_dir"], item["time"]])
            row.setData(0, Qt.UserRole, (item["local_dir"], item["remote_dir"]))
            for column in range(3):
                row.setToolTip(column, row.text(column))
            self.records.addTopLevelItem(row)
        self.status.setText(f"共 {len(items)} 条目录记录" if items else "暂无下载历史")

    def delete_selected(self):
        pairs = [tuple(row.data(0, Qt.UserRole)) for row in self.records.selectedItems()]
        if not pairs:
            return
        try:
            self.store.delete(pairs)
        except Exception as exc:
            self.status.setText(f"删除失败：{exc}")
            return
        self.refresh()
