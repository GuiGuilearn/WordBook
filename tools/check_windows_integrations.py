"""Check native shortcut registration and backend type without reading API keys."""
import ctypes
import json
from pathlib import Path

import keyring
from PySide6.QtCore import QCoreApplication, QStandardPaths
from wordbook.platform_win import hotkey_parts

app = QCoreApplication([])
app.setApplicationName("WordBook")
app.setOrganizationName("WordBook")
user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
user32.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
checks = {}
for sequence in ("Ctrl+Alt+W", "Ctrl+Alt+9"):
    modifiers, key = hotkey_parts(sequence)
    ctypes.set_last_error(0)
    success = bool(user32.RegisterHotKey(None, 0x5743, modifiers, key))
    checks[sequence] = {"register_ok": success, "error": 0 if success else ctypes.get_last_error()}
    if success:
        user32.UnregisterHotKey(None, 0x5743)
result = {"shortcuts": checks, "keyring_backend": type(keyring.get_keyring()).__name__,
          "production_data_location": QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation),
          "api_key_read": False}
output = Path(__file__).resolve().parent.parent / "evidence/windows-integrations.json"
output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False))
