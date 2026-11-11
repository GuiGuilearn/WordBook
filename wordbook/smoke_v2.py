"""Real Windows Qt and native-hotkey regression checks for the compact UI."""
import ctypes
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

import httpx
from PySide6.QtCore import QMimeData, QPoint, QPointF, QRect, QTimer, Qt, QUrl
from PySide6.QtGui import QCursor, QDragEnterEvent, QEnterEvent, QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

from .platform_win import SingleInstance, USER32, data_dir, hotkey_parts
from .ui import LibraryDialog, MainWindow, NORMAL_COLLAPSE_MS, SHORTCUT_GRACE_MS, SettingsDialog


def run(stage):
    if not os.environ.get("WORDBOOK_TEST_DATA_DIR"):
        raise RuntimeError("GUI 测试必须指定 WORDBOOK_TEST_DATA_DIR。")
    from . import ui
    ui.get_api_key = lambda: None
    app = QApplication(sys.argv)
    app.setApplicationName("WordBook")
    app.setOrganizationName("WordBook")
    root = data_dir()
    instance = SingleInstance(root)
    if not instance.is_primary:
        return 0
    saved_clipboard = QMimeData()
    saved_pointer = QCursor.pos()
    old_mime = app.clipboard().mimeData()
    if old_mime:
        for fmt in old_mime.formats():
            saved_clipboard.setData(fmt, old_mime.data(fmt))
    first_launch = MainWindow.first_launch
    MainWindow.first_launch = lambda self: None
    window = MainWindow(app)
    instance.activation_requested.connect(window.show_latest_record)
    app.aboutToQuit.connect(instance.close)
    results = {"stage": stage, "version": "direction-reset-v9", "qt_platform": app.platformName(),
               "screen": window.screen().name(), "screen_area": list(window.screen().availableGeometry().getRect()),
               "hotkey_registered": window.hotkey_ok, "mouse_hook_installed": window.mouse_hook.installed,
               "tray_available": window.tray.isSystemTrayAvailable()}

    def wait_until(predicate):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            QTest.qWait(20)
            if predicate():
                return
        results["timeout_state"] = {"expanded": window.expanded, "under_mouse": window.underMouse(),
                                    "hover_timer_active": window.hover_timer.isActive(),
                                    "leave_timer_active": window.leave_timer.isActive(),
                                    "geometry": list(window.geometry().getRect()),
                                    "pointer": [QCursor.pos().x(), QCursor.pos().y()],
                                    "focus": type(app.focusWidget()).__name__ if app.focusWidget() else None}
        raise AssertionError("GUI operation timed out; " + window.status_text)

    def enter_text(text):
        window.input.setPlainText(text)
        window.input.setFocus()
        QTest.keyClick(window.input, Qt.Key.Key_Return)
        app.processEvents()
        return window.store.card(window.current_id)

    def current_original():
        card = window.store.card(window.current_id) if window.current_id else None
        return card["original"] if card else ""

    def verify_native_hotkey():
        sequence = next((seq for seq in ("Ctrl+Alt+9", "Ctrl+Alt+8", "Ctrl+Alt+7", "Ctrl+Alt+0") if window.hotkey.register(seq)), None)
        if not sequence:
            raise AssertionError("No free QA shortcut")
        results["tested_shortcut"] = sequence
        modifiers, key = hotkey_parts(sequence)
        USER32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        def trigger():
            assert USER32.PostMessageW(window.hotkey.hwnd, 0x0312, 0x5742, (key << 16) | (modifiers & 0xFFFF))

        def move_pointer(widget):
            QCursor.setPos(widget.mapToGlobal(widget.rect().center()))
            QTest.qWait(100)

        def move_outside():
            QCursor.setPos(window.screen().availableGeometry().center())
            QTest.qWait(100)
            assert not window.underMouse()

        def wait_until_tile():
            wait_until(lambda: not window.expanded)
            assert window.tile.isVisible() and not window.panel.isVisible()

        def trigger_shortcut(text):
            app.clipboard().setText(text)
            move_outside()
            trigger()
            wait_until(lambda: current_original() == text and window.expanded)

        trigger_shortcut("shortcut-initial")
        initial = window.current_id
        settings = SettingsDialog(window)
        settings.hotkey.setText(sequence)
        settings.save()
        latest = enter_text("manual-latest")
        events = window.store.db.execute("SELECT count(*) FROM encounters").fetchone()[0]
        window.show_card(initial, fill_input=True)
        trigger()
        wait_until(lambda: current_original() == "manual-latest")
        results["shortcut_wakes_latest_after_settings_and_manual_input"] = window.current_id == latest["id"] and window.input.toPlainText() == "manual-latest"
        results["unchanged_clipboard_wake_creates_no_encounter"] = events == window.store.db.execute("SELECT count(*) FROM encounters").fetchone()[0]
        trigger_shortcut("fresh-clipboard-two")
        results["native_hotkey_reads_fresh_clipboard"] = window.input.toPlainText() == "fresh-clipboard-two"
        window.collapse()
        move_outside()
        card_id = window.current_id
        events = window.store.db.execute("SELECT count(*) FROM encounters").fetchone()[0]
        app.clipboard().setText("temporary-copy-without-submit")
        app.clipboard().setText("fresh-clipboard-two")
        QTest.qWait(100)
        results["clipboard_change_alone_does_not_submit"] = events == window.store.db.execute("SELECT count(*) FROM encounters").fetchone()[0]
        trigger()
        wait_until(lambda: window.store.card(card_id)["seen_count"] == 2)
        results["recopy_same_text_records_new_encounter"] = window.current_id == card_id

        window.collapse()
        move_outside()
        trigger_shortcut("shortcut-grace-visible")
        grace_started = time.monotonic()
        QTest.qWait(2600)
        results["shortcut_stays_visible_beyond_normal_collapse_delay"] = (
            window.expanded and window.shortcut_grace_active and window.panel.isVisible()
            and time.monotonic() - grace_started < SHORTCUT_GRACE_MS / 1000)
        QTest.keyClick(window.input, Qt.Key.Key_Control)
        QTest.qWait(100)
        results["shortcut_interaction_ends_grace"] = (
            not window.shortcut_grace_active and window.leave_timer.isActive()
            and window.leave_timer.remainingTime() <= NORMAL_COLLAPSE_MS)
        window.collapse()
        results["shortcut_interaction_resumes_normal_collapse"] = True

        window.collapse()
        move_outside()
        trigger_shortcut("shortcut-grace-timeout")
        window.pinned = False
        grace_started = time.monotonic()
        QTest.qWait(SHORTCUT_GRACE_MS + 250)
        wait_until_tile()
        elapsed = time.monotonic() - grace_started
        results["shortcut_idle_hides_after_five_seconds"] = (
            elapsed >= SHORTCUT_GRACE_MS / 1000 - 0.25 and elapsed < SHORTCUT_GRACE_MS / 1000 + 1.5)

    def verify_translation():
        original_translate = ui.translate
        mode = ["success"]
        calls = []
        def fake_translate(text, kind, *args, **kwargs):
            calls.append(text)
            time.sleep(0.06)
            if mode[0] == "failure":
                raise ui.ApiError("模拟网络超时，原文已保留。再次按 Enter 可重试。")
            zh = "苹果" if text == "apple" else "梨"
            return {"kind": kind, "brief_translation_zh": zh, "brief_usage_zh": "可数名词，常用于描述水果。",
                    "details_zh": f"常见搭配：an apple 或 a pear。中文：{zh}。", "part_of_speech": "n.",
                    "phonetic_us": "/ˈæpəl/" if text == "apple" else "/per/",
                    "phonetic_uk": "/ˈæpəl/" if text == "apple" else "/peə/",
                    "examples": [{"en": f"I ate {'an apple' if text == 'apple' else 'a pear'}.", "zh": f"我吃了一个{zh}。"}]}
        ui.translate = fake_translate
        ui.get_api_key = lambda: "mock-in-memory"
        try:
            apple = enter_text("apple")
            results["background_translation_keeps_input_enabled"] = window.input.isEnabled()
            wait_until(lambda: not window.workers)
            results["background_mock_translation_saved"] = bool(window.store.card(apple["id"])["analysis_json"]) and "苹果" in window.output.toPlainText()
            before = len(calls)
            window.output.anchorClicked.emit(QUrl("wordbook:details"))
            results["details_reuses_saved_analysis"] = window.details_open and "AI 生成例句" in window.output.toPlainText() and len(calls) == before
            window.grab().save(str(root / f"gui-{stage}-translated.png"))
            mode[0] = "failure"
            pear = enter_text("pear")
            wait_until(lambda: not window.workers)
            row = window.store.card(pear["id"])
            results["failure_preserves_pending_original"] = row["analysis_json"] is None and bool(row["error"])
            mode[0] = "success"
            enter_text("pear")
            wait_until(lambda: not window.workers)
            row = window.store.card(pear["id"])
            results["enter_retries_without_retry_button"] = bool(row["analysis_json"]) and row["seen_count"] == 2 and "梨" in window.output.toPlainText()
        finally:
            ui.translate = original_translate
            ui.get_api_key = lambda: None

    def verify_phonetics():
        original_translate = ui.translate
        calls = []
        mode = ["success"]
        def handler(request):
            calls.append(request)
            if mode[0] == "failure":
                raise httpx.ReadTimeout("mock timeout")
            message = json.loads(request.content)["messages"][1]["content"]
            sentence = "类型：sentence" in message
            tomato = "原文：tomato" in message
            if sentence:
                payload = {"kind": "sentence", "brief_translation": "我们喜欢番茄。",
                           "brief_usage_zh": "整句使用一般现在时，表示习惯性喜好。",
                           "details_zh": "主语是 We，谓语是 like，宾语是 tomatoes。",
                           "part_of_speech": "", "examples": []}
            else:
                payload = {"kind": "word", "brief_translation": "番茄" if tomato else "日程",
                           "brief_usage_zh": "番茄是可数名词，常作食物。" if tomato else "可数名词，表示时间安排。",
                           "details_zh": "模拟学习解析", "part_of_speech": "n.", "examples": []}
                payload.update(phonetic_us="/təˈmeɪtoʊ/" if tomato else "/ˈskedʒuːl/",
                               phonetic_uk="/təˈmɑːtəʊ/" if tomato else "/ˈʃedjuːl/")
                if mode[0] == "invalid":
                    payload.pop("phonetic_uk")
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(payload)}}]})
        def mocked_service(text, kind, key, model, **kwargs):
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                return original_translate(text, kind, key, model, client=client, **kwargs)
        ui.translate = mocked_service
        ui.get_api_key = lambda: "mock-in-memory"
        try:
            word = enter_text("tomato")
            wait_until(lambda: not window.workers)
            output = window.output.toPlainText()
            results["both_dialects_visible_in_compact_card"] = "美式 /təˈmeɪtoʊ/" in output and "英式 /təˈmɑːtəʊ/" in output and not window.details_open
            results["compact_word_shows_pos_and_direct_translation"] = "词性：n." in output and "番茄" in output
            results["compact_word_hides_usage_note"] = "番茄是可数名词" not in output and "用法补充" not in output
            results["phonetics_arrive_in_one_validated_translation_request"] = len(calls) == 1
            window.grab().save(str(root / "gui-first-phonetics.png"))
            window.toggle_details()
            results["phonetic_details_do_not_request_again"] = len(calls) == 1 and window.details_open
            results["usage_note_is_available_in_details"] = (
                "番茄是可数名词，常作食物。" in window.output.toPlainText())
            enter_text("TOMATO")
            results["complete_phonetics_are_cached_with_duplicate_count"] = len(calls) == 1 and window.store.card(word["id"])["seen_count"] == 2
            old = window.store.submit("schedule", "word")
            previous = {"kind": "word", "brief_translation_zh": "我的日程译文", "brief_usage_zh": "我的用法笔记",
                        "details_zh": "我的详细笔记", "part_of_speech": "n.", "examples": []}
            window.store.save_analysis(old["id"], previous)
            window.show_card(old["id"], fill_input=True)
            results["opening_legacy_word_makes_no_api_request"] = len(calls) == 1 and "再次按 Enter" in window.output.toPlainText()
            for failure_mode in ("failure", "invalid"):
                mode[0] = failure_mode
                enter_text("schedule")
                wait_until(lambda: not window.workers)
                row = window.store.card(old["id"])
                results[f"phonetic_{failure_mode}_preserves_old_analysis"] = json.loads(row["analysis_json"]) == previous and bool(row["error"])
            mode[0] = "success"
            enter_text("schedule")
            wait_until(lambda: not window.workers)
            row = window.store.card(old["id"])
            saved = json.loads(row["analysis_json"])
            results["legacy_phonetic_backfill_preserves_user_notes"] = all(saved[k] == value for k, value in previous.items()) and saved["phonetic_us"] == "/ˈskedʒuːl/" and saved["phonetic_uk"] == "/ˈʃedjuːl/" and row["error"] is None
            results["legacy_backfill_keeps_one_card_and_encounter_history"] = len(window.store.search("schedule")) == 1 and row["seen_count"] == 4
            window.grab().save(str(root / "gui-first-phonetic-backfill.png"))
            before = len(calls)
            enter_text("schedule")
            results["backfill_is_not_repeated_after_success"] = len(calls) == before
            enter_text("We like tomatoes.")
            wait_until(lambda: not window.workers)
            output = window.output.toPlainText()
            results["sentence_translation_needs_no_phonetics"] = "美式" not in output and "英式" not in output and "音标尚未补全" not in output and "翻译已保存" in output
            results["compact_sentence_shows_only_complete_direct_translation"] = (
                "我们喜欢番茄。" in output and "整句使用一般现在时" not in output
                and "词性：" not in output and "用法补充" not in output)
            before_details = len(calls)
            window.toggle_details()
            results["sentence_usage_note_is_available_in_details"] = (
                len(calls) == before_details and "整句使用一般现在时" in window.output.toPlainText())
            window.toggle_details()
        finally:
            ui.translate = original_translate
            ui.get_api_key = lambda: None

    def verify_mouse_selection():
        original_copy = ui.copy_selected_text
        original_translate = ui.translate
        replies = [("text", "mouse-side-apple"), ("empty", ""),
                   ("error", ui.SelectionError("没有检测到选中的文字，请先拖选文字再按侧键。"))]

        def fake_copy(target_hwnd=None):
            kind, value = replies.pop(0)
            if kind == "error":
                raise value
            return value

        def fake_translate(text, kind, *args, **kwargs):
            return {"kind": kind, "brief_translation_zh": "侧键译文", "brief_usage_zh": "侧键选词用法。",
                    "details_zh": "侧键选词详细解析。", "part_of_speech": "n." if kind == "word" else "",
                    "phonetic_us": "/saɪd/", "phonetic_uk": "/saɪd/",
                    "examples": [{"en": "A mouse side button.", "zh": "一个鼠标侧键。"}]}

        ui.copy_selected_text = fake_copy
        ui.translate = fake_translate
        ui.get_api_key = lambda: "mock-in-memory"
        try:
            window.collapse()
            before = window.store.db.execute("SELECT count(*) FROM encounters").fetchone()[0]
            window.mouse_hook.activated.emit(0)
            wait_until(lambda: current_original() == "mouse-side-apple" and not window.selection_workers and not window.workers)
            card = window.store.card(window.current_id)
            results["mouse_side_button_submits_selected_text"] = card["original"] == "mouse-side-apple"
            results["mouse_side_button_records_one_encounter"] = window.store.db.execute("SELECT count(*) FROM encounters").fetchone()[0] == before + 1
            results["mouse_side_button_uses_background_translation"] = bool(card["analysis_json"]) and "侧键译文" in window.output.toPlainText()
            results["mouse_side_button_keeps_result_visible"] = window.expanded and window.shortcut_grace_active and window.panel.isVisible()
            window.grab().save(str(root / f"gui-{stage}-mouse-selection.png"))

            before = window.store.db.execute("SELECT count(*) FROM encounters").fetchone()[0]
            window.mouse_hook.activated.emit(0)
            wait_until(lambda: not window.selection_workers and not window.selection_busy)
            results["empty_selection_does_not_create_encounter"] = window.store.db.execute("SELECT count(*) FROM encounters").fetchone()[0] == before
            results["empty_selection_has_clear_status"] = "没有读取到选中的文字" in window.status_text

            window.mouse_hook.activated.emit(0)
            wait_until(lambda: not window.selection_workers and not window.selection_busy)
            results["selection_copy_failure_is_explained"] = "没有检测到选中的文字" in window.status_text
            results["side_button_settings_include_off_one_and_both"] = list(ui.MOUSE_SIDE_BUTTONS) == ["off", "x1", "x2", "both"]
            results["mouse_side_hook_covers_both_buttons"] = (
                window.mouse_hook.installed and window.mouse_hook.buttons == {1, 2})
        finally:
            ui.copy_selected_text = original_copy
            ui.translate = original_translate
            ui.get_api_key = lambda: None

    def verify_bilingual_library():
        original_translate = ui.translate
        calls = []
        fail_next = [False]

        def fake_translate(text, kind, *args, translation_type="en-zh"):
            calls.append((text, translation_type))
            if fail_next[0]:
                fail_next[0] = False
                raise ui.ApiError("模拟中译英失败，原文已保留。")
            translated = "Hello!" if text == "你好" else "好的" if translation_type == "en-zh" else "OK"
            return {"kind": kind, "brief_translation": translated, "brief_usage_zh": "用于日常交流。", "details_zh": "根据语境使用。", "part_of_speech": "n." if kind == "word" else "",
                    "phonetic_us": "/ˌoʊˈkeɪ/" if kind == "word" else "", "phonetic_uk": "/ˌəʊˈkeɪ/" if kind == "word" else "", "examples": []}
        ui.translate = fake_translate
        ui.get_api_key = lambda: "mock-in-memory"
        try:
            results["default_direction_is_english_to_chinese"] = window.translation_type == "en-zh"
            button = window.input.type_button
            results["direction_control_is_inside_input_bottom_right"] = button.parentWidget() is window.input and button.x() + button.width() <= window.input.width() and button.y() + button.height() <= window.input.height() and button.y() >= window.input.height() // 2 and "⌃" in button.text()
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            app.processEvents()
            results["direction_popup_has_two_choices"] = window.input.type_menu.isVisible() and [a.text() for a in window.input.type_menu.actions()] == ["英译中", "中译英"]
            window.input.type_menu.grab().save(str(root / "gui-first-directions.png"))
            window.input.type_menu.hide()
            first = enter_text("OK")
            wait_until(lambda: not window.workers)
            window.input.type_menu.actions()[1].trigger()
            before = len(calls)
            results["direction_selection_does_not_call_api"] = window.translation_type == "zh-en" and len(calls) == before
            second = enter_text("OK")
            wait_until(lambda: not window.workers)
            results["same_original_in_two_directions_is_separate"] = first["id"] != second["id"] and "OK" in window.output.toPlainText()
            results["chinese_to_english_completion_resets_direction"] = (
                window.translation_type == "en-zh" and "英译中" in window.input.type_button.text()
                and "已切回英译中" in window.status_text)
            window.input.type_menu.actions()[1].trigger()
            fail_next[0] = True
            failed = enter_text("中译英失败测试")
            wait_until(lambda: not window.workers)
            results["chinese_to_english_failure_keeps_direction"] = (
                window.translation_type == "zh-en" and window.store.card(failed["id"])["analysis_json"] is None
                and "中译英" in window.input.type_button.text())
            window.store.delete_many([failed["id"]])
            window.current_id = None
            window.input.type_menu.actions()[1].trigger()
            hello = enter_text("你好")
            wait_until(lambda: not window.workers)
            results["chinese_input_translates_to_english"] = window.store.card(hello["id"])["translation_type"] == "zh-en" and "Hello!" in window.output.toPlainText() and calls[-1] == ("你好", "zh-en")
            results["hello_completion_resets_direction"] = window.translation_type == "en-zh"
            window.input.type_menu.actions()[1].trigger()
            before_cached = len(calls)
            enter_text("你好")
            results["cached_chinese_to_english_completion_resets_direction"] = (
                window.translation_type == "en-zh" and len(calls) == before_cached)
            window.grab().save(str(root / "gui-first-chinese-to-english.png"))
            disposable = [window.store.submit(text, "sentence", translation_type="zh-en")["id"] for text in ("待删除甲", "待删除乙")]
            library = LibraryDialog(window)
            library.translation_type.setCurrentIndex(2)
            library.show()
            app.processEvents()
            results["library_filters_by_translation_direction"] = library.list.count() == 4 and all("[中译英]" in library.list.item(i).text() for i in range(library.list.count()))
            for i in range(library.list.count()):
                item = library.list.item(i)
                item.setSelected(item.data(Qt.ItemDataRole.UserRole) in disposable)
            def confirm_delete():
                modal = app.activeModalWidget()
                if isinstance(modal, QMessageBox):
                    QTest.mouseClick(modal.button(QMessageBox.StandardButton.Yes), Qt.MouseButton.LeftButton)
            QTimer.singleShot(50, confirm_delete)
            QTest.mouseClick(library.delete_button, Qt.MouseButton.LeftButton)
            results["library_batch_delete_removes_selected_only"] = library.list.count() == 2 and all(window.store.card(cid) is None for cid in disposable) and window.store.card(hello["id"]) is not None
            results["library_delete_removes_related_encounters"] = not window.store.db.execute("SELECT 1 FROM encounters WHERE card_id IN (?,?)", disposable).fetchone()
            library.grab().save(str(root / "gui-first-library.png"))
            filename = root / "library-中译英.md"
            original_picker = ui.QFileDialog.getSaveFileName
            original_information = ui.QMessageBox.information
            try:
                ui.QFileDialog.getSaveFileName = lambda *args, **kwargs: (str(filename), "")
                ui.QMessageBox.information = lambda *args, **kwargs: QMessageBox.StandardButton.Ok
                export_button = next(b for b in library.findChildren(QPushButton) if b.text() == "导出当前列表")
                QTest.mouseClick(export_button, Qt.MouseButton.LeftButton)
            finally:
                ui.QFileDialog.getSaveFileName = original_picker
                ui.QMessageBox.information = original_information
            report = filename.read_text(encoding="utf-8")
            results["library_export_respects_direction_filter"] = "| 你好 | 2 | Hello! |" in report and "苹果" not in report
            results["export_has_only_original_count_translation"] = report.splitlines()[0] == "| 原文 | 次数 | 翻译 |" and all(word not in report for word in ("日期", "详细解析", "简要用法", "AI 生成例句"))
            library.close()
        finally:
            ui.translate = original_translate
            ui.get_api_key = lambda: None

    def verify_single_instance():
        latest = window.store.latest_card()
        old = window.store.search("shortcut-initial")[0]
        window.show_card(old["id"], fill_input=True)
        count = window.store.db.execute("SELECT count(*) FROM encounters").fetchone()[0]
        command = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, str(Path(__file__).resolve().parent.parent / "main.py")]
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ.copy())
        try:
            wait_until(lambda: child.poll() is not None and window.current_id == latest["id"])
            results["second_launch_activates_latest_without_duplicate_process"] = child.returncode == 0
            results["second_launch_does_not_submit"] = count == window.store.db.execute("SELECT count(*) FROM encounters").fetchone()[0]
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=3)

    def verify_layout():
        area = window.screen().availableGeometry()
        for edge in ("left", "right", "top"):
            x = area.left() if edge == "left" else area.right() - window.width() + 1 if edge == "right" else area.center().x() - window.width() // 2
            y = area.top() if edge == "top" else area.center().y() - window.height() // 2
            window.move(x, y)
            window.snap()
            results[f"snap_{edge}"] = window.settings["edge"] == edge and area.contains(window.geometry())
        actual_screen = window.screen
        class SmallScreen:
            def availableGeometry(self):
                return QRect(0, 0, 350, 300)
        window.screen = lambda: SmallScreen()
        window.reposition()
        app.processEvents()
        results["small_available_area_simulation"] = window.width() <= 350 and window.height() <= 300
        window.screen = actual_screen
        window.settings["edge"] = "right"
        window.reposition()
        window.collapse()
        window.enterEvent(QEnterEvent(QPointF(1, 1), QPointF(1, 1), QPointF(window.pos())))
        QTest.qWait(250)
        results["hover_timer_expands"] = window.expanded

    def verify_collapse():
        area = window.screen().availableGeometry()
        outside = area.center()

        def focus_inside(widget):
            window.expand()
            window.activateWindow()
            QCursor.setPos(widget.mapToGlobal(widget.rect().center()))
            widget.setFocus()
            QTest.qWait(150)
            assert widget.hasFocus()

        def leave_panel():
            for _ in range(10):
                QCursor.setPos(outside)
                QTest.qWait(100)
                if not window.underMouse():
                    return
            assert not window.underMouse(), "Qt still reports the pointer inside after native movement"

        def wait_for_tile():
            wait_until(lambda: not window.expanded)
            assert window.tile.isVisible() and not window.panel.isVisible(), "tile/panel visibility"
            assert (window.width(), window.height()) == (28, 52), "collapsed size"

        window.pinned = False
        window.show_latest_record()
        focus_inside(window.input)
        results["saved_input_has_no_unsent_edit"] = not window.input.document().isModified()
        leave_panel()
        wait_for_tile()
        results["focused_saved_input_collapses_after_pointer_leaves"] = True
        window.grab().save(str(root / f"gui-{stage}-auto-collapsed.png"))

        # Waking outside the window produces no new mouse-leave event.
        window.show_latest_record()
        assert window.expanded
        wait_for_tile()
        results["shortcut_wake_outside_rearms_auto_collapse"] = True

        window.show_latest_record()
        window.toggle_details()
        focus_inside(window.output)
        assert window.details_open
        leave_panel()
        wait_for_tile()
        results["expanded_details_and_output_focus_do_not_pin_window"] = True

        window.show_latest_record()
        focus_inside(window.input)
        window.input.moveCursor(QTextCursor.MoveOperation.End)
        QTest.keyClicks(window.input, " unsent-draft")
        draft = window.input.toPlainText()
        leave_panel()
        QTest.qWait(900)
        results["active_unsent_edit_prevents_auto_collapse"] = window.expanded and window.input.document().isModified()
        window.output.setFocus()
        wait_for_tile()
        window.expand()
        results["draft_survives_collapse_and_reopen"] = window.input.toPlainText() == draft
        window.show_latest_record()

        focus_inside(window.input)
        window.toggle_pin()
        leave_panel()
        QTest.qWait(900)
        results["explicit_pin_keeps_panel_open"] = window.expanded
        window.toggle_pin()
        wait_for_tile()
        results["unpin_outside_rearms_auto_collapse"] = True

        window.show_latest_record()
        focus_inside(window.input)
        window.input.show_type_menu()
        leave_panel()
        QTest.qWait(900)
        results["active_popup_prevents_auto_collapse"] = window.expanded and window.input.type_menu.isVisible()
        window.input.type_menu.hide()
        wait_for_tile()
        results["popup_close_resumes_auto_collapse_without_mouse_move"] = True

        window.show_latest_record()
        leave_panel()
        dialog = QMessageBox(QMessageBox.Icon.Information, "自动收起回归", "隔离测试对话框", QMessageBox.StandardButton.Ok, window)
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.open()
        QTest.qWait(900)
        results["active_dialog_prevents_auto_collapse"] = window.expanded and dialog.isVisible()
        dialog.accept()
        wait_for_tile()
        results["dialog_close_resumes_auto_collapse_without_mouse_move"] = True

        # Exercise an actual background worker finishing after auto-collapse.
        original_translate = ui.translate
        latest = window.store.latest_card()
        analysis = json.loads(latest["analysis_json"])
        def delayed_translate(*args, **kwargs):
            time.sleep(1.5)
            return analysis
        ui.translate = delayed_translate
        ui.get_api_key = lambda: "mock-in-memory"
        try:
            window.expand()
            pending = enter_text("collapse-background-qa")
            wait_for_tile()
            wait_until(lambda: not window.workers)
            results["background_completion_keeps_collapsed_tile"] = not window.expanded and bool(window.store.card(pending["id"])["analysis_json"])
        finally:
            ui.translate = original_translate
            ui.get_api_key = lambda: None
            window.store.delete(pending["id"])
            window.current_id = latest["id"]
            window.show_latest_record()

        # A successfully submitted edit is no longer a draft, even with focus.
        focus_inside(window.input)
        window.input.moveCursor(QTextCursor.MoveOperation.End)
        QTest.keyClick(window.input, Qt.Key.Key_Space)
        QTest.keyClick(window.input, Qt.Key.Key_Return)
        results["submission_clears_unsent_edit_state"] = not window.input.document().isModified()
        leave_panel()
        wait_for_tile()
        results["submitted_input_focus_does_not_block_collapse"] = True

        # Native hover still restores the panel from the 28x52 tile.
        QCursor.setPos(outside)
        QTest.qWait(150)
        QCursor.setPos(window.tile.mapToGlobal(window.tile.rect().center()))
        QTest.qWait(50)
        window.tile.entered.emit()
        wait_until(lambda: window.expanded)
        results["native_pointer_hover_reopens_collapsed_tile"] = True
        leave_panel()
        wait_for_tile()

    def check():
        try:
            app.processEvents()
            if stage == "first":
                notices = []
                timer = QTimer()
                def accept_notice():
                    modal = app.activeModalWidget()
                    if isinstance(modal, QMessageBox):
                        notices.append(modal.windowTitle())
                        modal.grab().save(str(root / f"gui-first-notice-{len(notices)}.png"))
                        modal.accept()
                timer.timeout.connect(accept_notice)
                timer.start(30)
                first_launch(window)
                timer.stop()
                results["onboarding_notice_visible"] = "欢迎使用 WordBook" in notices
                results["shortcut_conflict_notice_visible"] = window.hotkey_ok or "快捷键冲突" in notices
            results["tile_visible"] = window.tile.isVisible() and window.width() <= 40
            window.grab().save(str(root / f"gui-{stage}-tile.png"))
            get_style = USER32.GetWindowLongPtrW
            get_style.argtypes = [ctypes.c_void_p, ctypes.c_int]
            get_style.restype = ctypes.c_ssize_t
            style = get_style(int(window.winId()), -20)
            results["toolwindow_has_no_taskbar_appwindow_style"] = bool(style & 0x80) and not bool(style & 0x40000)
            # Keep unrelated flows stable while the cursor is outside;
            # auto-collapse is exercised separately with native cursor movement.
            window.pinned = True
            window.expand()
            results["floating_panel_has_one_settings_button"] = [b.text() for b in window.panel.findChildren(QPushButton)] == ["设置"]
            results["no_review_or_retry_widgets"] = not any(term in window.output.toPlainText() for term in ("开始复习", "今天已完成", "重试翻译", "重新解析"))
            window.settings_button.enterEvent(QEnterEvent(QPointF(1, 1), QPointF(1, 1), QPointF(window.pos())))
            app.processEvents()
            results["hover_opens_six_actions"] = window.action_menu.isVisible() and [a.text() for a in window.action_menu.actions()] == ["修改", "删除", "词库", "设置", "7 天导出", "30 天导出"]
            window.action_menu.grab().save(str(root / f"gui-{stage}-menu.png"))
            window.action_menu.hide()
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(root / "not-an-input-file.txt"))])
            drag = QDragEnterEvent(QPoint(1, 1), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            window.tile.dragEnterEvent(drag)
            results["file_drag_rejected_with_clear_hint"] = not drag.isAccepted() and "不接受文件" in window.status_text
            window.close()
            app.processEvents()
            results["closing_collapses_and_keeps_store_open"] = window.tile.isVisible() and not window.expanded and window.store.db.execute("SELECT 1").fetchone()[0] == 1
            window.expand()
            if stage == "first":
                card = enter_text("smoketestword")
                results["enter_submits_without_inserting_newline"] = card["original"] == "smoketestword" and window.input.toPlainText() == "smoketestword"
                results["first_saved_pending"] = card["analysis_json"] is None and card["seen_count"] == 1
                enter_text("smoketestword")
                results["duplicate_one_card_two_events"] = window.store.card(card["id"])["seen_count"] == 2
                count = window.store.db.execute("SELECT count(*) FROM encounters").fetchone()[0]
                window.input.setPlainText("line-one")
                window.input.moveCursor(QTextCursor.MoveOperation.End)
                QTest.keyClick(window.input, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
                QTest.keyClicks(window.input, "line-two")
                results["shift_enter_inserts_newline_without_submit"] = window.input.toPlainText() == "line-one\nline-two" and count == window.store.db.execute("SELECT count(*) FROM encounters").fetchone()[0]
                verify_native_hotkey()
                verify_translation()
                verify_mouse_selection()
                verify_phonetics()
                verify_single_instance()
                verify_layout()
                verify_bilingual_library()
                window.show_latest_record()
            else:
                pending = window.store.search("smoketestword")[0]
                results["reopened_pending_card"] = pending["analysis_json"] is None and pending["seen_count"] == 2
                results["restart_defaults_to_english_to_chinese"] = (
                    window.translation_type == "en-zh" and "英译中" in window.input.type_button.text())
                window.show_latest_record()
                latest = window.store.latest_card()
                results["restart_restores_latest_translation"] = window.current_id == latest["id"] and window.input.toPlainText() == "你好" and "Hello!" in window.output.toPlainText()
                results["opening_historical_chinese_card_shows_its_direction"] = (
                    window.translation_type == "zh-en" and "中译英" in window.input.type_button.text())
                for text, us, uk in (("tomato", "/təˈmeɪtoʊ/", "/təˈmɑːtəʊ/"), ("schedule", "/ˈskedʒuːl/", "/ˈʃedjuːl/")):
                    word = window.store.search(text, kind="word", translation_type="en-zh")[0]
                    window.show_card(word["id"], fill_input=True)
                    output = window.output.toPlainText()
                    results[f"restart_preserves_{text}_phonetics"] = f"美式 {us}" in output and f"英式 {uk}" in output
                    window.grab().save(str(root / f"gui-reopen-{text}-phonetics.png"))
                window.show_latest_record()
            window.grab().save(str(root / f"gui-{stage}-result.png"))
            verify_collapse()
            optional = {"stage", "version", "qt_platform", "screen", "screen_area", "hotkey_registered",
                        "tested_shortcut"}
            results["ok"] = all(value for key, value in results.items() if key not in optional)
        except Exception as exc:
            results["ok"] = False
            results["error"] = repr(exc)
            results["error_trace"] = traceback.format_exc()
            results["failure_state"] = {"expanded": window.expanded, "under_mouse": window.underMouse(),
                                        "geometry": list(window.geometry().getRect()),
                                        "pointer": [QCursor.pos().x(), QCursor.pos().y()],
                                        "hover_timer_active": window.hover_timer.isActive(),
                                        "leave_timer_active": window.leave_timer.isActive()}
            window.grab().save(str(root / f"gui-{stage}-failure.png"))
        finally:
            QCursor.setPos(saved_pointer)
            app.clipboard().setMimeData(saved_clipboard)
            ctypes.WinDLL("ole32").OleFlushClipboard()
            window.tray.contextMenu().actions()[-1].trigger()

    QTimer.singleShot(300, check)
    app.exec()
    results["tray_exit_returned"] = (not window.tray.isVisible() and not window.hotkey.active
                                     and not window.mouse_hook.installed)
    results["ok"] = results.get("ok", False) and results["tray_exit_returned"]
    (root / f"gui-smoke-{stage}.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if results.get("ok") else 1
