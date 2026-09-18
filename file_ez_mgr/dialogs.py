from __future__ import annotations

from dataclasses import replace

from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                             QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                             QLineEdit, QMessageBox, QPushButton, QSpinBox,
                             QVBoxLayout, QWidget)

from .config import Profile


class ConnectionDialog(QDialog):
    def __init__(self, profile=None, parent=None):
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
