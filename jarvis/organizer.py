"""Durable personal data and evidence. No account or network dependency."""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from .security import GateError, redact


def text(value, limit=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\0" in value:
        raise GateError(f"Expected nonempty text, at most {limit} characters")
    return value.strip()


class Organizer:
    def __init__(self, store, config, root: Path):
        self.store, self.config, self.root = store, config, root
        with store.lock:
            store.db.executescript("""
            CREATE TABLE IF NOT EXISTS activity(id TEXT PRIMARY KEY,action TEXT NOT NULL,state TEXT NOT NULL,
              detail TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS memories(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS notes(id TEXT PRIMARY KEY,title TEXT NOT NULL,body TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS appointments(id TEXT PRIMARY KEY,title TEXT NOT NULL,start REAL NOT NULL,
              end REAL NOT NULL,location TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'active',remote_id TEXT,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS reminders(id TEXT PRIMARY KEY,title TEXT NOT NULL,due REAL NOT NULL,
              channel TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',repeat TEXT NOT NULL DEFAULT 'none',
              task TEXT,appointment TEXT,created REAL NOT NULL,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS routines(name TEXT PRIMARY KEY,steps TEXT NOT NULL,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS reservations(id TEXT PRIMARY KEY,state TEXT NOT NULL,payload TEXT NOT NULL,
              confirmation TEXT NOT NULL DEFAULT '',created REAL NOT NULL,updated REAL NOT NULL);
            """)

    def recover(self):
        with self.store.transaction() as db:
            db.execute("UPDATE reminders SET state='uncertain' WHERE state='sending'")
            db.execute("UPDATE activity SET state='uncertain',updated=? WHERE state IN ('running','attempting')", (time.time(),))
            db.execute("UPDATE reservations SET state='uncertain' WHERE state='submitting'")

    def receipt(self, action, detail=None, state="running"):
        identifier, now = uuid.uuid4().hex[:12], time.time()
        with self.store.lock:
            self.store.db.execute("INSERT INTO activity VALUES(?,?,?,?,?,?)",
                (identifier, action, state, redact(json.dumps(detail or {}, ensure_ascii=False)), now, now))
        return identifier

    def finish(self, identifier, state, detail):
        if state not in {"completed", "failed", "uncertain", "awaiting_user", "cancelled"}:
            raise GateError("Invalid activity result")
        with self.store.lock:
            self.store.db.execute("UPDATE activity SET state=?,detail=?,updated=? WHERE id=?",
                (state, redact(json.dumps(detail, ensure_ascii=False)), time.time(), identifier))

    def parse_time(self, value):
        try:
            date = datetime.fromisoformat(text(value, 80).replace("Z", "+00:00"))
        except ValueError:
            raise GateError("Use an ISO date/time, for example 2026-11-10T15:30+03:00") from None
        if date.tzinfo is None:
            date = date.replace(tzinfo=self.config.tz())
        return date.timestamp()

    def local_time(self, stamp):
        return datetime.fromtimestamp(stamp, self.config.tz()).isoformat(timespec="minutes")

    def remember(self, key, value):
        key, value = text(key, 100), text(value)
        if re.search(r"(?i)password|api.?key|api.?hash|secret|access.?token|refresh.?token", key):
            raise GateError("Preferences are not a credential store; use account setup for secrets")
        with self.store.lock:
            self.store.db.execute("INSERT INTO memories VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated=excluded.updated",
                (key, redact(value), time.time()))
        return {"remembered": key}

    def forget(self, key):
        with self.store.lock:
            count = self.store.db.execute("DELETE FROM memories WHERE key=?", (text(key, 100),)).rowcount
        return {"deleted": count}

    def note(self, title, body):
        identifier = uuid.uuid4().hex[:12]
        with self.store.lock:
            self.store.db.execute("INSERT INTO notes VALUES(?,?,?,?)", (identifier, text(title, 200), redact(text(body, 50000)), time.time()))
        return {"note": identifier}

    def remind(self, title, due, channel="telegram", repeat="none", task=None, appointment=None):
        if channel not in {"telegram", "desktop", "both", "call"} or repeat not in {"none", "daily", "weekly"}:
            raise GateError("Invalid reminder channel or repeat interval")
        stamp = self.parse_time(due)
        if stamp <= time.time():
            raise GateError("Reminder time must be in the future")
        if task:
            self.store.task(task)
        identifier, now = uuid.uuid4().hex[:12], time.time()
        with self.store.lock:
            self.store.db.execute("INSERT INTO reminders VALUES(?,?,?,?,?,?,?,?,?,?)",
                (identifier, text(title, 1000), stamp, channel, "pending", repeat, task, appointment, now, now))
        return {"reminder": identifier, "due": self.local_time(stamp), "channel": channel, "repeat": repeat}

    def change_reminder(self, identifier, action, due=None):
        row = self.store.one("SELECT * FROM reminders WHERE id=?", (identifier,))
        if action not in {"dismiss", "snooze", "retry"}:
            raise GateError("Use dismiss, snooze or retry")
        if row["state"] == "sending":
            raise GateError("Reminder delivery is in progress")
        stamp = self.parse_time(due) if due else time.time() + 600
        if action != "dismiss" and stamp <= time.time():
            raise GateError("Snooze time must be in the future")
        with self.store.lock:
            self.store.db.execute("UPDATE reminders SET state=?,due=?,updated=? WHERE id=?",
                ("dismissed" if action == "dismiss" else "pending", stamp, time.time(), identifier))
        return {"reminder": identifier, "state": "dismissed" if action == "dismiss" else "pending", "due": self.local_time(stamp)}

    def appointment(self, title, start, end, location="", remind_minutes=30, allow_conflict=False):
        a, b = self.parse_time(start), self.parse_time(end)
        if a >= b or b - a > 86400 * 31:
            raise GateError("Appointment end must follow start, within 31 days")
        if type(remind_minutes) is not int or not 0 <= remind_minutes <= 525600:
            raise GateError("Invalid reminder lead time")
        identifier, now = uuid.uuid4().hex[:12], time.time()
        title = text(title, 500)
        with self.store.transaction() as db:
            conflicts = [dict(row) for row in db.execute("SELECT id,title,start,end FROM appointments WHERE state='active' AND start<? AND end>?", (b, a))]
            if conflicts and not allow_conflict:
                return {"created": False, "conflicts": conflicts}
            db.execute("INSERT INTO appointments VALUES(?,?,?,?,?,'active',NULL,?)", (identifier, title, a, b, str(location)[:1000], now))
            due = a - remind_minutes * 60
            if due > now:
                db.execute("INSERT INTO reminders VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex[:12], "Appointment: " + title, due, "both", "pending", "none", None, identifier, now, now))
        return {"created": True, "appointment": identifier, "start": self.local_time(a), "end": self.local_time(b), "conflicts": conflicts}

    def cancel_appointment(self, identifier):
        self.store.one("SELECT id FROM appointments WHERE id=?", (identifier,))
        with self.store.transaction() as db:
            db.execute("UPDATE appointments SET state='cancelled',updated=? WHERE id=?", (time.time(), identifier))
            db.execute("UPDATE reminders SET state='dismissed' WHERE appointment=? AND state!='sending'", (identifier,))
        return {"cancelled": identifier, "scope": "local calendar only"}

    def reschedule(self, identifier, start, end, remind_minutes=30, preview=False, allow_conflict=False):
        a, b, now = self.parse_time(start), self.parse_time(end), time.time()
        if a >= b or b - a > 86400 * 31 or type(remind_minutes) is not int or not 0 <= remind_minutes <= 525600:
            raise GateError("Invalid appointment time or reminder lead time")
        with self.store.transaction() as db:
            event = db.execute("SELECT * FROM appointments WHERE id=? AND state='active'", (identifier,)).fetchone()
            if not event: raise GateError("Active appointment not found")
            conflicts = [dict(row) for row in db.execute("SELECT id,title FROM appointments WHERE id!=? AND state='active' AND start<? AND end>?", (identifier, b, a))]
            if conflicts and not allow_conflict: return {"rescheduled": False, "conflicts": conflicts}
            if preview: return {"would_reschedule": True, "conflicts": conflicts}
            db.execute("UPDATE appointments SET start=?,end=?,updated=? WHERE id=?", (a, b, now, identifier))
            db.execute("UPDATE reminders SET state='dismissed',updated=? WHERE appointment=? AND state!='sending'", (now, identifier))
            if a - remind_minutes * 60 > now:
                db.execute("INSERT INTO reminders VALUES(?,?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex[:12], "Appointment: " + event["title"],
                    a - remind_minutes * 60, "both", "pending", "none", None, identifier, now, now))
        return {"rescheduled": True, "appointment": identifier, "start": self.local_time(a), "end": self.local_time(b), "conflicts": conflicts}

    def agenda(self, start=None, end=None):
        today = datetime.now(self.config.tz()).replace(hour=0, minute=0, second=0, microsecond=0)
        a = self.parse_time(start) if start else today.timestamp()
        b = self.parse_time(end) if end else (today + timedelta(days=7)).timestamp()
        return [{**r, "start": self.local_time(r["start"]), "end": self.local_time(r["end"])} for r in self.store.rows(
            "SELECT id,title,start,end,location,remote_id FROM appointments WHERE state='active' AND start<? AND end>? ORDER BY start LIMIT 100", (b, a))]

    def export_calendar(self):
        def escape(value):
            return str(value).replace("\\", "\\\\").replace("\r", "").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")
        from datetime import timezone
        def utc(stamp):
            return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//JARVIS//Personal Calendar//EN"]
        for row in self.store.rows("SELECT * FROM appointments WHERE state='active' ORDER BY start"):
            lines += ["BEGIN:VEVENT", "UID:" + row["id"] + "@jarvis.local", "DTSTAMP:" + utc(row["updated"]),
                "DTSTART:" + utc(row["start"]), "DTEND:" + utc(row["end"]), "SUMMARY:" + escape(row["title"]), "LOCATION:" + escape(row["location"]), "END:VEVENT"]
        lines.append("END:VCALENDAR")
        folder = self.root / "exports"; folder.mkdir(exist_ok=True)
        path = folder / "jarvis-calendar.ics"
        folded = []
        for line in lines:
            segment = ""
            for char in line:
                if len((segment + char).encode()) > 73:
                    folded.append(segment); segment = " "
                segment += char
            folded.append(segment)
        path.write_bytes(("\r\n".join(folded) + "\r\n").encode())
        return {"calendar_file": str(path)}

    def briefing(self):
        return {"local_time": datetime.now(self.config.tz()).isoformat(timespec="minutes"),
                "agenda": self.agenda(),
                "reminders": self.store.rows("SELECT id,title,due,channel,state FROM reminders WHERE state IN ('pending','failed','uncertain') ORDER BY due LIMIT 30"),
                "coding_tasks": self.store.rows("SELECT id,state,summary FROM tasks WHERE state NOT IN ('completed','cancelled') ORDER BY updated DESC LIMIT 15"),
                "reservations": self.store.rows("SELECT id,state,payload,confirmation FROM reservations ORDER BY updated DESC LIMIT 10")}

    def claim_due(self, now=None, channels=None):
        now = now or time.time()
        channels = list(channels) if channels is not None else ["telegram", "desktop", "both", "call"]
        if not channels: return []
        claimed = []
        with self.store.transaction() as db:
            rows = db.execute("SELECT * FROM reminders WHERE state='pending' AND due<=? AND channel IN (" + ",".join("?" for _ in channels) + ") ORDER BY due LIMIT 10", (now, *channels)).fetchall()
            for row in rows:
                row = dict(row)
                if row["task"]:
                    task = db.execute("SELECT state FROM tasks WHERE id=?", (row["task"],)).fetchone()
                    if task and task["state"] in {"completed", "cancelled"}:
                        db.execute("UPDATE reminders SET state='dismissed',updated=? WHERE id=?", (now, row["id"]))
                        continue
                db.execute("UPDATE reminders SET state='sending',updated=? WHERE id=?", (now, row["id"]))
                claimed.append(row)
        return claimed

    def delivered(self, reminder, success, uncertain=False):
        now = time.time()
        state = "delivered" if success else "uncertain" if uncertain else "failed"
        due = reminder["due"]
        if success and reminder["repeat"] != "none":
            date = datetime.fromtimestamp(due, self.config.tz())
            step = timedelta(days=1 if reminder["repeat"] == "daily" else 7)
            while date.timestamp() <= now:
                date += step
            due, state = date.timestamp(), "pending"
        with self.store.lock:
            self.store.db.execute("UPDATE reminders SET state=?,due=?,updated=? WHERE id=?", (state, due, now, reminder["id"]))

    def purge(self):
        cutoff = time.time() - 90 * 86400
        with self.store.transaction() as db:
            db.execute("DELETE FROM activity WHERE updated<? AND state IN ('completed','failed','cancelled')", (cutoff,))
            db.execute("DELETE FROM reminders WHERE updated<? AND state IN ('delivered','dismissed')", (cutoff,))
