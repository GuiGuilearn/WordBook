import sys
from PySide6 import QtCore, QtGui
from PySide6.QtWidgets import QApplication, QMessageBox
from wordbook.ui import MainWindow
from wordbook.platform_win import SingleInstance, data_dir


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("WordBook")
    app.setOrganizationName("WordBook")
    try:
        instance = SingleInstance(data_dir())
    except RuntimeError as exc:
        QMessageBox.warning(None, "WordBook", str(exc))
        return 1
    if not instance.is_primary:
        return 0
    window = MainWindow(app)
    instance.activation_requested.connect(window.show_latest_record)
    app.aboutToQuit.connect(instance.close)
    return app.exec()


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] in ("--smoke-first", "--smoke-reopen"):
        from wordbook.smoke_v2 import run
        raise SystemExit(run("first" if sys.argv[1] == "--smoke-first" else "reopen"))
    raise SystemExit(main())
