import ctypes
import json
import os
import sys
import time
import winreg
import hashlib
from ctypes import wintypes
from pathlib import Path

import keyring
from keyring.backends.Windows import WinVaultKeyring
from PySide6.QtCore import QAbstractNativeEventFilter, QLockFile, QObject, QStandardPaths, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from .translations import TRANSLATION_TYPES

SERVICE = "WordBook DeepSeek API"
USER = "default"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
DEFAULT_SETTINGS = {"model": "deepseek-flash", "hotkey": "Ctrl+Alt+W", "autostart": True,
                    "screen": "", "edge": "right", "offset": 0.5, "onboarded": False, "translation_type": "en-zh",
                    "mouse_side_button": "both"}
MOUSE_SIDE_BUTTONS = {"off": "关闭", "x1": "侧键 1（后退）", "x2": "侧键 2（前进）", "both": "两个侧键"}
XLONG = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t

USER32 = ctypes.WinDLL("user32", use_last_error=True)
KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
USER32.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
USER32.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
USER32.GetClipboardSequenceNumber.restype = ctypes.c_uint
USER32.OpenClipboard.argtypes = [ctypes.c_void_p]
USER32.GetClipboardData.argtypes = [ctypes.c_uint]
USER32.GetClipboardData.restype = ctypes.c_void_p
KERNEL32.GlobalLock.argtypes = [ctypes.c_void_p]
KERNEL32.GlobalLock.restype = ctypes.c_void_p
KERNEL32.GlobalUnlock.argtypes = [ctypes.c_void_p]
KERNEL32.GlobalSize.argtypes = [ctypes.c_void_p]
KERNEL32.GlobalSize.restype = ctypes.c_size_t
KERNEL32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
KERNEL32.GetModuleHandleW.restype = ctypes.c_void_p
USER32.GetForegroundWindow.restype = ctypes.c_void_p
USER32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
USER32.GetWindowThreadProcessId.restype = wintypes.DWORD
KERNEL32.GetCurrentProcessId.restype = wintypes.DWORD
USER32.GetDoubleClickTime.restype = ctypes.c_uint
USER32.GetSystemMetrics.argtypes = [ctypes.c_int]
USER32.GetSystemMetrics.restype = ctypes.c_int
USER32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
USER32.SendInput.restype = wintypes.UINT

SM_CXDRAG = 68
SM_CYDRAG = 69
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_MOUSEMOVE = 0x0200
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C
XBUTTON1 = 0x0001
XBUTTON2 = 0x0002
VK_CONTROL = 0x11
VK_C = 0x43
KEYEVENTF_KEYUP = 0x0002


class SelectionError(RuntimeError):
    pass


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", INPUTUNION)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


def clipboard_sequence():
    return USER32.GetClipboardSequenceNumber()


def read_clipboard_snapshot():
    """Read fresh Unicode text only when the user invokes the shortcut."""
    if not USER32.OpenClipboard(None):
        raise RuntimeError("剪贴板暂时被占用，请稍后再按一次快捷键。")
    try:
        sequence = clipboard_sequence()
        if USER32.IsClipboardFormatAvailable(15) or not USER32.IsClipboardFormatAvailable(13):
            return sequence, None
        handle = USER32.GetClipboardData(13)
        address = KERNEL32.GlobalLock(handle) if handle else None
        if not address:
            raise RuntimeError("无法读取剪贴板文本，请重新复制后再试。")
        try:
            size = KERNEL32.GlobalSize(handle)
            text = ctypes.wstring_at(address, size // ctypes.sizeof(ctypes.c_wchar)).split("\0", 1)[0]
            return sequence, text
        finally:
            KERNEL32.GlobalUnlock(handle)
    finally:
        USER32.CloseClipboard()


def foreground_window():
    return int(USER32.GetForegroundWindow() or 0)


def current_process_id():
    return int(KERNEL32.GetCurrentProcessId())


def process_id_for_window(hwnd):
    process_id = wintypes.DWORD()
    if not hwnd or not USER32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(process_id)):
        return 0
    return int(process_id.value)


def send_ctrl_c():
    inputs = (INPUT * 4)()
    for item, key, flags in zip(inputs, (VK_CONTROL, VK_C, VK_C, VK_CONTROL),
                                (0, 0, KEYEVENTF_KEYUP, KEYEVENTF_KEYUP)):
        item.type = 1  # INPUT_KEYBOARD
        item.ki.wVk = key
        item.ki.dwFlags = flags
    sent = USER32.SendInput(len(inputs), ctypes.byref(inputs), ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise SelectionError("无法向当前窗口发送复制命令，请改用 Ctrl+Alt+W。")


def copy_selected_text(target_hwnd=None, timeout_ms=900):
    """Copy the foreground selection once, after an explicit side-button press."""
    hwnd = int(target_hwnd or foreground_window())
    if not hwnd:
        raise SelectionError("没有找到可读取选中文字的前台窗口。")
    if process_id_for_window(hwnd) == current_process_id():
        raise SelectionError("请先在其它程序中选中文字，再按鼠标侧键。")
    sequence = clipboard_sequence()
    if foreground_window() != hwnd:
        raise SelectionError("目标窗口已经切换，请重新选中文字后再按侧键。")
    send_ctrl_c()
    deadline = time.monotonic() + timeout_ms / 1000
    changed = False
    last_error = ""
    while time.monotonic() < deadline:
        if foreground_window() != hwnd:
            raise SelectionError("目标窗口已经切换，请重新选中文字后再按侧键。")
        current = clipboard_sequence()
        if current and current != sequence:
            changed = True
        if changed:
            try:
                current, text = read_clipboard_snapshot()
                sequence = current or sequence
                if text and text.strip():
                    # The selected text intentionally remains on the clipboard,
                    # matching a normal Ctrl+C and never reading clipboard in the background.
                    return text.strip()
                last_error = "没有读取到纯文本，请确认选区包含文字。"
            except RuntimeError as exc:
                last_error = str(exc)
        time.sleep(0.02)
    if not changed:
        raise SelectionError("没有检测到选中的文字，请先拖选文字再按侧键。")
    raise SelectionError(last_error or "没有读取到选中的文字，请重试。")


class SingleInstance(QObject):
    activation_requested = Signal()

    def __init__(self, root):
        super().__init__()
        name = "WordBook-" + hashlib.sha256(str(Path(root).resolve()).casefold().encode()).hexdigest()[:24]
        self.lock = QLockFile(str(Path(root) / "wordbook.lock"))
        self.lock.setStaleLockTime(0)
        self.server = None
        self.is_primary = self.lock.tryLock(100)
        if not self.is_primary:
            socket = QLocalSocket(self)
            socket.connectToServer(name)
            if not socket.waitForConnected(1500):
                raise RuntimeError("WordBook 已在运行或正在启动，请稍后通过托盘展开。")
            socket.write(b"activate\n")
            socket.waitForBytesWritten(500)
            socket.disconnectFromServer()
            return
        QLocalServer.removeServer(name)
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not self.server.listen(name):
            self.lock.unlock()
            raise RuntimeError("无法创建 WordBook 本机窗口通信。")
        self.server.newConnection.connect(self.accept_connection)

    def accept_connection(self):
        socket = self.server.nextPendingConnection()
        buffer = bytearray()
        def read_message():
            buffer.extend(bytes(socket.readAll()))
            if len(buffer) > 64 or b"\n" in buffer:
                if bytes(buffer) == b"activate\n":
                    self.activation_requested.emit()
                socket.disconnectFromServer()
                socket.deleteLater()
        socket.readyRead.connect(read_message)
        if socket.bytesAvailable():
            read_message()

    def close(self):
        if self.server:
            self.server.close()
            self.lock.unlock()


def sanitize_settings(obj):
    result = DEFAULT_SETTINGS.copy()
    if not isinstance(obj, dict):
        return result
    for name, maximum in (("model", 120), ("hotkey", 80), ("screen", 256)):
        value = obj.get(name)
        if isinstance(value, str) and len(value) <= maximum and (name == "screen" or value.strip()):
            result[name] = value.strip()
    for name in ("autostart", "onboarded"):
        if isinstance(obj.get(name), bool):
            result[name] = obj[name]
    if obj.get("edge") in ("left", "right", "top"):
        result["edge"] = obj["edge"]
    if obj.get("translation_type") in TRANSLATION_TYPES:
        result["translation_type"] = obj["translation_type"]
    if obj.get("mouse_side_button") in MOUSE_SIDE_BUTTONS:
        result["mouse_side_button"] = obj["mouse_side_button"]
    if isinstance(obj.get("offset"), (int, float)) and 0 <= obj["offset"] <= 1:
        result["offset"] = obj["offset"]
    try:
        hotkey_parts(result["hotkey"])
    except ValueError:
        result["hotkey"] = DEFAULT_SETTINGS["hotkey"]
    return result


def data_dir():
    override = os.environ.get("WORDBOOK_TEST_DATA_DIR")
    root = Path(override) if override else Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation))
    root.mkdir(parents=True, exist_ok=True)
    return root


def load_settings(root):
    path = root / "settings.json"
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        obj = {}
    return sanitize_settings(obj)


def save_settings(root, settings):
    clean = sanitize_settings(settings)
    (root / "settings.json").write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")


def vault():
    backend = keyring.get_keyring()
    if not isinstance(backend, WinVaultKeyring):
        raise RuntimeError("Windows 凭据管理器不可用，密钥没有保存。")
    return backend


def get_api_key():
    if os.environ.get("WORDBOOK_TEST_DATA_DIR"):
        return ""
    try:
        return vault().get_password(SERVICE, USER)
    except Exception as exc:
        raise RuntimeError("无法读取 Windows 凭据管理器中的密钥。") from exc


def set_api_key(value):
    try:
        vault().set_password(SERVICE, USER, value)
    except Exception as exc:
        raise RuntimeError("无法写入 Windows 凭据管理器，密钥没有保存。") from exc


def set_autostart(enabled):
    if not getattr(sys, "frozen", False) or os.environ.get("WORDBOOK_TEST_DATA_DIR"):
        return False
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, "WordBook", 0, winreg.REG_SZ, f'"{sys.executable}"')
        else:
            try:
                winreg.DeleteValue(key, "WordBook")
            except FileNotFoundError:
                pass
    return True


def hotkey_parts(value):
    pieces = [x.strip().lower() for x in value.split("+")]
    if len(set(pieces)) != len(pieces) or len(pieces) < 2:
        raise ValueError("快捷键需包含 Ctrl/Alt/Shift 和一个字母或数字。")
    modifiers = 0
    for word, bit in (("ctrl", 2), ("alt", 1), ("shift", 4), ("win", 8)):
        if word in pieces:
            modifiers |= bit
            pieces.remove(word)
    if not modifiers or len(pieces) != 1 or len(pieces[0]) != 1 or not pieces[0].isalnum() or not pieces[0].isascii():
        raise ValueError("快捷键需包含 Ctrl/Alt/Shift 和一个英文字母或数字。")
    return modifiers | 0x4000, ord(pieces[0].upper())


HOOKPROC = ctypes.WINFUNCTYPE(XLONG, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t)
USER32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, wintypes.DWORD]
USER32.SetWindowsHookExW.restype = ctypes.c_void_p
USER32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
USER32.UnhookWindowsHookEx.restype = wintypes.BOOL
USER32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t]
USER32.CallNextHookEx.restype = XLONG


class MouseSideHook(QObject):
    activated = Signal(object)
    WH_MOUSE_LL = 14

    def __init__(self, parent=None):
        super().__init__(parent)
        self._buttons = set()
        self._hook = None
        self._callback = HOOKPROC(self._hook_proc)
        self._left_down = False
        self._dragging = False
        self._selection_ready = False
        self._selection_hwnd = 0
        self._down_point = (0, 0)
        self._last_click_at = 0.0
        self._last_click_point = (0, 0)
        self._click_streak = 0
        self._swallow_side_up = False
        self._drag_x = max(3, int(USER32.GetSystemMetrics(SM_CXDRAG)))
        self._drag_y = max(3, int(USER32.GetSystemMetrics(SM_CYDRAG)))

    @property
    def installed(self):
        return bool(self._hook)

    @property
    def buttons(self):
        return set(self._buttons)

    def start(self, value="both"):
        self.stop()
        if value == "off":
            return True
        if value == "x1":
            self._buttons = {XBUTTON1}
        elif value == "x2":
            self._buttons = {XBUTTON2}
        else:
            self._buttons = {XBUTTON1, XBUTTON2}
        module = KERNEL32.GetModuleHandleW(None)
        self._hook = USER32.SetWindowsHookExW(self.WH_MOUSE_LL, self._callback, module, 0)
        if not self._hook:
            self._buttons.clear()
            return False
        return True

    def stop(self):
        if self._hook:
            USER32.UnhookWindowsHookEx(self._hook)
            self._hook = None
        self._buttons.clear()
        self._left_down = False
        self._dragging = False
        self._selection_ready = False
        self._selection_hwnd = 0
        self._last_click_at = 0.0
        self._click_streak = 0
        self._swallow_side_up = False

    @staticmethod
    def _side_button(mouse_data):
        return (int(mouse_data) >> 16) & 0xFFFF

    def _observe(self, message, info):
        point = (int(info.pt.x), int(info.pt.y))
        if message == WM_LBUTTONDOWN:
            self._left_down = True
            self._dragging = False
            self._down_point = point
            return None
        if message == WM_MOUSEMOVE and self._left_down:
            if (abs(point[0] - self._down_point[0]) >= self._drag_x
                    or abs(point[1] - self._down_point[1]) >= self._drag_y):
                self._dragging = True
            return None
        if message == WM_LBUTTONUP:
            if self._dragging:
                self._selection_ready = True
                self._selection_hwnd = foreground_window()
                self._last_click_at = 0.0
                self._click_streak = 0
            else:
                now = time.monotonic()
                repeated_click = (self._last_click_at
                                  and now - self._last_click_at <= USER32.GetDoubleClickTime() / 1000
                                  and abs(point[0] - self._last_click_point[0]) <= self._drag_x
                                  and abs(point[1] - self._last_click_point[1]) <= self._drag_y)
                if repeated_click:
                    self._click_streak += 1
                else:
                    self._click_streak = 1
                self._last_click_at = now
                self._last_click_point = point
                self._selection_ready = self._click_streak >= 2
                self._selection_hwnd = foreground_window() if self._selection_ready else 0
            self._left_down = False
            self._dragging = False
            return None
        if message in (WM_XBUTTONDOWN, WM_XBUTTONUP):
            if self._side_button(info.mouseData) not in self._buttons:
                return None
            if message == WM_XBUTTONUP:
                if self._swallow_side_up:
                    self._swallow_side_up = False
                    return "swallow"
                return None
            if self._selection_ready:
                if not self._selection_hwnd or foreground_window() != self._selection_hwnd:
                    self._selection_ready = False
                    self._selection_hwnd = 0
                    return None
                self._selection_ready = False
                self._swallow_side_up = True
                return "trigger"
        return None

    def _hook_proc(self, code, message, data):
        try:
            if code >= 0 and data:
                info = MSLLHOOKSTRUCT.from_address(int(data))
                action = self._observe(message, info)
                if action == "trigger":
                    hwnd = self._selection_hwnd
                    self._selection_hwnd = 0
                    if hwnd and process_id_for_window(hwnd) != current_process_id():
                        self.activated.emit(hwnd)
                        return 1
                    self._swallow_side_up = False
                elif action == "swallow":
                    return 1
        except Exception:
            pass
        return USER32.CallNextHookEx(None, code, message, data or 0)


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint), ("wParam", ctypes.c_size_t),
                ("lParam", ctypes.c_ssize_t), ("time", ctypes.c_ulong), ("pt_x", ctypes.c_long), ("pt_y", ctypes.c_long)]


class HotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def nativeEventFilter(self, event_type, message):
        if event_type == b"windows_generic_MSG":
            msg = MSG.from_address(int(message))
            if msg.message == 0x0312 and msg.wParam == 0x5742:
                self.callback()
                return True, 0
        return False, 0


class Hotkey:
    def __init__(self, app, window, callback):
        self.app = app
        self.hwnd = int(window.winId())
        self.filter = HotkeyFilter(callback)
        app.installNativeEventFilter(self.filter)
        self.active = False

    def register(self, sequence):
        modifiers, key = hotkey_parts(sequence)
        if self.active:
            USER32.UnregisterHotKey(self.hwnd, 0x5742)
            self.active = False
        self.active = bool(USER32.RegisterHotKey(self.hwnd, 0x5742, modifiers, key))
        return self.active

    def close(self):
        if self.active:
            USER32.UnregisterHotKey(self.hwnd, 0x5742)
            self.active = False
