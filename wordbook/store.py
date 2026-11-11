import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from html import escape
from .translations import TRANSLATION_TYPES, brief_translation


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalized(text):
    return re.sub(r"\s+", " ", text.strip()).casefold()


def local_day(utc_text):
    return datetime.fromisoformat(utc_text).astimezone().date()


def next_day(day):
    return (day + timedelta(days=1)).isoformat()


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.migrate()

    def migrate(self):
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > 2:
            raise RuntimeError("词库版本高于当前程序，请更新程序。")
        if version == 0:
            with self.db:
                self.db.executescript("""
                    CREATE TABLE cards (
                      id INTEGER PRIMARY KEY, norm TEXT NOT NULL UNIQUE,
                      original TEXT NOT NULL, kind TEXT NOT NULL,
                      created_utc TEXT NOT NULL, last_seen_utc TEXT NOT NULL,
                      seen_count INTEGER NOT NULL DEFAULT 1,
                      analysis_json TEXT, error TEXT,
                      due_date TEXT, review_stage INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE encounters (
                      id INTEGER PRIMARY KEY, card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
                      occurred_utc TEXT NOT NULL
                    );
                    CREATE INDEX encounters_time ON encounters(occurred_utc);
                    CREATE TABLE reviews (
                      id INTEGER PRIMARY KEY, card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
                      reviewed_utc TEXT NOT NULL, known INTEGER NOT NULL,
                      due_after TEXT NOT NULL
                    );
                    PRAGMA user_version=1;
                """)
            version = 1
        if version == 1:
            self.db.execute("PRAGMA foreign_keys=OFF")
            try:
                self.db.executescript("""
                    BEGIN;
                    CREATE TABLE cards_v2 (
                      id INTEGER PRIMARY KEY, norm TEXT NOT NULL,
                      original TEXT NOT NULL, kind TEXT NOT NULL,
                      created_utc TEXT NOT NULL, last_seen_utc TEXT NOT NULL,
                      seen_count INTEGER NOT NULL DEFAULT 1,
                      analysis_json TEXT, error TEXT,
                      due_date TEXT, review_stage INTEGER NOT NULL DEFAULT 0,
                      translation_type TEXT NOT NULL DEFAULT 'en-zh'
                        CHECK(translation_type IN ('en-zh','zh-en')),
                      UNIQUE(norm,translation_type)
                    );
                    INSERT INTO cards_v2(id,norm,original,kind,created_utc,last_seen_utc,seen_count,analysis_json,error,due_date,review_stage)
                    SELECT id,norm,original,kind,created_utc,last_seen_utc,seen_count,analysis_json,error,due_date,review_stage FROM cards;
                    DROP TABLE cards;
                    ALTER TABLE cards_v2 RENAME TO cards;
                    PRAGMA user_version=2;
                    COMMIT;
                """)
            except Exception:
                self.db.rollback()
                raise
            finally:
                self.db.execute("PRAGMA foreign_keys=ON")
            if self.db.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("词库迁移后记录关系校验失败。")

    def close(self):
        self.db.close()

    def submit(self, text, kind, when=None, translation_type="en-zh"):
        text = text.strip()
        if not text:
            raise ValueError("请输入要翻译的文本。")
        if len(text) > 1000:
            raise ValueError("最多输入 1000 个字符，请缩短内容。")
        if kind not in ("word", "sentence"):
            raise ValueError("无效类型")
        if translation_type not in TRANSLATION_TYPES:
            raise ValueError("无效翻译类型")
        when = when or now_utc()
        norm = normalized(text)
        with self.db:
            old = self.db.execute("SELECT id FROM cards WHERE norm=? AND translation_type=?", (norm, translation_type)).fetchone()
            if old:
                card_id = old["id"]
                self.db.execute("UPDATE cards SET seen_count=seen_count+1,last_seen_utc=? WHERE id=?", (when, card_id))
            else:
                cur = self.db.execute(
                    "INSERT INTO cards(norm,original,kind,created_utc,last_seen_utc,translation_type) VALUES(?,?,?,?,?,?)",
                    (norm, text, kind, when, when, translation_type))
                card_id = cur.lastrowid
            self.db.execute("INSERT INTO encounters(card_id,occurred_utc) VALUES(?,?)", (card_id, when))
        return self.card(card_id)

    def card(self, card_id):
        row = self.db.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
        return dict(row) if row else None

    def latest_card(self):
        # Encounter order distinguishes repeated old cards even within one second.
        row = self.db.execute("SELECT c.* FROM cards c JOIN encounters e ON e.card_id=c.id ORDER BY e.id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def save_analysis(self, card_id, analysis, when=None):
        when = when or now_utc()
        with self.db:
            row = self.card(card_id)
            if not row:
                return
            due = row["due_date"] or next_day(local_day(when))
            self.db.execute("UPDATE cards SET analysis_json=?,error=NULL,due_date=? WHERE id=?",
                            (json.dumps(analysis, ensure_ascii=False), due, card_id))

    def set_error(self, card_id, message):
        with self.db:
            self.db.execute("UPDATE cards SET error=? WHERE id=?", (message[:200], card_id))

    def edit(self, card_id, brief, usage, details):
        row = self.card(card_id)
        if not row or not row["analysis_json"]:
            return
        analysis = json.loads(row["analysis_json"])
        if not all(isinstance(v, str) and v.strip() for v in (brief, usage, details)):
            raise ValueError("译文、用法和详细说明都不能为空。")
        translation_field = "brief_translation" if "brief_translation" in analysis else "brief_translation_zh"
        analysis.update({translation_field: brief.strip(), "brief_usage_zh": usage.strip(), "details_zh": details.strip()})
        self.save_analysis(card_id, analysis)

    def delete(self, card_id):
        with self.db:
            self.db.execute("DELETE FROM cards WHERE id=?", (card_id,))

    def delete_many(self, card_ids):
        with self.db:
            self.db.executemany("DELETE FROM cards WHERE id=?", ((cid,) for cid in set(card_ids)))

    def search(self, query="", kind=None, translation_type=None):
        sql = "SELECT * FROM cards WHERE (original LIKE ? OR analysis_json LIKE ?)"
        args = [f"%{query}%", f"%{query}%"]
        if kind in ("word", "sentence"):
            sql += " AND kind=?"
            args.append(kind)
        if translation_type in TRANSLATION_TYPES:
            sql += " AND translation_type=?"
            args.append(translation_type)
        sql += " ORDER BY last_seen_utc DESC,id DESC"
        return [dict(x) for x in self.db.execute(sql, args)]

    def due_cards(self, today=None):
        today = (today or date.today()).isoformat()
        return [dict(x) for x in self.db.execute(
            "SELECT * FROM cards WHERE analysis_json IS NOT NULL AND due_date<=? ORDER BY due_date,id", (today,))]

    def grade(self, card_id, known, today=None, when=None):
        today = today or date.today()
        when = when or now_utc()
        row = self.card(card_id)
        if not row or not row["analysis_json"] or not row["due_date"] or row["due_date"] > today.isoformat():
            raise ValueError("此卡片当前无需复习。")
        stage = min(row["review_stage"] + 1, 4) if known else 0
        interval = (1, 3, 7, 14, 30)[stage]
        due = (today + timedelta(days=interval)).isoformat()
        with self.db:
            self.db.execute("UPDATE cards SET review_stage=?,due_date=? WHERE id=?", (stage, due, card_id))
            self.db.execute("INSERT INTO reviews(card_id,reviewed_utc,known,due_after) VALUES(?,?,?,?)",
                            (card_id, when, int(known), due))
        return due

    def export_markdown(self, days=None, today=None, translation_type=None, query=""):
        if days not in (None, 7, 30):
            raise ValueError("仅支持全部词库或最近 7/30 天。")
        today = today or date.today()
        start = today - timedelta(days=days - 1) if days else None
        counts = {}
        for row in self.db.execute("SELECT card_id,occurred_utc FROM encounters"):
            day = local_day(row["occurred_utc"])
            if start is None or start <= day <= today:
                item = counts.setdefault(row["card_id"], {"count": 0, "days": set(), "last": row["occurred_utc"]})
                item["count"] += 1
                item["days"].add(day.isoformat())
                item["last"] = max(item["last"], row["occurred_utc"])
        matches = [(card, counts[card["id"]]) for card in self.search(query, translation_type=translation_type) if card["id"] in counts]
        matches.sort(key=lambda pair: pair[1]["last"], reverse=True)
        def cell(value):
            return escape(str(value), quote=False).replace("|", "\\|").replace("\r\n", "\n").replace("\n", "<br>")
        lines = ["| 原文 | 次数 | 翻译 |", "| --- | ---: | --- |"]
        for card, meta in matches:
            translated = brief_translation(json.loads(card["analysis_json"])) if card["analysis_json"] else "待翻译"
            count = card["seen_count"] if days is None else meta["count"]
            lines.append(f"| {cell(card['original'])} | {count} | {cell(translated)} |")
        return "\n".join(lines) + "\n"

    def backup_to(self, path, settings=None):
        import tempfile
        import zipfile
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "wordbook.sqlite3"
            target = sqlite3.connect(db_path)
            with target:
                self.db.backup(target)
            target.close()
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(db_path, "wordbook.sqlite3")
                allowed = {"model", "hotkey", "autostart", "screen", "edge", "offset", "onboarded",
                           "translation_type", "mouse_side_button"}
                clean_settings = {k: v for k, v in (settings or {}).items() if k in allowed}
                z.writestr("settings.json", json.dumps(clean_settings, ensure_ascii=False))

    def restore_from(self, path):
        import tempfile
        import zipfile
        with zipfile.ZipFile(path) as z:
            if "wordbook.sqlite3" not in z.namelist():
                raise ValueError("备份缺少词库。")
            with tempfile.TemporaryDirectory() as temp:
                staged = Path(temp) / "restore.sqlite3"
                staged.write_bytes(z.read("wordbook.sqlite3"))
                source = sqlite3.connect(f"file:{staged}?mode=ro", uri=True)
                try:
                    if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("备份词库损坏。")
                    version = source.execute("PRAGMA user_version").fetchone()[0]
                    if version not in (1, 2):
                        raise ValueError("备份版本不受支持。")
                    expected = {"cards": {"id", "norm", "original", "kind", "analysis_json", "due_date", "review_stage", "seen_count", "created_utc", "last_seen_utc", "error"},
                                "encounters": {"id", "card_id", "occurred_utc"},
                                "reviews": {"id", "card_id", "reviewed_utc", "known", "due_after"}}
                    for table, required in expected.items():
                        if table == "cards" and version == 2:
                            required = required | {"translation_type"}
                        columns = {row[1] for row in source.execute(f"PRAGMA table_info({table})")}
                        if not required <= columns:
                            raise ValueError("备份词库结构无效。")
                    if source.execute("PRAGMA foreign_key_check").fetchall():
                        raise ValueError("备份词库记录不完整。")
                    if source.execute("SELECT count(*) FROM cards WHERE norm IS NULL OR original IS NULL OR kind NOT IN ('word','sentence') OR seen_count<1").fetchone()[0]:
                        raise ValueError("备份词条数据无效。")
                    group = "norm,translation_type" if version == 2 else "norm"
                    if source.execute(f"SELECT 1 FROM cards GROUP BY {group} HAVING count(*)>1 LIMIT 1").fetchone():
                        raise ValueError("备份词条重复。")
                    if version == 2 and source.execute("SELECT 1 FROM cards WHERE translation_type NOT IN ('en-zh','zh-en') OR translation_type IS NULL LIMIT 1").fetchone():
                        raise ValueError("备份翻译类型无效。")
                    settings = json.loads(z.read("settings.json")) if "settings.json" in z.namelist() else {}
                    if not isinstance(settings, dict):
                        raise ValueError("备份设置无效。")
                    with self.db:
                        source.backup(self.db)
                    self.migrate()
                finally:
                    source.close()
                return settings
