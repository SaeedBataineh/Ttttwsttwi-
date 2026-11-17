"""SQLite storage for clips and small key/value state."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    id              TEXT PRIMARY KEY,      -- Twitch clip id (slug)
    url             TEXT NOT NULL,
    broadcaster_id  TEXT NOT NULL,
    broadcaster     TEXT NOT NULL,         -- login
    creator_name    TEXT,
    video_id        TEXT,
    vod_offset      INTEGER,
    game_id         TEXT,
    game_name       TEXT,
    title           TEXT,
    view_count      INTEGER NOT NULL,
    duration        REAL,
    created_at      TEXT NOT NULL,         -- ISO UTC
    score           REAL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'seen',
    -- seen | rendering | review | approved | posted | rejected | duplicate | failed | skipped
    flags           TEXT DEFAULT '[]',     -- JSON list of warnings
    post_title      TEXT,
    work_dir        TEXT,
    review_message_id TEXT,
    rendered_at     TEXT,
    approved_at     TEXT,
    posted_at       TEXT,
    results         TEXT DEFAULT '{}',     -- JSON {platform: url}
    error           TEXT,
    first_seen_at   TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clips_status ON clips(status);
CREATE INDEX IF NOT EXISTS idx_clips_broadcaster ON clips(broadcaster);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

FINAL_STATUSES = ("posted", "rejected", "duplicate", "skipped")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def execute(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def one(self, sql: str, params=()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # --- kv ---
    def kv_get(self, key: str, default=None):
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def kv_set(self, key: str, value) -> None:
        self.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    # --- clips ---
    def clip(self, clip_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM clips WHERE id=?", (clip_id,))

    def upsert_clip(self, c: dict) -> None:
        """Insert a new clip, or refresh views/score of a known one."""
        ts = now_iso()
        self.execute(
            """
            INSERT INTO clips(id, url, broadcaster_id, broadcaster, creator_name, video_id, vod_offset,
                              game_id, game_name, title, view_count, duration, created_at, score,
                              first_seen_at, updated_at)
            VALUES(:id, :url, :broadcaster_id, :broadcaster, :creator_name, :video_id, :vod_offset,
                   :game_id, :game_name, :title, :view_count, :duration, :created_at, :score, :ts, :ts)
            ON CONFLICT(id) DO UPDATE SET
                view_count=excluded.view_count,
                score=excluded.score,
                game_name=COALESCE(excluded.game_name, clips.game_name),
                updated_at=excluded.updated_at
            """,
            {**c, "ts": ts},
        )

    def update_clip(self, clip_id: str, **fields) -> None:
        if not fields:
            return
        for k in ("flags", "results"):
            if k in fields and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k])
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k}=:{k}" for k in fields)
        self.execute(f"UPDATE clips SET {cols} WHERE id=:id", {**fields, "id": clip_id})

    def by_status(self, *statuses: str, order: str = "score DESC") -> list[sqlite3.Row]:
        marks = ",".join("?" * len(statuses))
        return self.query(f"SELECT * FROM clips WHERE status IN ({marks}) ORDER BY {order}", statuses)
