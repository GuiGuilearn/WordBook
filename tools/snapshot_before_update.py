"""Create a private, read-only snapshot of the user's library before replacement."""
import json
import os
import sqlite3
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from PySide6.QtCore import QCoreApplication, QStandardPaths
from wordbook.platform_win import sanitize_settings

if os.environ.get("WORDBOOK_TEST_DATA_DIR"):
    raise RuntimeError("更新前快照不能使用测试数据目录。")
app = QCoreApplication([])
app.setApplicationName("WordBook")
app.setOrganizationName("WordBook")
data = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation))
database = data / "wordbook.sqlite3"
result = {"data_directory": str(data), "snapshot_created": False}
if database.is_file():
    root = PROJECT
    destination = root / "evidence/user-snapshots"
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / f"WordBook-更新前-{uuid.uuid4().hex[:12]}.wbak"
    with tempfile.TemporaryDirectory(dir=root / ".tmp") as temporary:
        staged = Path(temporary) / "wordbook.sqlite3"
        source = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        target = sqlite3.connect(staged)
        try:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("更新前词库快照校验失败。")
            result["record_counts"] = {table: target.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("cards", "encounters", "reviews")}
            result["schema_version"] = target.execute("PRAGMA user_version").fetchone()[0]
            result["translation_type_counts"] = dict(target.execute("SELECT translation_type, count(*) FROM cards GROUP BY translation_type")) if result["schema_version"] >= 2 else {"en-zh": result["record_counts"]["cards"]}
        finally:
            target.close()
            source.close()
        settings_path = data / "settings.json"
        settings = sanitize_settings(json.loads(settings_path.read_text(encoding="utf-8"))) if settings_path.exists() else sanitize_settings({})
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(staged, "wordbook.sqlite3")
            archive.writestr("settings.json", json.dumps(settings, ensure_ascii=False))
    result.update(snapshot_created=True, snapshot=str(output), integrity_ok=True)
Path(__file__).resolve().parent.parent.joinpath("evidence/user-update-snapshot.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False))
