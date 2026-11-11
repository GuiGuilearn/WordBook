import ctypes
from types import SimpleNamespace
from ctypes import wintypes

import pytest

import wordbook.platform_win as platform_win
from wordbook.platform_win import INPUT, MouseSideHook, SelectionError, copy_selected_text


def mouse_info(x=10, y=10, button=0):
    return SimpleNamespace(pt=wintypes.POINT(x, y), mouseData=button << 16)


def drag_select(hook):
    hook._observe(platform_win.WM_LBUTTONDOWN, mouse_info(10, 10))
    hook._observe(platform_win.WM_MOUSEMOVE, mouse_info(40, 10))
    hook._observe(platform_win.WM_LBUTTONUP, mouse_info(40, 10))


def test_mouse_side_button_only_consumes_a_fresh_mouse_selection():
    hook = MouseSideHook()
    hook._buttons = {platform_win.XBUTTON1, platform_win.XBUTTON2}

    assert hook._observe(platform_win.WM_XBUTTONDOWN, mouse_info(button=1)) is None
    drag_select(hook)
    assert hook._observe(platform_win.WM_XBUTTONDOWN, mouse_info(button=1)) == "trigger"
    assert hook._observe(platform_win.WM_XBUTTONUP, mouse_info(button=1)) == "swallow"
    assert hook._observe(platform_win.WM_XBUTTONDOWN, mouse_info(button=1)) is None


def test_single_click_clears_selection_candidate_and_other_button_passes():
    hook = MouseSideHook()
    hook._buttons = {platform_win.XBUTTON1}
    drag_select(hook)
    hook._observe(platform_win.WM_LBUTTONDOWN, mouse_info(100, 100))
    hook._observe(platform_win.WM_LBUTTONUP, mouse_info(100, 100))
    assert hook._observe(platform_win.WM_XBUTTONDOWN, mouse_info(button=1)) is None
    assert hook._observe(platform_win.WM_XBUTTONDOWN, mouse_info(button=2)) is None


def test_side_button_does_not_read_another_foreground_window(monkeypatch):
    hook = MouseSideHook()
    hook._buttons = {platform_win.XBUTTON1}
    monkeypatch.setattr(platform_win, "foreground_window", lambda: 101)
    drag_select(hook)
    monkeypatch.setattr(platform_win, "foreground_window", lambda: 202)
    assert hook._observe(platform_win.WM_XBUTTONDOWN, mouse_info(button=1)) is None


def test_double_click_selects_a_word_for_side_button_capture():
    hook = MouseSideHook()
    hook._buttons = {platform_win.XBUTTON2}
    for _ in range(2):
        hook._observe(platform_win.WM_LBUTTONDOWN, mouse_info(50, 50))
        hook._observe(platform_win.WM_LBUTTONUP, mouse_info(50, 50))
    assert hook._observe(platform_win.WM_XBUTTONDOWN, mouse_info(button=2)) == "trigger"


def test_triple_click_remains_a_valid_selection():
    hook = MouseSideHook()
    hook._buttons = {platform_win.XBUTTON1}
    for _ in range(3):
        hook._observe(platform_win.WM_LBUTTONDOWN, mouse_info(50, 50))
        hook._observe(platform_win.WM_LBUTTONUP, mouse_info(50, 50))
    assert hook._observe(platform_win.WM_XBUTTONDOWN, mouse_info(button=1)) == "trigger"


def test_ctrl_c_input_uses_expected_native_structure(monkeypatch):
    sent = []

    class FakeUser32:
        @staticmethod
        def SendInput(count, pointer, size):
            sent.append((count, size, ctypes.string_at(pointer, count * size)))
            return count

    monkeypatch.setattr(platform_win, "USER32", FakeUser32)
    platform_win.send_ctrl_c()
    assert ctypes.sizeof(INPUT) == 40
    assert sent[0][0:2] == (4, ctypes.sizeof(INPUT))
    inputs = (INPUT * 4).from_buffer_copy(sent[0][2])
    assert [item.ki.wVk for item in inputs] == [
        platform_win.VK_CONTROL, platform_win.VK_C, platform_win.VK_C, platform_win.VK_CONTROL
    ]
    assert [item.ki.dwFlags for item in inputs] == [
        0, 0, platform_win.KEYEVENTF_KEYUP, platform_win.KEYEVENTF_KEYUP
    ]


def test_copy_selected_text_reads_only_the_fresh_clipboard_copy(monkeypatch):
    state = {"sequence": 10}

    monkeypatch.setattr(platform_win, "foreground_window", lambda: 101)
    monkeypatch.setattr(platform_win, "current_process_id", lambda: 202)
    monkeypatch.setattr(platform_win, "process_id_for_window", lambda hwnd: 303)
    monkeypatch.setattr(platform_win, "clipboard_sequence", lambda: state["sequence"])
    monkeypatch.setattr(platform_win, "send_ctrl_c", lambda: state.update(sequence=11))
    monkeypatch.setattr(platform_win, "read_clipboard_snapshot", lambda: (state["sequence"], "  selected words  "))

    assert copy_selected_text(timeout_ms=50) == "selected words"


def test_copy_selected_text_rejects_no_copy_and_window_switch(monkeypatch):
    monkeypatch.setattr(platform_win, "current_process_id", lambda: 202)
    monkeypatch.setattr(platform_win, "process_id_for_window", lambda hwnd: 303)
    monkeypatch.setattr(platform_win, "clipboard_sequence", lambda: 10)
    monkeypatch.setattr(platform_win, "send_ctrl_c", lambda: None)
    monkeypatch.setattr(platform_win, "foreground_window", lambda: 101)
    with pytest.raises(SelectionError, match="没有检测到选中"):
        copy_selected_text(timeout_ms=30)

    monkeypatch.setattr(platform_win, "foreground_window", lambda: 999)
    with pytest.raises(SelectionError, match="目标窗口已经切换"):
        copy_selected_text(target_hwnd=101, timeout_ms=50)


def test_copy_selected_text_rejects_own_window(monkeypatch):
    monkeypatch.setattr(platform_win, "foreground_window", lambda: 101)
    monkeypatch.setattr(platform_win, "current_process_id", lambda: 202)
    monkeypatch.setattr(platform_win, "process_id_for_window", lambda hwnd: 202)
    with pytest.raises(SelectionError, match="其它程序"):
        copy_selected_text()
