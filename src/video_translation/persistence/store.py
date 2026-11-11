"""Transactional record store. Mutations hold a SQLite write transaction."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def identifier() -> str:
    return uuid.uuid4().hex


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        with sqlite3.connect(self.path, timeout=30) as setup:
            setup.execute("PRAGMA journal_mode=WAL")
        with self.connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS records (kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(kind,id))"
            )
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('schema_version','1')")

    @contextmanager
    def connection(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=30)
            try:
                db.execute("PRAGMA busy_timeout=30000")
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def put(self, kind: str, data: dict):
        if "id" not in data:
            raise ValueError("Record requires id")
        with self.connection() as db:
            db.execute(
                "INSERT INTO records VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data",
                (kind, data["id"], json.dumps(data, ensure_ascii=False), data.get("created_at", now())),
            )
        return data

    def get(self, kind: str, record_id: str) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT data FROM records WHERE kind=? AND id=?", (kind, record_id)).fetchone()
        return json.loads(row[0]) if row else None

    def require(self, kind: str, record_id: str) -> dict:
        record = self.get(kind, record_id)
        if record is None:
            raise KeyError(f"{kind}: {record_id}")
        return record

    def list(self, kind: str) -> list[dict]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT data FROM records WHERE kind=? ORDER BY created_at DESC,id", (kind,)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def update(self, kind: str, record_id: str, mutate: Callable[[dict], None]) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT data FROM records WHERE kind=? AND id=?", (kind, record_id)).fetchone()
            if row is None:
                raise KeyError(f"{kind}: {record_id}")
            data = json.loads(row[0])
            mutate(data)
            data["updated_at"] = now()
            db.execute(
                "UPDATE records SET data=? WHERE kind=? AND id=?",
                (json.dumps(data, ensure_ascii=False), kind, record_id),
            )
        return data

    def delete(self, kind: str, record_id: str):
        with self.connection() as db:
            cursor = db.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, record_id))
            if cursor.rowcount == 0:
                raise KeyError(record_id)


class WorkerLock:
    """OS-owned byte lock released by process termination; no stale PID guessing."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0)
        self.handle.write(b"0")
        self.handle.flush()
        self.handle.seek(0)
        try:
            if __import__("os").name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            raise RuntimeError("此运行目录已有后台执行器；请勿使用多个worker") from None

    def release(self):
        if self.handle:
            self.handle.close()
            self.handle = None
