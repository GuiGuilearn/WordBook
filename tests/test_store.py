import json
import sqlite3
from datetime import date, timedelta

import pytest

from wordbook.store import Store


ANALYSIS = {"kind": "word", "brief_translation_zh": "苹果", "brief_usage_zh": "可数名词",
            "details_zh": "一种水果", "part_of_speech": "n.", "examples": []}


def test_duplicate_and_events(tmp_path):
    db = Store(tmp_path / "a.sqlite3")
    a = db.submit("  Hello   WORLD  ", "sentence", "2026-09-20T00:00:00+00:00")
    b = db.submit("hello world", "sentence", "2026-09-21T00:00:00+00:00")
    assert a["id"] == b["id"]
    assert b["seen_count"] == 2
    assert b["original"] == "Hello   WORLD"
    assert db.db.execute("SELECT count(*) FROM encounters").fetchone()[0] == 2
    db.close()
    assert Store(tmp_path / "a.sqlite3").card(a["id"])["seen_count"] == 2


def test_latest_card_uses_last_encounter_even_when_times_tie(tmp_path):
    db = Store(tmp_path / "a.sqlite3")
    timestamp = "2026-10-09T00:00:00+00:00"
    first = db.submit("first", "word", timestamp)
    second = db.submit("second", "word", timestamp)
    assert db.latest_card()["id"] == second["id"]
    db.submit("first", "word", timestamp)
    assert db.latest_card()["id"] == first["id"]
    db.close()
    assert Store(tmp_path / "a.sqlite3").latest_card()["id"] == first["id"]


def test_review_intervals_and_restart(tmp_path):
    path = tmp_path / "a.sqlite3"
    db = Store(path)
    card = db.submit("apple", "word", "2026-09-20T12:00:00+00:00")
    db.save_analysis(card["id"], ANALYSIS, "2026-09-20T12:01:00+00:00")
    first = db.card(card["id"])["due_date"]
    assert first == "2026-09-21"
    assert not db.due_cards(date(2026, 9, 20))
    due = date.fromisoformat(first)
    for interval in (3, 7, 14, 30, 30):
        due = due + timedelta(days=interval)
        assert db.grade(card["id"], True, due - timedelta(days=interval)) == due.isoformat()
    db.close()
    db = Store(path)
    assert db.card(card["id"])["review_stage"] == 4
    assert db.grade(card["id"], False, due) == (due + timedelta(days=1)).isoformat()
    assert db.card(card["id"])["review_stage"] == 0
    assert db.db.execute("SELECT count(*) FROM reviews").fetchone()[0] == 6


def test_export_encounter_boundaries(tmp_path):
    db = Store(tmp_path / "a.sqlite3")
    today = date.today()
    old = today - timedelta(days=50)
    card = db.submit("apple", "word", f"{old}T12:00:00+00:00")
    db.save_analysis(card["id"], ANALYSIS)
    recent = today - timedelta(days=6)
    db.submit("apple", "word", f"{recent}T12:00:00+00:00")
    db.submit("pear", "word", f"{today - timedelta(days=7)}T12:00:00+00:00")
    report = db.export_markdown(7, today)
    assert "| apple | 1 | 苹果 |" in report
    assert "pear" not in report
    assert "待翻译" not in report
    report30 = db.export_markdown(30, today)
    assert "pear" in report30 and "待翻译" in report30


def test_backup_restore(tmp_path):
    db = Store(tmp_path / "a.sqlite3")
    card = db.submit("apple", "word")
    db.save_analysis(card["id"], ANALYSIS)
    db.grade(card["id"], False, date.today() + timedelta(days=1))
    backup = tmp_path / "backup.wbak"
    db.backup_to(backup, {"model": "deepseek-flash"})
    db.delete(card["id"])
    settings = db.restore_from(backup)
    assert settings["model"] == "deepseek-flash"
    assert db.card(card["id"])["original"] == "apple"
    assert db.db.execute("SELECT count(*) FROM encounters").fetchone()[0] == 1
    assert db.db.execute("SELECT count(*) FROM reviews").fetchone()[0] == 1


def test_phonetics_survive_edit_restart_backup_and_restore(tmp_path):
    path = tmp_path / "phonetics.sqlite3"
    db = Store(path)
    card = db.submit("tomato", "word")
    analysis = {**ANALYSIS, "phonetic_us": "/təˈmeɪtoʊ/", "phonetic_uk": "/təˈmɑːtəʊ/"}
    db.save_analysis(card["id"], analysis)
    db.edit(card["id"], "番茄", "我的笔记", "我的详细说明")
    db.close()
    db = Store(path)
    saved = json.loads(db.card(card["id"])["analysis_json"])
    assert saved["phonetic_us"] == analysis["phonetic_us"]
    assert saved["phonetic_uk"] == analysis["phonetic_uk"]
    assert saved["brief_translation_zh"] == "番茄"
    backup = tmp_path / "phonetics.wbak"
    db.backup_to(backup, {})
    db.delete(card["id"])
    db.restore_from(backup)
    assert json.loads(db.card(card["id"])["analysis_json"]) == saved
    assert db.export_markdown().splitlines()[0] == "| 原文 | 次数 | 翻译 |"
    db.close()


def create_legacy_database(path):
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE cards (id INTEGER PRIMARY KEY,norm TEXT NOT NULL UNIQUE,original TEXT NOT NULL,kind TEXT NOT NULL,
            created_utc TEXT NOT NULL,last_seen_utc TEXT NOT NULL,seen_count INTEGER NOT NULL,analysis_json TEXT,error TEXT,
            due_date TEXT,review_stage INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE encounters (id INTEGER PRIMARY KEY,card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,occurred_utc TEXT NOT NULL);
        CREATE TABLE reviews (id INTEGER PRIMARY KEY,card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,reviewed_utc TEXT NOT NULL,known INTEGER NOT NULL,due_after TEXT NOT NULL);
        PRAGMA user_version=1;
    """)
    timestamp = "2026-10-01T12:00:00+00:00"
    connection.execute("INSERT INTO cards VALUES(?,?,?,?,?,?,?,?,?,?,?)", (7, "apple", "Apple", "word", timestamp, timestamp, 2, json.dumps(ANALYSIS, ensure_ascii=False), None, "2026-10-05", 1))
    connection.executemany("INSERT INTO encounters VALUES(?,?,?)", [(20, 7, timestamp), (21, 7, timestamp)])
    connection.execute("INSERT INTO reviews VALUES(9,7,?,1,'2026-10-05')", (timestamp,))
    connection.commit()
    connection.close()


def test_v1_migration_preserves_ids_data_and_events(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    create_legacy_database(path)
    store = Store(path)
    assert store.db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert store.card(7)["translation_type"] == "en-zh"
    assert store.card(7)["original"] == "Apple"
    assert store.card(7)["seen_count"] == 2
    assert json.loads(store.card(7)["analysis_json"]) == ANALYSIS
    assert [row[0] for row in store.db.execute("SELECT id FROM encounters ORDER BY id")] == [20, 21]
    assert store.db.execute("SELECT card_id FROM reviews WHERE id=9").fetchone()[0] == 7
    assert not store.db.execute("PRAGMA foreign_key_check").fetchall()


def test_v1_backup_restores_and_migrates(tmp_path):
    import zipfile
    legacy = tmp_path / "legacy.sqlite3"
    create_legacy_database(legacy)
    backup = tmp_path / "legacy.wbak"
    with zipfile.ZipFile(backup, "w") as archive:
        archive.write(legacy, "wordbook.sqlite3")
    target = Store(tmp_path / "new.sqlite3")
    target.submit("temporary", "word")
    target.restore_from(backup)
    assert target.card(7)["translation_type"] == "en-zh"
    assert target.latest_card()["id"] == 7
    assert len(target.search()) == 1


def test_same_text_different_directions_do_not_share_card(tmp_path):
    store = Store(tmp_path / "a.sqlite3")
    first = store.submit("OK", "word", translation_type="en-zh")
    second = store.submit("OK", "word", translation_type="zh-en")
    assert first["id"] != second["id"]
    assert store.submit("ok", "word", translation_type="zh-en")["seen_count"] == 2
    assert store.card(first["id"])["seen_count"] == 1
    assert len(store.search(translation_type="en-zh")) == 1
    assert len(store.search(translation_type="zh-en")) == 1


def test_export_contains_only_three_fields_and_filters_direction(tmp_path):
    store = Store(tmp_path / "a.sqlite3")
    old = store.submit("apple", "word")
    store.save_analysis(old["id"], ANALYSIS)
    new = store.submit("苹果", "sentence", translation_type="zh-en")
    store.save_analysis(new["id"], {**ANALYSIS, "brief_translation": "apple"})
    store.submit("苹果", "sentence", translation_type="zh-en")
    report = store.export_markdown(translation_type="zh-en")
    assert report == "| 原文 | 次数 | 翻译 |\n| --- | ---: | --- |\n| 苹果 | 2 | apple |\n"
    assert "用法" not in report and "日期" not in report and "详细" not in report


def test_batch_delete_cascades_without_deleting_other_direction(tmp_path):
    store = Store(tmp_path / "a.sqlite3")
    first = store.submit("OK", "word", translation_type="en-zh")
    second = store.submit("OK", "word", translation_type="zh-en")
    store.delete_many([second["id"], second["id"]])
    assert store.card(first["id"])
    assert not store.card(second["id"])
    assert store.db.execute("SELECT count(*) FROM encounters").fetchone()[0] == 1


def test_export_escapes_table_pipes_and_newlines(tmp_path):
    store = Store(tmp_path / "a.sqlite3")
    store.submit("A|B\nnext", "sentence")
    assert "A\\|B<br>next" in store.export_markdown()


def test_invalid_input(tmp_path):
    db = Store(tmp_path / "a.sqlite3")
    for text in ("", " " * 5, "x" * 1001):
        with pytest.raises(ValueError):
            db.submit(text, "word")
    assert not db.search()


def test_30_day_boundary_and_local_midnight(tmp_path):
    from datetime import datetime, time, timezone
    db = Store(tmp_path / "a.sqlite3")
    today = date(2026, 10, 9)
    for text, delta in (("inside", 29), ("outside", 30)):
        day = today - timedelta(days=delta)
        # Convert local midnight to UTC so the test checks the calendar-day rule.
        timestamp = datetime.combine(day, time(0, 0)).astimezone(timezone.utc).isoformat()
        db.submit(text, "word", timestamp)
    report = db.export_markdown(30, today)
    assert "inside" in report and "outside" not in report


def test_failed_reanalysis_preserves_previous_analysis(tmp_path):
    db = Store(tmp_path / "a.sqlite3")
    card = db.submit("apple", "word")
    db.save_analysis(card["id"], ANALYSIS)
    original = db.card(card["id"])["analysis_json"]
    db.set_error(card["id"], "连接超时")
    assert db.card(card["id"])["analysis_json"] == original
    assert db.card(card["id"])["error"] == "连接超时"


def test_backup_excludes_secret_fields(tmp_path):
    import zipfile
    db = Store(tmp_path / "a.sqlite3")
    backup = tmp_path / "backup.wbak"
    db.backup_to(backup, {"model": "deepseek-flash", "mouse_side_button": "x2",
                          "api_key": "must-not-save", "secret": "must-not-save"})
    with zipfile.ZipFile(backup) as archive:
        settings = json.loads(archive.read("settings.json"))
    assert settings == {"model": "deepseek-flash", "mouse_side_button": "x2"}


def test_invalid_backup_schema_does_not_replace_data(tmp_path):
    import zipfile
    db = Store(tmp_path / "a.sqlite3")
    card = db.submit("apple", "word")
    invalid_path = tmp_path / "invalid.sqlite3"
    invalid = sqlite3.connect(invalid_path)
    invalid.execute("PRAGMA user_version=1")
    invalid.close()
    backup = tmp_path / "bad.wbak"
    with zipfile.ZipFile(backup, "w") as archive:
        archive.write(invalid_path, "wordbook.sqlite3")
    with pytest.raises(ValueError, match="结构"):
        db.restore_from(backup)
    assert db.card(card["id"])["original"] == "apple"
