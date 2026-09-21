from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import QMimeData, Qt, QUrl, pyqtSignal
from PyQt5.QtGui import QDrag
from PyQt5.QtWidgets import (QAbstractItemView, QComboBox, QFrame, QHBoxLayout,
                             QHeaderView, QLabel, QLineEdit, QPushButton,
                             QStyle, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from .backends import Entry


def size_text(size):
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


class HistoryComboBox(QComboBox):
    opening = pyqtSignal()

    def showPopup(self):
        self.opening.emit()
        super().showPopup()


class FileItem(QTreeWidgetItem):
    def __init__(self, entry: Entry, parent_entry=False):
        self.entry, self.parent_entry = entry, parent_entry
        super().__init__([entry.name, "上一级" if parent_entry else entry.kind,
                          entry.extension, "—" if entry.is_dir else size_text(entry.size),
                          datetime.fromtimestamp(entry.modified).strftime("%Y-%m-%d %H:%M")
                          if entry.modified else "—"])
        self.setToolTip(0, entry.path)
        self.setTextAlignment(3, Qt.AlignRight | Qt.AlignVCenter)

    def __lt__(self, other):
        tree = self.treeWidget()
        descending = tree.header().sortIndicatorOrder() == Qt.DescendingOrder
        if self.parent_entry != other.parent_entry:
            return not self.parent_entry if descending else self.parent_entry
        column = tree.sortColumn()
        a, b = self.entry, other.entry
        keys_a = (a.name.casefold(), (not a.is_dir, a.is_link), a.extension.casefold(), a.size, a.modified)
        keys_b = (b.name.casefold(), (not b.is_dir, b.is_link), b.extension.casefold(), b.size, b.modified)
        return (keys_a[column], a.name.casefold()) < (keys_b[column], b.name.casefold())


class FileTree(QTreeWidget):
    filesDropped = pyqtSignal(list)
    remoteDragRequested = pyqtSignal(list)

    def __init__(self, local=False, parent=None):
        super().__init__(parent)
        self.local = local
        self.setHeaderLabels(["名称", "类别", "类型", "大小", "修改时间"])
        self.setRootIsDecorated(False)
        self.setAlternatingRowColors(True)
        self.setUniformRowHeights(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSortingEnabled(True)
        self.sortByColumn(4, Qt.DescendingOrder)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.setDragEnabled(True)
        self.setAcceptDrops(not local)
        self.setDropIndicatorShown(not local)
        self.setDragDropMode(QAbstractItemView.DragOnly if local else QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.CopyAction)
        self.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.header().setMinimumSectionSize(50)
        for column, width in [(1, 78), (2, 80), (3, 88), (4, 145)]:
            self.setColumnWidth(column, width)

    def selected_entries(self):
        return [item.entry for item in self.selectedItems() if not item.parent_entry]

    def selection_mime(self):
        mime = QMimeData()
        if self.local:
            mime.setUrls([QUrl.fromLocalFile(os.path.abspath(e.path)) for e in self.selected_entries()])
        return mime

    def startDrag(self, supported_actions):
        entries = self.selected_entries()
        if not entries:
            return
        if not self.local:
            self.remoteDragRequested.emit(entries)
            return
        self.drag_paths([entry.path for entry in entries])

    def drag_paths(self, paths):
        if not paths:
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(os.path.abspath(path)) for path in paths])
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(self.style().standardIcon(QStyle.SP_FileIcon).pixmap(32, 32))
        # Copy-only prevents an external application's move action deleting sources.
        return drag.exec_(Qt.CopyAction, Qt.CopyAction)

    def _can_drop(self, event):
        return (not self.local and event.source() is not self and event.mimeData().hasUrls()
                and all(url.isLocalFile() for url in event.mimeData().urls()))

    def dragEnterEvent(self, event):
        if self._can_drop(event):
            event.setDropAction(Qt.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        if self._can_drop(event):
            self.filesDropped.emit([url.toLocalFile() for url in event.mimeData().urls()])
            event.setDropAction(Qt.CopyAction)
            event.accept()
        else:
            event.ignore()


class FilePane(QFrame):
    navigate = pyqtSignal(str)
    refresh = pyqtSignal()
    activated = pyqtSignal(object)

    def __init__(self, local=False, parent=None):
        super().__init__(parent)
        self.local, self.path, self.entries = local, "", []
        self.setObjectName("filePane")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        heading = QHBoxLayout()
        title = QLabel("本地文件" if local else "远端文件")
        title.setObjectName("paneTitle")
        heading.addWidget(title)
        heading.addStretch()
        self.badge = QLabel("LOCAL" if local else "未连接")
        self.badge.setObjectName("badge")
        heading.addWidget(self.badge)
        layout.addLayout(heading)
        nav = QHBoxLayout()
        up = QPushButton("↑")
        up.setFixedWidth(32)
        up.setToolTip("返回上一级目录")
        up.clicked.connect(self.go_up)
        nav.addWidget(up)
        self.address = QLineEdit()
        self.address.setPlaceholderText("输入目录路径，按 Enter 跳转")
        self.address.returnPressed.connect(lambda: self.navigate.emit(self.address.text()))
        nav.addWidget(self.address)
        reload_button = QPushButton("刷新")
        reload_button.clicked.connect(self.refresh)
        nav.addWidget(reload_button)
        layout.addLayout(nav)
        self.search = QLineEdit()
        self.search.setPlaceholderText("筛选当前目录中的文件或文件夹…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.filter_entries)
        layout.addWidget(self.search)
        self.note = QLabel()
        self.note.setObjectName("muted")
        self.note.setWordWrap(True)
        self.note.hide()
        layout.addWidget(self.note)
        self.tree = FileTree(local)
        self.tree.itemDoubleClicked.connect(self._activate)
        layout.addWidget(self.tree, 1)
        self.footer = QLabel("从本地列表拖拽文件到微信、飞书等应用" if local else "连接后浏览远端文件")
        self.footer.setObjectName("muted")
        layout.addWidget(self.footer)

    def go_up(self):
        import posixpath
        parent = str(Path(self.path).parent) if self.local else posixpath.dirname(self.path.rstrip("/")) or "/"
        if self.path:
            self.navigate.emit(parent)

    def _activate(self, item):
        if item.parent_entry or item.entry.is_dir:
            self.navigate.emit(item.entry.path)
        else:
            self.activated.emit(item.entry)

    def show_entries(self, path, entries):
        import posixpath
        self.path, self.entries = path, entries
        self.address.setText(path)
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        parent = str(Path(path).parent) if self.local else posixpath.dirname(path.rstrip("/")) or "/"
        items = [FileItem(Entry("..", parent, True), True)]
        items.extend(FileItem(entry) for entry in entries)
        for item in items:
            item.setIcon(0, self.style().standardIcon(
                QStyle.SP_DirIcon if item.entry.is_dir else QStyle.SP_FileIcon))
        self.tree.addTopLevelItems(items)
        self.tree.setSortingEnabled(True)
        self.filter_entries()

    def filter_entries(self):
        query = self.search.text().casefold()
        visible = 0
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            hidden = not item.parent_entry and query not in item.entry.name.casefold()
            item.setHidden(hidden)
            if not hidden and not item.parent_entry:
                visible += 1
        self.footer.setText(f"{visible} / {len(self.entries)} 项" +
                            ("  ·  支持拖拽到其他应用" if self.local else "  ·  普通文件可拖出，先下载后交付"))
