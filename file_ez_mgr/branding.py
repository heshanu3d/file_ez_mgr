"""Application branding rendered by Qt, without external image dependencies."""

from PyQt5.QtCore import QRectF, Qt
from PyQt5.QtGui import QColor, QFont, QIcon, QPainter, QPixmap


def application_icon():
    """Create a crisp red-and-white EZ icon for windows and the macOS Dock."""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256, 512, 1024):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.TextAntialiasing)
        painter.scale(size / 256, size / 256)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#E53935"))
        painter.drawRoundedRect(QRectF(12, 12, 232, 232), 50, 50)
        font = QFont("Arial")
        font.setPixelSize(120)
        font.setWeight(QFont.Bold)
        painter.setFont(font)
        painter.setPen(Qt.white)
        # Center the visible letter shapes, rather than the font's line box.
        bounds = painter.fontMetrics().tightBoundingRect("EZ")
        painter.drawText(128 - bounds.center().x(), 128 - bounds.center().y(), "EZ")
        painter.end()
        icon.addPixmap(pixmap)
    return icon
