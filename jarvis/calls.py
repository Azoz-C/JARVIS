from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

from .config import Config
from .security import GateError
from .store import Store


class CallPolicy:
    def __init__(self, config: Config, store: Store):
        self.config, self.store = config, store

    def quiet(self, now: datetime) -> bool:
        if self.store.setting("quiet") == "true":
            return True
        hour = now.astimezone(self.config.tz()).hour
        start, end = self.config.quiet_start, self.config.quiet_end
        return (hour >= start or hour < end) if start > end else start <= hour < end

    def reserve(self, peer: int, reason: str, *, automatic: bool = False,
                blockers: list[str] | None = None, now: datetime | None = None) -> str:
        c = self.config
        if not c.calls_enabled or not c.owner_id or peer != c.owner_id:
            raise GateError("Calling is disabled or recipient is not the owner")
        now = now or datetime.now(timezone.utc)
        timestamp = now.timestamp()
        with self.store.transaction() as db:
            if db.execute("SELECT 1 FROM calls WHERE state IN ('dialing','active')").fetchone():
                raise GateError("A call is already in progress")
            if automatic:
                if not c.automatic_calls or self.quiet(now):
                    raise GateError("Automatic calling is disabled or quiet hours are active")
                latest = db.execute("SELECT MAX(created) FROM calls").fetchone()[0]
                if latest is not None and timestamp - latest < c.call_cooldown_seconds:
                    raise GateError("Call cooldown is active")
                hour = db.execute("SELECT COUNT(*) FROM calls WHERE automatic=1 AND created>=?", (timestamp - 3600,)).fetchone()[0]
                midnight = now.astimezone(c.tz()).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
                day = db.execute("SELECT COUNT(*) FROM calls WHERE automatic=1 AND created>=?", (midnight,)).fetchone()[0]
                if hour >= c.hourly_calls or day >= c.daily_calls:
                    raise GateError("Automatic call frequency limit reached")
                if not blockers:
                    raise GateError("Automatic calls require unresolved blockers")
                for identifier in blockers:
                    row = db.execute("SELECT b.*,t.state FROM blockers b JOIN tasks t ON t.id=b.task WHERE b.id=?", (identifier,)).fetchone()
                    if (not row or row["resolved"] or row["attempted"]
                            or row["state"] not in ("needs_input", "awaiting_approval")
                            or timestamp - row["created"] < c.blocker_delay_seconds):
                        raise GateError("Blocker is stale, already attempted, or not yet due")
                for identifier in blockers:
                    db.execute("UPDATE blockers SET attempted=1 WHERE id=?", (identifier,))
            identifier = uuid.uuid4().hex[:12]
            db.execute("INSERT INTO calls(id,reason,automatic,state,created) VALUES(?,?,?,?,?)",
                       (identifier, reason, int(automatic), "dialing", timestamp))
        return identifier

    def active(self, identifier: str):
        with self.store.lock:
            self.store.db.execute("UPDATE calls SET state='active' WHERE id=? AND state='dialing'", (identifier,))

    def finish(self, identifier: str, state: str = "ended"):
        if state not in {"ended", "failed", "unanswered", "interrupted"}:
            raise GateError("Invalid call result")
        with self.store.lock:
            self.store.db.execute("UPDATE calls SET state=?,ended=? WHERE id=?", (state, time.time(), identifier))

    def due(self) -> list[dict]:
        return self.store.rows("""SELECT b.* FROM blockers b JOIN tasks t ON t.id=b.task
          WHERE b.resolved=0 AND b.attempted=0 AND b.created<=?
          AND t.state IN ('needs_input','awaiting_approval') ORDER BY b.created""",
                               (time.time() - self.config.blocker_delay_seconds,))

