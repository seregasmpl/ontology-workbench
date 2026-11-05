from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def uid() -> str:
    return uuid.uuid4().hex


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS versions(
 id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),revision INTEGER NOT NULL,
 body TEXT NOT NULL,frozen INTEGER NOT NULL DEFAULT 0,examples TEXT NOT NULL DEFAULT '[]',
 parameters TEXT NOT NULL DEFAULT '{}',created TEXT NOT NULL,note TEXT NOT NULL DEFAULT '',
 UNIQUE(project_id,revision));
CREATE TABLE IF NOT EXISTS items(
 id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),source_id TEXT NOT NULL,
 group_id TEXT NOT NULL,text TEXT NOT NULL,context TEXT NOT NULL DEFAULT '',digest TEXT NOT NULL,
 provenance TEXT NOT NULL,split TEXT NOT NULL DEFAULT 'pool',restricted INTEGER NOT NULL DEFAULT 1,
 synthetic_kind TEXT,expected TEXT,created TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS items_project ON items(project_id);
CREATE TABLE IF NOT EXISTS gold(
 item_id TEXT NOT NULL REFERENCES items(id),version_id TEXT NOT NULL REFERENCES versions(id),
 body TEXT NOT NULL,reviewed INTEGER NOT NULL DEFAULT 1,updated TEXT NOT NULL,
 PRIMARY KEY(item_id,version_id));
CREATE TABLE IF NOT EXISTS gold_history(
 id INTEGER PRIMARY KEY,item_id TEXT NOT NULL,version_id TEXT NOT NULL,body TEXT NOT NULL,updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY,body TEXT NOT NULL,secret_mode TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(
 id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),version_id TEXT NOT NULL REFERENCES versions(id),
 profile_id TEXT NOT NULL,profile TEXT NOT NULL,prompt TEXT NOT NULL,split TEXT NOT NULL,
 state TEXT NOT NULL,created TEXT NOT NULL,finished TEXT,evaluation_seen INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS tasks(
 job_id TEXT NOT NULL REFERENCES jobs(id),item_id TEXT NOT NULL REFERENCES items(id),
 status TEXT NOT NULL DEFAULT 'pending',result TEXT,error TEXT,attempts INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(job_id,item_id));
CREATE TABLE IF NOT EXISTS decisions(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,kind TEXT NOT NULL,
 body TEXT NOT NULL,created TEXT NOT NULL);
PRAGMA user_version=1;
"""


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
            if "gold_snapshot" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN gold_snapshot TEXT")
                db.execute("ALTER TABLE tasks ADD COLUMN gold_reviewed INTEGER NOT NULL DEFAULT 0")
            db.execute("PRAGMA user_version=2")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def query(self, sql: str, params=()):
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, params).fetchall()]

    def one(self, sql: str, params=()):
        rows = self.query(sql, params)
        if not rows:
            raise LookupError("Объект не найден")
        return rows[0]

    def execute(self, sql: str, params=()):
        with self.connect() as db:
            db.execute(sql, params)

    def decision(self, project_id: str, kind: str, body: dict):
        self.execute("INSERT INTO decisions VALUES(?,?,?,?,?)", (uid(), project_id, kind, encode(body), now()))
