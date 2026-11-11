"""Include verified evidence and create the portable onedir delivery archive."""
import hashlib
import json
import shutil
import zipfile
from pathlib import Path


root = Path(__file__).resolve().parent.parent
bundle = root / "dist" / "WordBook"
evidence = root / "evidence"
version = "direction-reset-v9"
source_checks = "source-direction-v9"
packaged_checks = "packaged-direction-v9c"

gui_checks = []
for folder in (source_checks, packaged_checks):
    for stage in ("first", "reopen"):
        result = json.loads((evidence / folder / f"gui-smoke-{stage}.json").read_text(encoding="utf-8"))
        assert result["ok"] and result["qt_platform"] == "windows" and result["version"] == version
        if stage == "first":
            assert all(result[key] for key in (
                "default_direction_is_english_to_chinese",
                "chinese_to_english_completion_resets_direction",
                "chinese_to_english_failure_keeps_direction",
                "cached_chinese_to_english_completion_resets_direction",
            ))
        else:
            assert result["restart_defaults_to_english_to_chinese"]
        gui_checks.append({"path": f"{folder}/gui-smoke-{stage}.json", "ok": True})
    destination = bundle / "evidence" / folder
    destination.mkdir(parents=True, exist_ok=True)
    for file in (evidence / folder).glob("gui-*"):
        if file.suffix in (".json", ".png"):
            shutil.copy2(file, destination / file.name)
    classified_export = evidence / folder / "library-中译英.md"
    if classified_export.exists():
        shutil.copy2(classified_export, destination / classified_export.name)

native = json.loads((evidence / "running-direction-v9.json").read_text(encoding="utf-8"))
user_data = json.loads((evidence / "user-data-after-direction-v9.json").read_text(encoding="utf-8"))
audit = json.loads((evidence / "bundle-audit.json").read_text(encoding="utf-8"))
assert native["ok"] and native["user_record_counts_preserved"] and audit["ok"]
assert user_data["integrity"] == "ok" and user_data["foreign_keys_valid"]
test_output = (evidence / "test-results-direction-v9.txt").read_text(encoding="utf-8")
assert "65 passed" in test_output

for name in ("README.md", "验收报告.md", "开发任务书.md"):
    shutil.copy2(root / name, bundle / name)

exe_hash = hashlib.sha256((bundle / "WordBook.exe").read_bytes()).hexdigest()
assert exe_hash == hashlib.sha256((root / "build" / "WordBook" / "WordBook.exe").read_bytes()).hexdigest()
(evidence / "executable-sha256.json").write_text(
    json.dumps({"file": "WordBook.exe", "sha256": exe_hash}, indent=2), encoding="utf-8"
)

delivery = {
    "date": "2026-11-11",
    "version": version,
    "automated_tests_passed": 65,
    "gui_checks": gui_checks,
    "native_window_verified": native["ok"],
    "user_library_preserved": native["user_record_counts_preserved"],
    "user_record_counts_after_install": {
        "cards": user_data["cards"],
        "encounters": user_data["encounters"],
        "translation_type_counts": user_data["translation_type_counts"],
    },
    "bundle_audit_passed": audit["ok"],
    "api_validation": "mock_only",
    "real_api_key_read": False,
    "real_api_call_made": False,
    "direction_reset_verified_in_source_and_packaged_gui": True,
    "executable_sha256": exe_hash,
}
(evidence / "direction-reset-delivery.json").write_text(
    json.dumps(delivery, ensure_ascii=False, indent=2), encoding="utf-8"
)

destination = bundle / "evidence"
destination.mkdir(parents=True, exist_ok=True)
for name in (
    "test-results-direction-v9.txt",
    "bundle-audit.json",
    "running-direction-v9.json",
    "user-data-after-direction-v9.json",
    "direction-reset-delivery.json",
    "executable-sha256.json",
):
    shutil.copy2(evidence / name, destination / name)

archive = root / "dist" / "WordBook-Windows-x64.zip"
if archive.exists():
    archive.unlink()
archive = Path(shutil.make_archive(str(root / "dist" / "WordBook-Windows-x64"), "zip", root / "dist", "WordBook"))
with zipfile.ZipFile(archive) as output:
    assert "WordBook/WordBook.exe" in output.namelist()
    assert "WordBook/_internal/python310.dll" in output.namelist()
    assert output.testzip() is None
    assert not any("user-snapshots" in name or name.endswith(".sqlite3") for name in output.namelist())
    assert not any("{{" in name for name in output.namelist())

print(json.dumps({
    "archive": str(archive),
    "bytes": archive.stat().st_size,
    "zip_integrity": True,
    "executable_sha256": exe_hash,
}, ensure_ascii=False))
