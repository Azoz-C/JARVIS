from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .security import GateError, redact

STATES = {"queued", "running", "needs_input", "awaiting_approval", "verifying",
          "completed", "failed", "cancelled", "awaiting_frontend", "frontend_submitted"}


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA foreign_keys=ON;
        PRAGMA journal_mode=WAL;
        PRAGMA secure_delete=ON;
        CREATE TABLE IF NOT EXISTS tasks(
          id TEXT PRIMARY KEY, project TEXT NOT NULL, prompt TEXT NOT NULL,
          state TEXT NOT NULL, thread TEXT, summary TEXT NOT NULL DEFAULT '',
          created REAL NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS events(
          id INTEGER PRIMARY KEY, task TEXT, kind TEXT NOT NULL, data TEXT NOT NULL, at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS proposals(
          id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id), payload TEXT NOT NULL,
          digest TEXT NOT NULL, state TEXT NOT NULL, expires REAL NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS blockers(
          id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id), question TEXT NOT NULL,
          created REAL NOT NULL, resolved INTEGER NOT NULL DEFAULT 0, attempted INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS calls(
          id TEXT PRIMARY KEY, reason TEXT NOT NULL, automatic INTEGER NOT NULL,
          state TEXT NOT NULL, created REAL NOT NULL, ended REAL);
        CREATE UNIQUE INDEX IF NOT EXISTS single_call ON calls((1)) WHERE state IN ('dialing','active');
        CREATE TABLE IF NOT EXISTS watched(thread TEXT PRIMARY KEY, title TEXT NOT NULL, handed_off INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS inbox(id TEXT PRIMARY KEY, command TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'pending', response TEXT NOT NULL DEFAULT '', created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS command_proposals(id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id),
          payload TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL, expires REAL NOT NULL, created REAL NOT NULL);
        """)

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def rows(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args)]

    def one(self, sql: str, args=()) -> dict:
        rows = self.rows(sql, args)
        if not rows:
            raise GateError("Record not found")
        return rows[0]

    def event(self, task: str | None, kind: str, data: dict):
        with self.lock:
            self.db.execute("INSERT INTO events(task,kind,data,at) VALUES(?,?,?,?)",
                            (task, kind, redact(json.dumps(data)), time.time()))

    def create_task(self, project: Path, prompt: str, thread: str | None = None) -> str:
        project = project.resolve(strict=True)
        if not project.is_dir() or not prompt.strip():
            raise GateError("A project directory and a nonempty task are required")
        task = uuid.uuid4().hex[:12]
        now = time.time()
        with self.lock:
            self.db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?)",
                            (task, str(project), redact(prompt), "queued", thread, "", now, now))
        return task

    def task(self, task: str) -> dict:
        return self.one("SELECT * FROM tasks WHERE id=?", (task,))

    def transition(self, task: str, state: str, summary: str = "", thread: str | None = None):
        if state not in STATES:
            raise GateError("Invalid task state")
        with self.lock:
            self.db.execute("UPDATE tasks SET state=?,summary=?,thread=COALESCE(?,thread),updated=? WHERE id=?",
                            (state, redact(summary), thread, time.time(), task))
        self.event(task, "state", {"state": state, "summary": summary})

    def blocker(self, task: str, question: str) -> str:
        self.task(task)
        identifier = uuid.uuid4().hex[:12]
        with self.lock:
            self.db.execute("INSERT INTO blockers(id,task,question,created) VALUES(?,?,?,?)",
                            (identifier, task, redact(question), time.time()))
        self.transition(task, "needs_input", question)
        return identifier

    def setting(self, name: str, default: str = "") -> str:
        rows = self.rows("SELECT value FROM settings WHERE key=?", (name,))
        return rows[0]["value"] if rows else default

    def set_setting(self, name: str, value: str):
        with self.lock:
            self.db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (name, value))

    def claim_command(self, identifier: str) -> bool:
        with self.lock:
            return self.db.execute("INSERT OR IGNORE INTO commands VALUES(?,?)", (identifier, time.time())).rowcount == 1

    def recover(self):
        # Never replay tasks or call attempts whose outcome is uncertain.
        with self.transaction() as db:
            db.execute("UPDATE calls SET state='interrupted',ended=? WHERE state IN ('dialing','active')", (time.time(),))
            db.execute("UPDATE tasks SET state='needs_input',summary='Service restarted; inspect before resuming',updated=? WHERE state IN ('running','verifying')", (time.time(),))
            db.execute("UPDATE proposals SET state='uncertain' WHERE state='applying'")
            db.execute("UPDATE command_proposals SET state='uncertain' WHERE state='running'")
            db.execute("UPDATE inbox SET state='uncertain',response='Service restarted; command not replayed' WHERE state IN ('pending','running')")

    def purge(self):
        now = time.time()
        with self.transaction() as db:
            db.execute("DELETE FROM events WHERE at<?", (now - 7 * 86400,))
            db.execute("DELETE FROM proposals WHERE created<? AND state!='applying'", (now - 30 * 86400,))
            db.execute("DELETE FROM command_proposals WHERE created<? AND state!='running'", (now - 30 * 86400,))
            db.execute("DELETE FROM commands WHERE created<?", (now - 30 * 86400,))
            db.execute("DELETE FROM inbox WHERE created<? AND state!='running'", (now - 7 * 86400,))
            db.execute("DELETE FROM calls WHERE created<? AND state NOT IN ('dialing','active')", (now - 30 * 86400,))
            old = (now - 30 * 86400,)
            db.execute("DELETE FROM blockers WHERE task IN (SELECT id FROM tasks WHERE updated<? AND state IN ('completed','failed','cancelled'))", old)
            db.execute("DELETE FROM tasks WHERE updated<? AND state IN ('completed','failed','cancelled') AND id NOT IN (SELECT task FROM proposals) AND id NOT IN (SELECT task FROM command_proposals) AND id NOT IN (SELECT task FROM blockers)", old)

    def close(self):
        self.db.close()
