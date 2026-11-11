"""Export and restore only the isolated GUI-test library, never production data."""
import hashlib
import json
import sys
from pathlib import Path

from wordbook.store import Store

root = Path(__file__).resolve().parent.parent
evidence = root / "evidence"
source_root = evidence / (sys.argv[1] if len(sys.argv) > 1 else "packaged-release")
source = Store(source_root / "wordbook.sqlite3")
settings = json.loads((source_root / "settings.json").read_text(encoding="utf-8"))
for days in (7, 30):
    report = source.export_markdown(days)
    output = evidence / f"WordBook-测试最近{days}天.md"
    output.write_text(report, encoding="utf-8")
    assert "苹果" in output.read_text(encoding="utf-8")
    assert "待翻译" in report
backup = evidence / "WordBook-测试全量备份.wbak"
source.backup_to(backup, settings)
restored = Store(evidence / "restore-check" / "wordbook.sqlite3")
restored.restore_from(backup)
assert source.search() == restored.search()
for table in ("encounters", "reviews"):
    assert [tuple(row) for row in source.db.execute(f"SELECT * FROM {table} ORDER BY id")] == [tuple(row) for row in restored.db.execute(f"SELECT * FROM {table} ORDER BY id")]
result = {"export_utf8_7_30": True, "pending_items_labelled": True,
          "full_backup_restores_cards_encounters_reviews": True,
          "record_counts": {table: source.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("cards", "encounters", "reviews")},
          "executable_sha256": hashlib.sha256((root / "dist/WordBook/WordBook.exe").read_bytes()).hexdigest(),
          "real_api_call": False}
source.close()
restored.close()
(evidence / "delivery-checks.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False))
