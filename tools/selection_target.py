"""Minimal external app used to verify native mouse-selection capture."""
import argparse
import sys

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication, QTextEdit


TITLE = "WordBook Selection Probe Target"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", required=True)
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    editor = QTextEdit()
    editor.setWindowTitle(TITLE)
    editor.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
    editor.setReadOnly(True)
    editor.document().setDocumentMargin(12)
    editor.setStyleSheet("font-size:18px;background:white;color:black;")
    editor.setPlainText(args.text)
    editor.move(100, 100)
    editor.resize(620, 190)
    editor.show()
    editor.raise_()
    editor.activateWindow()

    def select_all_again():
        editor.setFocus()
        editor.selectAll()
        editor.activateWindow()

    QTimer.singleShot(350, select_all_again)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
