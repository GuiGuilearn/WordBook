"""Drive a real XBUTTON event into a running WordBook with isolated test data."""
import argparse
import ctypes
import json
import os
import sqlite3
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QMimeData
from PySide6.QtWidgets import QApplication


PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))
USER32 = ctypes.WinDLL("user32", use_last_error=True)
OLE32 = ctypes.WinDLL("ole32")
INPUT_MOUSE = 0
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
XBUTTON1 = 0x0001
XBUTTON2 = 0x0002
SW_RESTORE = 9
HWND_TOPMOST = -1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", INPUTUNION)]


USER32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
USER32.FindWindowW.restype = ctypes.c_void_p
USER32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
USER32.SetForegroundWindow.restype = wintypes.BOOL
USER32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
USER32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
USER32.GetForegroundWindow.restype = ctypes.c_void_p
USER32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
USER32.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
USER32.ClientToScreen.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.POINT)]
USER32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, ctypes.c_uint]
USER32.GetSystemMetrics.argtypes = [ctypes.c_int]
USER32.GetSystemMetrics.restype = ctypes.c_int
USER32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
USER32.SendInput.restype = wintypes.UINT
OLE32.OleFlushClipboard.restype = ctypes.c_long


def send_mouse(flags, mouse_data=0, dx=0, dy=0):
    item = INPUT()
    item.type = INPUT_MOUSE
    item.mi.dx = dx
    item.mi.dy = dy
    item.mi.dwFlags = flags
    item.mi.mouseData = mouse_data
    sent = USER32.SendInput(1, ctypes.byref(item), ctypes.sizeof(INPUT))
    if sent != 1:
        raise RuntimeError(f"SendInput failed: {ctypes.get_last_error()}")


def move_absolute(x, y):
    left = USER32.GetSystemMetrics(76)
    top = USER32.GetSystemMetrics(77)
    width = max(1, USER32.GetSystemMetrics(78))
    height = max(1, USER32.GetSystemMetrics(79))
    dx = round((x - left) * 65535 / max(1, width - 1))
    dy = round((y - top) * 65535 / max(1, height - 1))
    send_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, dx=dx, dy=dy)


def wait_window(title, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        hwnd = int(USER32.FindWindowW(None, title) or 0)
        if hwnd:
            return hwnd
        time.sleep(0.05)
    return 0


def activate_window(hwnd):
    USER32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    USER32.ShowWindow(hwnd, SW_RESTORE)
    for _ in range(5):
        USER32.SetForegroundWindow(hwnd)
        rectangle = wintypes.RECT()
        USER32.GetWindowRect(hwnd, ctypes.byref(rectangle))
        USER32.SetCursorPos((rectangle.left + rectangle.right) // 2, rectangle.top + 40)
        send_mouse(MOUSEEVENTF_LEFTDOWN)
        send_mouse(MOUSEEVENTF_LEFTUP)
        time.sleep(0.2)
        if int(USER32.GetForegroundWindow() or 0) == hwnd:
            return True
    return int(USER32.GetForegroundWindow() or 0) == hwnd


def save_clipboard():
    app = QApplication.instance() or QApplication([])
    mime = app.clipboard().mimeData()
    saved = QMimeData()
    if mime:
        for fmt in mime.formats():
            saved.setData(fmt, mime.data(fmt))
    return app, saved


def wait_for_row(database, original, timeout=8):
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
            try:
                row = connection.execute(
                    "SELECT id, original, seen_count, analysis_json, error FROM cards WHERE original = ?",
                    (original,),
                ).fetchone()
                if row:
                    events = connection.execute(
                        "SELECT count(*) FROM encounters WHERE card_id = ?", (row[0],)
                    ).fetchone()[0]
                    return {
                        "ok": True,
                        "card_id": row[0],
                        "original": row[1],
                        "seen_count": row[2],
                        "analysis_saved": bool(row[3]),
                        "error": row[4],
                        "encounter_count": events,
                    }
            finally:
                connection.close()
        except (OSError, sqlite3.Error) as exc:
            last_error = str(exc)
        time.sleep(0.1)
    return {"ok": False, "timeout_error": last_error}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--text", default="sidebuttonprobe")
    parser.add_argument("--button", choices=("x1", "x2"), default="x1")
    args = parser.parse_args()
    data_dir = Path(args.data_dir).resolve()
    database = data_dir / "wordbook.sqlite3"
    result = {"ok": False, "text": args.text, "button": args.button}
    app, saved_clipboard = save_clipboard()
    target = None
    try:
        target_python = Path(sys.executable).with_name("pythonw.exe")
        if not target_python.exists():
            target_python = Path(sys.executable)
        target = subprocess.Popen(
            [str(target_python), str(PROJECT / "tools" / "selection_target.py"), "--text", args.text],
            cwd=PROJECT,
            env=os.environ.copy(),
        )
        hwnd = wait_window("WordBook Selection Probe Target")
        result["target_window"] = hwnd
        if not hwnd:
            raise RuntimeError("target window did not appear")
        if not activate_window(hwnd):
            raise RuntimeError("target window could not become foreground")
        time.sleep(0.5)
        result["foreground_before_double_click"] = int(USER32.GetForegroundWindow() or 0)

        # The low-level hook only accepts a side button after a real mouse selection.
        rectangle = wintypes.RECT()
        client = wintypes.RECT()
        origin = wintypes.POINT(0, 0)
        USER32.GetWindowRect(hwnd, ctypes.byref(rectangle))
        USER32.GetClientRect(hwnd, ctypes.byref(client))
        USER32.ClientToScreen(hwnd, ctypes.byref(origin))
        start_x = origin.x + 8
        end_x = origin.x + max(30, client.right - 20)
        drag_y = origin.y + min(30, max(8, client.bottom - 8))
        result["target_rect"] = [rectangle.left, rectangle.top, rectangle.right, rectangle.bottom]
        result["drag_points"] = [start_x, drag_y, end_x, drag_y]
        move_absolute(start_x, drag_y)
        time.sleep(0.05)
        send_mouse(MOUSEEVENTF_LEFTDOWN)
        for x in range(start_x + 8, end_x + 1, 30):
            move_absolute(min(x, end_x), drag_y)
            time.sleep(0.025)
        send_mouse(MOUSEEVENTF_LEFTUP)
        time.sleep(0.35)
        result["target_foreground_after_drag"] = int(USER32.GetForegroundWindow() or 0) == hwnd
        if not result["target_foreground_after_drag"]:
            raise RuntimeError("target lost foreground during mouse selection")

        started = time.monotonic()
        send_mouse(MOUSEEVENTF_XDOWN, XBUTTON1 if args.button == "x1" else XBUTTON2)
        send_mouse(MOUSEEVENTF_XUP, XBUTTON1 if args.button == "x1" else XBUTTON2)
        result["row"] = wait_for_row(database, args.text)
        result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        result["foreground_after_side_button"] = int(USER32.GetForegroundWindow() or 0)
        app.processEvents()
        clipboard_text = app.clipboard().text()
        result["clipboard_contains_selection"] = clipboard_text.strip() == args.text
        result["ok"] = bool(result["row"].get("ok")) and result["clipboard_contains_selection"]
    except Exception as exc:
        result["error"] = repr(exc)
    finally:
        if target and target.poll() is None:
            target.terminate()
            try:
                target.wait(timeout=3)
            except subprocess.TimeoutExpired:
                target.kill()
        try:
            app.clipboard().setMimeData(saved_clipboard)
            OLE32.OleFlushClipboard()
        except Exception:
            pass
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
