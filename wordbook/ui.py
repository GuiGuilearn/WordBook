import json
import os
import re
from html import escape
from datetime import date, datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QRect, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QMenu, QMessageBox,
    QAbstractItemView, QPushButton, QSystemTrayIcon, QTextBrowser, QTextEdit, QToolButton, QVBoxLayout, QWidget)

from .api import ApiError, translate
from .platform_win import (Hotkey, MOUSE_SIDE_BUTTONS, MouseSideHook, SelectionError, data_dir,
                           copy_selected_text, get_api_key, load_settings, save_settings,
                           clipboard_sequence, read_clipboard_snapshot,
                           sanitize_settings, set_api_key, set_autostart)
from .store import Store
from .translations import (PHONETIC_FIELDS, TRANSLATION_TYPES, brief_translation,
                           has_word_phonetics, type_name, valid_phonetic)


SHORTCUT_GRACE_MS = 5000
NORMAL_COLLAPSE_MS = 600


def icon():
    image = QPixmap(64, 64)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#2775cc"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(2, 2, 60, 60, 14, 14)
    painter.setPen(QColor("white"))
    font = painter.font()
    font.setBold(True)
    font.setPixelSize(33)
    painter.setFont(font)
    painter.drawText(image.rect(), Qt.AlignmentFlag.AlignCenter, "W")
    painter.end()
    return QIcon(image)


class TranslationWorker(QThread):
    finished_result = Signal(int, object, str)

    def __init__(self, card_id, text, kind, key, model, translation_type="en-zh"):
        super().__init__()
        self.card_id, self.text, self.kind, self.key, self.model = card_id, text, kind, key, model
        self.translation_type = translation_type

    def run(self):
        try:
            result = translate(self.text, self.kind, self.key, self.model, translation_type=self.translation_type)
            self.finished_result.emit(self.card_id, result, "")
        except ApiError as exc:
            self.finished_result.emit(self.card_id, None, str(exc))
        except Exception:
            self.finished_result.emit(self.card_id, None, "翻译时发生错误，原文已保存；请手动重试。")


class SelectionWorker(QThread):
    finished_selection = Signal(str, str)

    def __init__(self, target_hwnd=None):
        super().__init__()
        self.target_hwnd = target_hwnd

    def run(self):
        try:
            text = copy_selected_text(target_hwnd=self.target_hwnd)
            if not text.strip():
                self.finished_selection.emit("", "没有读取到选中的文字，请重新拖选后再按侧键。")
            else:
                self.finished_selection.emit(text, "")
        except SelectionError as exc:
            self.finished_selection.emit("", str(exc))
        except Exception:
            self.finished_selection.emit("", "读取选中文字失败，请改用 Ctrl+Alt+W 或手动粘贴。")


class DropArea(QLabel):
    dropped = Signal(str)
    entered = Signal()
    rejected = Signal()

    def __init__(self, text=""):
        super().__init__(text)
        self.setAcceptDrops(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def dragEnterEvent(self, event):
        if event.mimeData().hasText() and not event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.entered.emit()
        else:
            self.rejected.emit()
            event.ignore()

    def dropEvent(self, event):
        if event.mimeData().hasText() and not event.mimeData().hasUrls():
            self.dropped.emit(event.mimeData().text())
            event.acceptProposedAction()


class InputEdit(QTextEdit):
    dropped = Signal(str)
    rejected = Signal()
    submitted = Signal()
    translation_type_selected = Signal(str)

    def __init__(self):
        super().__init__()
        self.composing = False
        self.setViewportMargins(0, 0, 0, 30)
        self.type_button = QToolButton(self)
        self.type_button.setObjectName("translationType")
        self.type_button.setToolTip("选择翻译方向")
        self.type_menu = QMenu(self)
        for code, label in TRANSLATION_TYPES.items():
            action = self.type_menu.addAction(label)
            action.setCheckable(True)
            action.triggered.connect(lambda _, value=code: self.translation_type_selected.emit(value))
        self.type_button.clicked.connect(self.show_type_menu)
        self.set_translation_type("en-zh")

    def set_translation_type(self, code):
        self.type_button.setText(type_name(code) + "  ⌃")
        self.type_button.adjustSize()
        for action, value in zip(self.type_menu.actions(), TRANSLATION_TYPES):
            action.setChecked(value == code)
        self.position_type_button()

    def position_type_button(self):
        self.type_button.move(max(4, self.width() - self.type_button.width() - 7), max(4, self.height() - self.type_button.height() - 6))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "type_button"):
            self.position_type_button()

    def show_type_menu(self):
        size = self.type_menu.sizeHint()
        self.type_menu.popup(self.type_button.mapToGlobal(QPoint(0, -size.height())))

    def inputMethodEvent(self, event):
        self.composing = bool(event.preeditString())
        super().inputMethodEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            if self.composing:
                super().keyPressEvent(event)
                return
            self.submitted.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasText() and not event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            self.rejected.emit()
            event.ignore()

    def dropEvent(self, event):
        if event.mimeData().hasText() and not event.mimeData().hasUrls():
            self.dropped.emit(event.mimeData().text())
            event.acceptProposedAction()


class HoverButton(QPushButton):
    hovered = Signal()

    def enterEvent(self, event):
        self.hovered.emit()
        super().enterEvent(event)


class HoverMenu(QMenu):
    def __init__(self, owner, button):
        super().__init__(owner)
        self.button = button
        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.hide_if_outside)

    def leaveEvent(self, event):
        self.hide_timer.start(250)
        super().leaveEvent(event)

    def enterEvent(self, event):
        self.hide_timer.stop()
        super().enterEvent(event)

    def hide_if_outside(self):
        if not self.underMouse() and not self.button.underMouse():
            self.hide()


class SettingsDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.setWindowTitle("WordBook 设置")
        self.resize(500, 380)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("留空则保留已保存密钥")
        show = QCheckBox("显示本次输入")
        show.toggled.connect(lambda yes: self.key.setEchoMode(QLineEdit.EchoMode.Normal if yes else QLineEdit.EchoMode.Password))
        form.addRow("DeepSeek 密钥", self.key)
        form.addRow("", show)
        self.model = QLineEdit(owner.settings["model"])
        form.addRow("模型名", self.model)
        self.hotkey = QLineEdit(owner.settings["hotkey"])
        form.addRow("全局快捷键", self.hotkey)
        self.mouse_button = QComboBox()
        for code, label in MOUSE_SIDE_BUTTONS.items():
            self.mouse_button.addItem(label, code)
        self.mouse_button.setCurrentIndex(max(0, self.mouse_button.findData(owner.settings["mouse_side_button"])))
        self.mouse_button.setToolTip("拖选或双击选中文字后按侧键翻译；未选中时侧键保持原有前进/后退功能")
        form.addRow("鼠标侧键划词", self.mouse_button)
        self.autostart = QCheckBox("Windows 登录后自动启动")
        self.autostart.setChecked(owner.settings["autostart"])
        form.addRow("", self.autostart)
        layout.addLayout(form)
        info = QLabel("只有主动提交的文字会发送至 DeepSeek；调用可能产生费用。密钥仅保存在 Windows 凭据管理器。")
        info.setWordWrap(True)
        layout.addWidget(info)
        row = QHBoxLayout()
        for title, callback in (("测试连接", self.test_connection), ("打开数据目录", owner.open_data_dir),
                                ("备份", owner.backup), ("恢复", owner.restore)):
            button = QPushButton(title)
            button.clicked.connect(callback)
            row.addWidget(button)
        layout.addLayout(row)
        save = QPushButton("保存设置")
        save.clicked.connect(self.save)
        layout.addWidget(save)

    def test_connection(self):
        key = self.key.text().strip()
        if not key:
            try:
                key = get_api_key()
            except RuntimeError as exc:
                QMessageBox.warning(self, "连接测试", str(exc))
                return
        if not key:
            QMessageBox.information(self, "连接测试", "请先输入密钥。")
            return
        # A real call is explicitly user initiated and may incur a small charge.
        if QMessageBox.question(self, "连接测试", "将发送测试文本至 DeepSeek，可能产生费用。继续吗？") != QMessageBox.StandardButton.Yes:
            return
        self.owner.start_test(key, self.model.text().strip())

    def save(self):
        model = self.model.text().strip()
        sequence = self.hotkey.text().strip()
        if not model:
            QMessageBox.warning(self, "设置", "模型名不能为空。")
            return
        try:
            from .platform_win import hotkey_parts
            hotkey_parts(sequence)
            if self.key.text().strip():
                set_api_key(self.key.text().strip())
        except (ValueError, RuntimeError) as exc:
            QMessageBox.warning(self, "设置", str(exc))
            return
        old = self.owner.settings.copy()
        mouse_button = self.mouse_button.currentData()
        self.owner.settings.update(model=model, hotkey=sequence, autostart=self.autostart.isChecked(),
                                   mouse_side_button=mouse_button)
        if not self.owner.hotkey.register(sequence):
            self.owner.settings = old
            self.owner.hotkey.register(old["hotkey"])
            QMessageBox.warning(self, "快捷键冲突", "该快捷键被其他程序占用，请换一个组合。")
            return
        if not self.owner.mouse_hook.start(mouse_button):
            self.owner.settings = old
            self.owner.hotkey.register(old["hotkey"])
            self.owner.mouse_hook.start(old["mouse_side_button"])
            QMessageBox.warning(self, "鼠标侧键", "系统没有允许启用鼠标监听；快捷键仍可使用。")
            return
        try:
            set_autostart(self.autostart.isChecked())
        except OSError:
            QMessageBox.warning(self, "开机启动", "无法修改开机启动项，请检查系统权限。")
        save_settings(self.owner.root, self.owner.settings)
        self.owner.hotkey_ok = True
        self.owner.mouse_hook_ok = True
        self.owner.clipboard_seen = clipboard_sequence()
        self.accept()


class LibraryDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.setWindowTitle("WordBook 词库")
        self.resize(600, 550)
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText("搜索原文或译文")
        self.query.textChanged.connect(self.refresh)
        self.translation_type = QComboBox()
        self.translation_type.addItem("全部翻译类型", None)
        for code, name in TRANSLATION_TYPES.items():
            self.translation_type.addItem(name, code)
        self.translation_type.currentIndexChanged.connect(self.refresh)
        row.addWidget(self.query)
        row.addWidget(self.translation_type)
        layout.addLayout(row)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.itemDoubleClicked.connect(self.open_item)
        layout.addWidget(self.list)
        buttons = QHBoxLayout()
        self.delete_button = QPushButton("删除选中")
        self.delete_button.clicked.connect(self.delete_selected)
        buttons.addWidget(self.delete_button)
        for text, days in (("导出当前列表", None), ("最近 7 天", 7), ("最近 30 天", 30)):
            button = QPushButton(text)
            button.clicked.connect(lambda _, n=days: owner.export(n, translation_type=self.translation_type.currentData(), query=self.query.text().strip()))
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.count_label = QLabel()
        layout.addWidget(self.count_label)
        self.refresh()

    def refresh(self):
        self.list.clear()
        for card in self.owner.store.search(self.query.text().strip(), translation_type=self.translation_type.currentData()):
            analysis = json.loads(card["analysis_json"]) if card["analysis_json"] else None
            summary = brief_translation(analysis) if analysis else "待翻译"
            self.list.addItem(f"[{type_name(card['translation_type'])}] {card['original']}  ·  {summary}  ·  {card['seen_count']} 次")
            self.list.item(self.list.count() - 1).setData(Qt.ItemDataRole.UserRole, card["id"])
        self.count_label.setText(f"当前列表共 {self.list.count()} 条，按住 Ctrl 可多选。")

    def delete_selected(self):
        selected = self.list.selectedItems()
        if not selected:
            QMessageBox.information(self, "删除记录", "请先选择要删除的词条。")
            return
        if QMessageBox.question(self, "删除记录", f"确认删除选中的 {len(selected)} 条词条及其遇到记录吗？", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        ids = [item.data(Qt.ItemDataRole.UserRole) for item in selected]
        self.owner.store.delete_many(ids)
        if self.owner.current_id in ids:
            self.owner.current_id = None
            self.owner.details_open = False
            self.owner.set_status("所选词条已删除。")
        self.refresh()

    def open_item(self, item):
        self.owner.show_card(item.data(Qt.ItemDataRole.UserRole), fill_input=True)
        self.accept()


class MainWindow(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        app.setQuitOnLastWindowClosed(False)
        app.setWindowIcon(icon())
        self.root = data_dir()
        self.settings = load_settings(self.root)
        self.translation_type = "en-zh"
        self.store = Store(self.root / "wordbook.sqlite3")
        self.workers = set()
        self.selection_workers = set()
        self.selection_busy = False
        self.current_id = None
        latest = self.store.latest_card()
        self.current_id = latest["id"] if latest else None
        self.last_submitted_id = latest["id"] if latest else None
        self.clipboard_seen = clipboard_sequence()
        self.status_text = "输入文本后按 Enter 翻译并记录。"
        if latest:
            self.status_text = latest["error"] or ("翻译已保存。" if latest["analysis_json"] else "原文已保存，待翻译。")
        self.details_open = False
        self.expanded = False
        self.pinned = False
        self.shortcut_grace_active = False
        self.drag_origin = None
        self.setWindowTitle("WordBook")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAcceptDrops(True)
        self.build_ui()
        app.installEventFilter(self)
        self.create_tray()
        self.hotkey = Hotkey(app, self, self.from_clipboard)
        self.hotkey_ok = self.hotkey.register(self.settings["hotkey"])
        self.mouse_hook = MouseSideHook(self)
        self.mouse_hook.activated.connect(self.from_mouse_selection, Qt.ConnectionType.QueuedConnection)
        self.mouse_hook_ok = self.mouse_hook.start(self.settings["mouse_side_button"])
        self.hover_timer = QTimer(self)
        self.hover_timer.setSingleShot(True)
        self.hover_timer.timeout.connect(self.expand)
        self.leave_timer = QTimer(self)
        self.leave_timer.setSingleShot(True)
        self.leave_timer.timeout.connect(self.maybe_collapse)
        self.shortcut_grace_timer = QTimer(self)
        self.shortcut_grace_timer.setSingleShot(True)
        self.shortcut_grace_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.shortcut_grace_timer.timeout.connect(self.shortcut_grace_expired)
        app.screenAdded.connect(self.add_screen)
        app.screenRemoved.connect(lambda _: self.reposition())
        for screen in app.screens():
            self.connect_screen(screen)
        self.expanded = True
        self.collapse()
        self.show()
        self.daily_backup()
        if not os.environ.get("WORDBOOK_TEST_DATA_DIR"):
            try:
                set_autostart(self.settings["autostart"])
            except OSError:
                pass
        QTimer.singleShot(300, self.first_launch)

    def build_ui(self):
        self.setStyleSheet("QWidget { background:#f8fbff; color:#172d45; font-size:13px; } QPushButton { background:#e4eef9; border:1px solid #b5cbe3; border-radius:6px; padding:6px; } QPushButton:hover { background:#d2e7fb; } QTextEdit,QLineEdit { background:white; border:1px solid #aac5df; border-radius:5px; padding:5px; }")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.tile = DropArea("W")
        self.tile.setStyleSheet("background:#2775cc;color:white;font-size:21px;font-weight:bold;border-radius:6px")
        self.tile.dropped.connect(self.submit_drop)
        self.tile.entered.connect(self.expand)
        self.tile.rejected.connect(self.reject_drop)
        outer.addWidget(self.tile)
        self.panel = QWidget()
        body = QVBoxLayout(self.panel)
        self.handle = QLabel("⋯")
        self.handle.setFixedHeight(10)
        self.handle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.handle.setToolTip("拖动到屏幕边缘")
        self.handle.setCursor(Qt.CursorShape.SizeAllCursor)
        body.addWidget(self.handle)
        self.input = InputEdit()
        self.input.dropped.connect(self.submit_drop)
        self.input.rejected.connect(self.reject_drop)
        self.input.submitted.connect(self.submit_input)
        self.input.translation_type_selected.connect(self.set_translation_type)
        self.input.set_translation_type(self.translation_type)
        self.input.setPlaceholderText("输入或粘贴文本\nEnter 翻译 · Shift+Enter 换行")
        self.input.setFixedHeight(92)
        if self.current_id:
            self.input.setPlainText(self.store.card(self.current_id)["original"])
        body.addWidget(self.input)
        self.output = QTextBrowser()
        self.output.setMinimumSize(0, 0)
        self.output.setOpenLinks(False)
        self.output.setOpenExternalLinks(False)
        self.output.anchorClicked.connect(lambda _: self.toggle_details())
        body.addWidget(self.output, 1)
        bottom = QHBoxLayout()
        bottom.addStretch()
        self.settings_button = HoverButton("设置")
        self.settings_button.setToolTip("悬停展开更多操作")
        bottom.addWidget(self.settings_button)
        body.addLayout(bottom)
        self.action_menu = HoverMenu(self, self.settings_button)
        for title, fn in (("修改", self.edit_card), ("删除", self.delete_card), ("词库", self.library),
                          ("设置", self.open_settings), ("7 天导出", lambda: self.export(7)), ("30 天导出", lambda: self.export(30))):
            self.action_menu.addAction(title, fn)
        self.settings_button.hovered.connect(self.show_action_menu)
        self.settings_button.clicked.connect(self.show_action_menu)
        outer.addWidget(self.panel)
        for widget in self.findChildren(QWidget):
            widget.installEventFilter(self)
        self.render_output()

    def show_action_menu(self):
        if self.action_menu.isVisible():
            return
        self.end_shortcut_grace()
        self.leave_timer.start(NORMAL_COLLAPSE_MS)
        point = self.settings_button.mapToGlobal(QPoint(0, self.settings_button.height()))
        area = (self.app.screenAt(point) or self.screen()).availableGeometry()
        size = self.action_menu.sizeHint()
        x = max(area.left(), min(point.x(), area.right() - size.width() + 1))
        y = point.y() if point.y() + size.height() <= area.bottom() else point.y() - size.height() - self.settings_button.height()
        self.action_menu.popup(QPoint(x, max(area.top(), y)))

    def set_translation_type(self, code, remember=True):
        if code not in TRANSLATION_TYPES:
            return
        self.translation_type = code
        self.input.set_translation_type(code)
        if remember:
            self.end_shortcut_grace()
            self.settings["translation_type"] = code
            save_settings(self.root, self.settings)
            self.set_status(f"已选择{type_name(code)}，按 Enter 翻译。")

    def reset_direction_after_completion(self, card):
        if (card["translation_type"] != "zh-en" or self.translation_type != "zh-en"):
            return False
        self.translation_type = "en-zh"
        self.input.set_translation_type("en-zh")
        self.settings["translation_type"] = "en-zh"
        save_settings(self.root, self.settings)
        return True

    def create_tray(self):
        self.tray = QSystemTrayIcon(icon(), self)
        menu = QMenu()
        for title, fn in (("展开 WordBook", self.show_latest_record), ("收起", self.collapse),
                          ("固定展开/取消固定", self.toggle_pin), ("词库", self.library), ("设置", self.open_settings), ("退出", self.quit)):
            menu.addAction(title, fn)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.show_latest_record() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()

    def first_launch(self):
        if not self.settings["onboarded"]:
            QMessageBox.information(self, "欢迎使用 WordBook", "原文和词库保存在本机。只有通过快捷键、鼠标侧键划词、拖入或输入框主动提交的文字才发送至 DeepSeek。API 可能收费；请在设置中填写自己的密钥，也可暂时跳过。")
            self.settings["onboarded"] = True
            save_settings(self.root, self.settings)
        if not self.hotkey_ok:
            QMessageBox.warning(self, "快捷键冲突", "默认快捷键被其他程序占用。请在设置中指定新组合。")
        if not self.mouse_hook_ok and self.settings["mouse_side_button"] != "off":
            QMessageBox.warning(self, "鼠标侧键", "系统没有允许启用鼠标侧键监听。可在设置中关闭，或重启程序后再试。")

    def screen(self):
        return next((s for s in self.app.screens() if s.name() == self.settings["screen"]), self.app.primaryScreen())

    def connect_screen(self, screen):
        screen.availableGeometryChanged.connect(lambda _: self.reposition())
        screen.logicalDotsPerInchChanged.connect(lambda _: self.reposition())

    def add_screen(self, screen):
        self.connect_screen(screen)
        self.reposition()

    def reposition(self):
        area = self.screen().availableGeometry()
        if self.expanded:
            self.input.setFixedHeight(min(92, max(36, (area.height() - 60) // 3)))
            self.resize(min(380, area.width()), min(400, area.height()))
        edge = self.settings["edge"]
        size = self.size()
        ratio = max(0, min(1, float(self.settings["offset"])))
        if edge == "left":
            x, y = area.left(), area.top() + round((area.height() - size.height()) * ratio)
        elif edge == "top":
            x, y = area.left() + round((area.width() - size.width()) * ratio), area.top()
        else:
            x, y = area.right() - size.width() + 1, area.top() + round((area.height() - size.height()) * ratio)
        self.move(max(area.left(), min(x, area.right() - size.width() + 1)),
                  max(area.top(), min(y, area.bottom() - size.height() + 1)))

    def expand(self):
        self.hover_timer.stop()
        self.leave_timer.stop()
        if not self.expanded:
            self.expanded = True
            self.tile.hide()
            self.panel.show()
            area = self.screen().availableGeometry()
            self.resize(min(380, area.width()), min(400, area.height()))
            self.reposition()
        self.show()
        self.raise_()
        # A shortcut can reveal the panel while the pointer is elsewhere,
        # without producing a leave event. Always arm the idle check.
        if not self.shortcut_grace_active:
            self.leave_timer.start(NORMAL_COLLAPSE_MS)

    def collapse(self):
        self.shortcut_grace_active = False
        self.shortcut_grace_timer.stop()
        self.leave_timer.stop()
        self.expanded = False
        self.action_menu.hide()
        self.input.type_menu.hide()
        self.details_open = False
        self.render_output()
        self.panel.hide()
        self.tile.show()
        self.resize(28, 52)
        self.reposition()

    def toggle_pin(self):
        self.end_shortcut_grace()
        self.pinned = not self.pinned
        if self.expanded:
            self.leave_timer.start(NORMAL_COLLAPSE_MS)

    def begin_shortcut_grace(self):
        self.shortcut_grace_active = True
        self.leave_timer.stop()
        self.shortcut_grace_timer.start(SHORTCUT_GRACE_MS)
        self.expand()

    def end_shortcut_grace(self, start_normal=True):
        if not self.shortcut_grace_active:
            return
        self.shortcut_grace_active = False
        self.shortcut_grace_timer.stop()
        if start_normal and self.expanded:
            self.leave_timer.start(NORMAL_COLLAPSE_MS)

    def shortcut_grace_expired(self):
        self.shortcut_grace_active = False
        if (self.pinned or self.underMouse() or self.drag_origin is not None
                or self.app.activePopupWidget() or self.app.activeModalWidget()):
            self.leave_timer.start(NORMAL_COLLAPSE_MS)
            return
        # Expiry is itself the idle deadline; do not grant a fresh 600 ms.
        self.collapse()

    def eventFilter(self, watched, event):
        if self.shortcut_grace_active and event.type() in (
                QEvent.Type.Enter, QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick,
                QEvent.Type.KeyPress, QEvent.Type.Wheel, QEvent.Type.DragEnter, QEvent.Type.Drop):
            widget = watched if isinstance(watched, QWidget) else None
            if widget is self or (widget is not None and self.isAncestorOf(widget)):
                self.end_shortcut_grace()
        return super().eventFilter(watched, event)

    def enterEvent(self, event):
        self.end_shortcut_grace(start_normal=False)
        self.leave_timer.stop()
        if not self.expanded:
            self.hover_timer.start(200)
        else:
            self.leave_timer.start(NORMAL_COLLAPSE_MS)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.hover_timer.stop()
        if self.expanded:
            self.leave_timer.start(NORMAL_COLLAPSE_MS)
        super().leaveEvent(event)

    def maybe_collapse(self):
        if not self.expanded or self.shortcut_grace_active:
            return
        editing = self.input.hasFocus() and (self.input.document().isModified() or self.input.composing)
        interacting = self.drag_origin is not None or self.app.mouseButtons() != Qt.MouseButton.NoButton
        if self.underMouse() or self.pinned or editing or interacting or self.app.activePopupWidget() or self.app.activeModalWidget():
            # Temporary blockers must be checked again after they end.
            self.leave_timer.start(NORMAL_COLLAPSE_MS)
            return
        self.collapse()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and (not self.expanded or self.handle.rect().contains(self.handle.mapFrom(self, event.position().toPoint()))):
            self.drag_origin = event.globalPosition().toPoint() - self.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.drag_origin and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self.drag_origin)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.drag_origin:
            self.drag_origin = None
            self.snap()
        super().mouseReleaseEvent(event)

    def snap(self):
        center = self.frameGeometry().center()
        screen = self.app.screenAt(center) or self.app.primaryScreen()
        area = screen.availableGeometry()
        distances = {"left": abs(center.x() - area.left()), "right": abs(center.x() - area.right()),
                     "top": abs(center.y() - area.top())}
        edge = min(distances, key=distances.get)
        span = area.width() - self.width() if edge == "top" else area.height() - self.height()
        coordinate = self.x() - area.left() if edge == "top" else self.y() - area.top()
        self.settings.update(screen=screen.name(), edge=edge, offset=max(0, min(1, coordinate / max(1, span))))
        save_settings(self.root, self.settings)
        self.reposition()

    def dragEnterEvent(self, event):
        if event.mimeData().hasText() and not event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.expand()
        else:
            self.reject_drop()
            event.ignore()

    def reject_drop(self):
        self.expand()
        self.set_status("只支持拖入纯文本，不接受文件或其他内容。")

    def dropEvent(self, event):
        if event.mimeData().hasText() and not event.mimeData().hasUrls():
            self.submit_drop(event.mimeData().text())
            event.acceptProposedAction()

    def submit_drop(self, text):
        self.expand()
        self.input.setPlainText(text)
        self.submit_input()

    def from_clipboard(self):
        self.action_menu.hide()
        self.input.type_menu.hide()
        try:
            sequence = clipboard_sequence()
            if sequence and sequence == self.clipboard_seen:
                self.show_latest_record()
                return
            try:
                sequence, text = read_clipboard_snapshot()
            except RuntimeError as exc:
                self.expand()
                self.set_status(str(exc))
                return
            self.clipboard_seen = sequence
            if not text or not text.strip():
                self.show_latest_record()
                self.set_status("剪贴板没有纯文本。" if text is None else "剪贴板为空，已显示最近记录。")
                return
            self.input.setPlainText(text)
            self.submit_input()
            self.activateWindow()
            self.input.setFocus()
        finally:
            # Shortcut-triggered content stays readable for five seconds unless
            # the user interacts, which switches back to the normal idle rule.
            self.begin_shortcut_grace()

    def from_mouse_selection(self, target_hwnd=None):
        if self.selection_busy:
            return
        self.selection_busy = True
        try:
            worker = SelectionWorker(target_hwnd)
            self.selection_workers.add(worker)
            worker.finished_selection.connect(self.selection_done)
            worker.finished.connect(lambda w=worker: self.selection_workers.discard(w))
            worker.finished.connect(worker.deleteLater)
            worker.start()
        except Exception:
            self.selection_busy = False
            self.expand()
            self.set_status("无法启动选中文字读取，请改用 Ctrl+Alt+W 或手动粘贴。")
            self.begin_shortcut_grace()

    def selection_done(self, text, error):
        self.selection_busy = False
        self.clipboard_seen = clipboard_sequence()
        try:
            if error:
                self.set_status(error)
                return
            self.input.setPlainText(text)
            self.submit_input()
        finally:
            self.begin_shortcut_grace()
            self.activateWindow()
            self.input.setFocus()

    def show_latest_record(self):
        self.end_shortcut_grace()
        self.action_menu.hide()
        self.input.type_menu.hide()
        latest = self.store.latest_card()
        self.last_submitted_id = latest["id"] if latest else None
        if latest:
            self.show_card(latest["id"], fill_input=True)
        else:
            self.expand()
        self.activateWindow()
        self.input.setFocus()

    def submit_input(self):
        text = self.input.toPlainText()
        kind = "word" if re.fullmatch(r"[A-Za-z]+(?:['’\-][A-Za-z]+)*", text.strip()) else "sentence"
        try:
            card = self.store.submit(text, kind, translation_type=self.translation_type)
        except ValueError as exc:
            self.set_status(str(exc))
            return
        self.last_submitted_id = card["id"]
        self.clipboard_seen = clipboard_sequence()
        self.input.document().setModified(False)
        self.show_card(card["id"])
        analysis = json.loads(card["analysis_json"]) if card["analysis_json"] else None
        if analysis and (card["kind"] != "word" or has_word_phonetics(analysis)):
            if self.reset_direction_after_completion(card):
                self.set_status("已记录再次遇到；中译英已完成，已切回英译中。")
            else:
                self.set_status("已记录再次遇到，沿用现有解析。")
        else:
            self.start_translation(card)

    def start_translation(self, card, key=None):
        try:
            key = key if key is not None else get_api_key()
        except RuntimeError as exc:
            self.store.set_error(card["id"], str(exc))
            self.show_card(card["id"])
            return
        if not key:
            self.store.set_error(card["id"], "请先在设置中填写 DeepSeek API 密钥。")
            self.show_card(card["id"])
            return
        if any(worker.card_id == card["id"] for worker in self.workers):
            self.set_status("正在翻译，请稍候。")
            return
        worker = TranslationWorker(card["id"], card["original"], card["kind"], key, self.settings["model"], card["translation_type"])
        self.workers.add(worker)
        worker.finished_result.connect(self.translation_done)
        worker.finished.connect(lambda w=worker: self.workers.discard(w))
        worker.finished.connect(worker.deleteLater)
        self.set_status("保留原解析，后台补全美式、英式音标中…" if card["analysis_json"] else "原文已保存，后台翻译中…")
        worker.start()

    def translation_done(self, card_id, result, error):
        card = self.store.card(card_id)
        if not card:
            return
        translated = False
        if error:
            self.store.set_error(card_id, error)
        else:
            # Upgrading a cached word adds pronunciation without overwriting
            # its saved translation, notes, or the user's own edits.
            previous = json.loads(card["analysis_json"]) if card["analysis_json"] else None
            if previous and card["kind"] == "word" and not has_word_phonetics(previous):
                result = {**previous, **{field: result[field] for field in PHONETIC_FIELDS}}
            self.store.save_analysis(card_id, result)
            translated = True
        if self.current_id == card_id:
            self.show_card(card_id, reveal=False)
        if translated and self.reset_direction_after_completion(card) and self.current_id == card_id:
            self.set_status("中译英已完成，已切回英译中。")

    def show_card(self, card_id, fill_input=False, reveal=True):
        card = self.store.card(card_id)
        if not card:
            return
        if self.current_id != card_id:
            self.details_open = False
        self.current_id = card_id
        if fill_input:
            self.set_translation_type(card["translation_type"], remember=False)
            self.input.setPlainText(card["original"])
            self.input.document().setModified(False)
        self.status_text = card["error"] or ("翻译已保存。" if card["analysis_json"] else "原文已保存，待翻译。")
        self.render_output()
        if reveal:
            self.expand()

    def set_status(self, message):
        self.status_text = message
        self.render_output()

    def render_output(self):
        card = self.store.card(self.current_id) if self.current_id else None
        parts = []
        if card:
            parts.append(f"<h3>{escape(card['original']).replace(chr(10), '<br>')}</h3>")
            if card["analysis_json"]:
                a = json.loads(card["analysis_json"])
                if card["kind"] == "word":
                    phonetics = [f"{label} {escape(a[field])}" for field, label in
                                 (("phonetic_us", "美式"), ("phonetic_uk", "英式"))
                                 if valid_phonetic(a.get(field))]
                    if phonetics:
                        parts.append("<p>" + "<br>".join(phonetics) + "</p>")
                    if not has_word_phonetics(a):
                        parts.append("<p style='color:#64768a'>音标尚未补全，再次按 Enter 翻译时补全。</p>")
                translation = escape(brief_translation(a)).replace(chr(10), "<br>")
                if card["kind"] == "word" and a.get("part_of_speech"):
                    parts.append(f"<p>词性：{escape(a['part_of_speech'])}　{translation}</p>")
                else:
                    parts.append(f"<p>{translation}</p>")
                parts.append(f'<p><a href="wordbook:details">{"收起详情" if self.details_open else "详细解析"}</a></p>')
                if self.details_open:
                    parts.append(f"<p>{escape(a['details_zh']).replace(chr(10), '<br>')}</p>")
                    if a.get("brief_usage_zh"):
                        parts.append(f"<p>用法补充：{escape(a['brief_usage_zh'])}</p>")
                    for example in a["examples"]:
                        parts.append(f"<p>AI 生成例句：{escape(example['en'])}<br>{escape(example['zh'])}</p>")
            else:
                parts.append("<p>待翻译</p>")
            last = datetime.fromisoformat(card["last_seen_utc"]).astimezone().strftime("%Y-%m-%d %H:%M")
            parts.append(f"<p style='color:#64768a'>{type_name(card['translation_type'])} · 遇到 {card['seen_count']} 次 · 最近 {last}</p>")
        parts.append(f"<p style='color:#64768a'>{escape(self.status_text)}</p>")
        self.output.setHtml("".join(parts))

    def toggle_details(self):
        self.end_shortcut_grace()
        self.details_open = not self.details_open
        self.render_output()

    def edit_card(self):
        card = self.store.card(self.current_id) if self.current_id else None
        if not card or not card["analysis_json"]:
            self.set_status("请先完成翻译，再修改解析。")
            return
        a = json.loads(card["analysis_json"])
        dialog = QDialog(self)
        dialog.setWindowTitle("修改译文与说明")
        dialog.resize(470, 390)
        form = QFormLayout(dialog)
        brief = QLineEdit(brief_translation(a))
        usage = QLineEdit(a["brief_usage_zh"])
        details = QTextEdit(a["details_zh"])
        form.addRow("简译", brief)
        form.addRow("简要用法", usage)
        form.addRow("详细解析", details)
        save = QPushButton("保存")
        def commit():
            try:
                self.store.edit(card["id"], brief.text(), usage.text(), details.toPlainText())
                dialog.accept()
                self.show_card(card["id"])
            except ValueError as exc:
                QMessageBox.warning(dialog, "修改", str(exc))
        save.clicked.connect(commit)
        form.addRow(save)
        dialog.exec()

    def delete_card(self):
        if self.current_id and QMessageBox.question(self, "删除记录", "确定删除这张卡片及全部遇到记录吗？") == QMessageBox.StandardButton.Yes:
            self.store.delete(self.current_id)
            self.current_id = None
            self.details_open = False
            self.set_status("记录已删除。")

    def library(self):
        self.expand()
        LibraryDialog(self).exec()

    def open_settings(self):
        self.expand()
        SettingsDialog(self).exec()

    def open_data_dir(self):
        os.startfile(self.root)

    def export(self, days=None, translation_type=None, query=""):
        period = f"最近{days}天" if days else "词库"
        classification = type_name(translation_type) + "-" if translation_type else ""
        suggested = f"WordBook-{classification}{period}-{date.today().isoformat()}.md"
        path, _ = QFileDialog.getSaveFileName(self, "导出 Markdown", str(Path.home() / "Documents" / suggested), "Markdown (*.md)")
        if path:
            Path(path).write_text(self.store.export_markdown(days, translation_type=translation_type, query=query), encoding="utf-8")
            QMessageBox.information(self, "导出完成", f"已保存到：{path}")

    def backup(self):
        path, _ = QFileDialog.getSaveFileName(self, "备份全部数据", str(Path.home() / "Documents" / f"WordBook-备份-{date.today()}.wbak"), "WordBook 备份 (*.wbak)")
        if path:
            try:
                self.store.backup_to(path, self.settings)
                QMessageBox.information(self, "备份完成", f"已保存到：{path}")
            except Exception:
                QMessageBox.warning(self, "备份失败", "无法创建备份，请检查目标位置。")

    def restore(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择备份", str(Path.home() / "Documents"), "WordBook 备份 (*.wbak)")
        if not path:
            return
        if QMessageBox.question(self, "确认恢复", "恢复将替换当前词库；程序会先保存当前数据快照。继续吗？") != QMessageBox.StandardButton.Yes:
            return
        try:
            snapshot = self.root / f"恢复前快照-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.wbak"
            self.store.backup_to(snapshot, self.settings)
            settings = self.store.restore_from(path)
            restored = sanitize_settings({**self.settings, **settings})
            if not self.hotkey.register(restored["hotkey"]):
                restored["hotkey"] = self.settings["hotkey"]
                self.hotkey.register(restored["hotkey"])
                QMessageBox.warning(self, "快捷键冲突", "词库已恢复，但备份中的快捷键被占用；已保留当前快捷键。")
            if not self.mouse_hook.start(restored["mouse_side_button"]):
                restored["mouse_side_button"] = "off"
                self.mouse_hook.start("off")
                QMessageBox.warning(self, "鼠标侧键", "词库已恢复，但系统未允许重新启用鼠标监听；侧键划词已关闭。")
            self.mouse_hook_ok = True
            self.settings = restored
            try:
                set_autostart(self.settings["autostart"])
            except OSError:
                QMessageBox.warning(self, "开机启动", "词库已恢复，但系统未允许更新开机启动项。")
            save_settings(self.root, self.settings)
            self.current_id = None
            self.details_open = False
            self.clipboard_seen = clipboard_sequence()
            self.show_latest_record()
            self.reposition()
            QMessageBox.information(self, "恢复完成", f"词库已恢复。恢复前快照：{snapshot}")
        except Exception:
            QMessageBox.warning(self, "恢复失败", "备份无效或无法恢复，当前词库未确认替换。")

    def daily_backup(self):
        backups = self.root / "自动备份"
        backups.mkdir(exist_ok=True)
        path = backups / f"{date.today().isoformat()}.wbak"
        if not path.exists():
            try:
                self.store.backup_to(path, self.settings)
            except Exception:
                self.set_status("今日自动备份失败，请检查数据目录。")
        for old in sorted(backups.glob("*.wbak"), reverse=True)[7:]:
            old.unlink(missing_ok=True)

    def start_test(self, key, model):
        worker = TranslationWorker(-1, "apple", "word", key, model)
        self.workers.add(worker)
        worker.finished_result.connect(lambda _, result, error: QMessageBox.information(self, "连接测试", "连接成功。" if result else error))
        worker.finished.connect(lambda w=worker: self.workers.discard(w))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def closeEvent(self, event):
        self.collapse()
        event.ignore()

    def quit(self):
        self.mouse_hook.stop()
        self.hotkey.close()
        self.tray.hide()
        for worker in list(self.selection_workers):
            worker.wait(3000)
        for worker in list(self.workers):
            worker.wait(30000)
        self.store.close()
        self.hide()
        # QApplication.quit() can be cancelled by the normal collapse-only closeEvent.
        self.app.exit(0)
