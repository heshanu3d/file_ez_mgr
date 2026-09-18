"""In-app remote text editor. Saving writes back through the session queue."""
from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFontDatabase, QKeySequence
from PyQt5.QtWidgets import (QDialog, QHBoxLayout, QLabel, QMessageBox,
                             QPlainTextEdit, QPushButton, QShortcut, QVBoxLayout)

MAX_EDIT_BYTES = 8 * 1024 * 1024


class RemoteEditor(QDialog):
    def __init__(self, path, data, save_callback, parent=None):
        super().__init__(parent)
        if len(data) > MAX_EDIT_BYTES:
            raise ValueError("内置编辑器支持不超过 8 MB 的 UTF-8 文本文件。")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("此文件不是 UTF-8 文本，请使用「编辑（下载后打开）」选择合适的应用。") from exc
        if "\x00" in text:
            raise ValueError("此文件包含二进制内容，不能使用内置文本编辑器。")
        self.path = path
        self.encoding = "utf-8-sig" if data.startswith(b"\xef\xbb\xbf") else "utf-8"
        self.newline = "\r\n" if "\r\n" in text and "\n" not in text.replace("\r\n", "") else "\n"
        self.save_callback = save_callback
        self.saving = False
        self.force_close = False
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowTitle("编辑 · " + path)
        self.resize(860, 620)
        layout = QVBoxLayout(self)
        label = QLabel(path)
        label.setTextFormat(Qt.PlainText)
        label.setWordWrap(True)
        layout.addWidget(label)
        self.text = QPlainTextEdit()
        self.text.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.text.setPlainText(text)
        self.text.document().setModified(False)
        layout.addWidget(self.text, 1)
        bar = QHBoxLayout()
        self.status = QLabel("保存后直接更新远端文件")
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        bar.addWidget(self.status, 1)
        self.save_button = QPushButton("保存到远端")
        self.save_button.setObjectName("primary")
        self.save_button.clicked.connect(self.save)
        bar.addWidget(self.save_button)
        close = QPushButton("关闭")
        close.clicked.connect(self.close)
        bar.addWidget(close)
        layout.addLayout(bar)
        QShortcut(QKeySequence.Save, self, activated=self.save)
        self.text.document().modificationChanged.connect(self.update_title)

    def update_title(self, modified):
        self.setWindowTitle(("* " if modified else "") + "编辑 · " + self.path)

    def save(self):
        if self.saving or not self.text.document().isModified():
            return
        data = self.text.toPlainText().replace("\n", self.newline).encode(self.encoding)
        if len(data) > MAX_EDIT_BYTES:
            self.status.setText("保存失败：文本超过 8 MB 限制。")
            return
        self.saving = True
        self.text.setReadOnly(True)
        self.save_button.setEnabled(False)
        self.status.setText("正在保存到远端…")
        try:
            self.save_callback(data, self.finish_save)
        except Exception as exc:
            self.finish_save(exc)

    def finish_save(self, error=None):
        self.saving = False
        self.text.setReadOnly(False)
        self.save_button.setEnabled(True)
        if error is None:
            self.text.document().setModified(False)
            self.status.setText("已保存到远端")
        else:
            self.status.setText("保存未完成：" + (str(error) or type(error).__name__))

    def can_close(self):
        if self.saving:
            QMessageBox.information(self, "正在保存", "请等待远端保存结束，或在任务列表取消保存。")
            return False
        if self.text.document().isModified():
            return QMessageBox.question(self, "尚未保存", f"放弃对 {self.path} 的未保存修改？",
                                        QMessageBox.Discard | QMessageBox.Cancel,
                                        QMessageBox.Cancel) == QMessageBox.Discard
        return True

    def closeEvent(self, event):
        event.accept() if self.force_close or self.can_close() else event.ignore()

    def reject(self):
        self.close()
