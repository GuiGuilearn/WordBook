"""Read-only diagnostic of frozen Qt imports and native dependency exports."""
import ctypes
import os
import sys
import traceback
from pathlib import Path

import pefile

try:
    from PySide6 import QtCore
    print("QT_IMPORT_OK", QtCore.qVersion(), flush=True)
except Exception:
    traceback.print_exc()

base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent.parent / ".venv310/Lib/site-packages"))
handles = [os.add_dll_directory(str(base / name)) for name in ("PySide6", "shiboken6")]
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
kernel.GetProcAddress.restype = ctypes.c_void_p
kernel.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint]
kernel.GetModuleFileNameW.restype = ctypes.c_uint
kernel.LoadLibraryExW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint]
kernel.LoadLibraryExW.restype = ctypes.c_void_p
target = base / "PySide6/QtCore.pyd"
for entry in pefile.PE(str(target)).DIRECTORY_ENTRY_IMPORT:
    name = entry.dll.decode()
    path = next((folder / name for folder in (base, base / "PySide6", base / "shiboken6") if (folder / name).exists()), Path(name))
    try:
        dll = ctypes.WinDLL(str(path))
        actual = ctypes.create_unicode_buffer(32768)
        kernel.GetModuleFileNameW(dll._handle, actual, len(actual))
        print("LOADED", name, actual.value, flush=True)
        missing = [item.name.decode() for item in entry.imports if item.name and not kernel.GetProcAddress(dll._handle, item.name)]
        print("MISSING_EXPORTS", name, missing, flush=True)
    except OSError as exc:
        print("DLL_LOAD_ERROR", name, repr(exc), repr(exc.__cause__), repr(exc.__context__), flush=True)

print("NATIVE_QT_DEPENDENCIES", flush=True)
for entry in pefile.PE(str(base / "PySide6/Qt6Core.dll")).DIRECTORY_ENTRY_IMPORT:
    name = entry.dll.decode()
    path = next((folder / name for folder in (base, base / "PySide6", base / "shiboken6") if (folder / name).exists()), Path(name))
    handle = kernel.LoadLibraryExW(str(path), None, 0x1100 if path.is_absolute() else 0x1000)
    if not handle:
        print("NATIVE_LOAD_ERROR", name, ctypes.get_last_error(), flush=True)
        continue
    actual = ctypes.create_unicode_buffer(32768)
    kernel.GetModuleFileNameW(handle, actual, len(actual))
    missing = [item.name.decode() for item in entry.imports if item.name and not kernel.GetProcAddress(handle, item.name)]
    print("NATIVE_IMPORTED", name, actual.value, "missing", missing, flush=True)
