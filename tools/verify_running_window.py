"""Read native styles of the known running WordBook window without capturing text."""
import ctypes
import json
import sys
import sqlite3
from ctypes import wintypes
from pathlib import Path

user32 = ctypes.WinDLL("user32", use_last_error=True)
pid = int(sys.argv[1])
user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_ssize_t)
found = []
@callback_type
def visit(hwnd, unused):
    owner = ctypes.c_uint()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
    if owner.value == pid:
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 256)
        if title.value == "WordBook":
            style = user32.GetWindowLongPtrW(hwnd, -20)
            rectangle = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rectangle))
            found.append({"toolwindow": bool(style & 0x80), "appwindow": bool(style & 0x40000), "visible": bool(user32.IsWindowVisible(hwnd)),
                          "size_pixels": [rectangle.right - rectangle.left, rectangle.bottom - rectangle.top]})
    return True
user32.EnumWindows.argtypes = [callback_type, ctypes.c_ssize_t]
user32.EnumWindows(visit, 0)
result = {"pid": pid, "windows": found, "ok": any(w["toolwindow"] and not w["appwindow"] and w["visible"] for w in found)}
snapshot_path = Path(__file__).resolve().parent.parent / "evidence/user-update-snapshot.json"
if snapshot_path.exists():
    before = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if before.get("snapshot_created"):
        connection = sqlite3.connect((Path(before["data_directory"]) / "wordbook.sqlite3").as_uri() + "?mode=ro", uri=True)
        try:
            after = {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("cards", "encounters", "reviews")}
            result["schema_version"] = connection.execute("PRAGMA user_version").fetchone()[0]
            result["foreign_keys_valid"] = not bool(connection.execute("PRAGMA foreign_key_check").fetchall())
            direction_counts = dict(connection.execute("SELECT translation_type, count(*) FROM cards GROUP BY translation_type"))
            expected_directions = before.get("translation_type_counts", {"en-zh": before["record_counts"]["cards"]})
            result["original_translation_direction_counts_preserved"] = all(direction_counts.get(code, 0) >= count for code, count in expected_directions.items())
        finally:
            connection.close()
        result["user_record_counts_preserved"] = all(after[table] >= count for table, count in before["record_counts"].items())
        result["ok"] = result["ok"] and result["user_record_counts_preserved"] and result["schema_version"] == 2 and result["foreign_keys_valid"] and result["original_translation_direction_counts_preserved"]
output_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).resolve().parent.parent / "evidence/running-v2.json"
output_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result))
raise SystemExit(0 if result["ok"] else 1)
