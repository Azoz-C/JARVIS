"""Google Calendar desktop OAuth with PKCE and owner-local DPAPI tokens."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .security import GateError, Vault

SCOPE = "https://www.googleapis.com/auth/calendar.events"


def request_json(url, *, method="GET", payload=None, form=None, bearer=None):
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode(); headers["Content-Type"] = "application/json"
    if form is not None:
        data = urllib.parse.urlencode(form).encode(); headers["Content-Type"] = "application/x-www-form-urlencoded"
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=25) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise GateError("Calendar response exceeded its size limit")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        raise GateError(f"Google Calendar request failed with HTTP {error.code}; check account access and API setup") from None
    except urllib.error.URLError:
        raise GateError("Google Calendar could not be reached") from None


class GoogleCalendar:
    def __init__(self, root, organizer):
        self.vault, self.organizer = Vault(root / "secrets"), organizer
        self.lock = threading.RLock()
        self.cancel_connect = threading.Event()

    def connected(self):
        return (self.vault.root / "google_calendar_tokens.dpapi").is_file()

    def connect(self, credentials_path):
        path = Path(credentials_path).expanduser().resolve(strict=True)
        if path.stat().st_size > 100000:
            raise GateError("OAuth client file is too large")
        installed = json.loads(path.read_text("utf-8")).get("installed", {})
        client_id = installed.get("client_id", "")
        if not client_id.endswith(".apps.googleusercontent.com"):
            raise GateError("Select the downloaded Google Desktop app OAuth client JSON file")
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        result = {}
        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                valid = urllib.parse.urlsplit(self.path).path == "/oauth/callback" and hmac.compare_digest(query.get("state", [""])[0], state)
                if not valid:
                    self.send_response(400); self.end_headers(); self.wfile.write(b"Invalid authorization response."); return
                result.update({"code": query.get("code", [""])[0], "error": query.get("error", [""])[0]})
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers()
                self.wfile.write(b"<p>Authorization received. You can close this tab and return to JARVIS.</p>")
        server = HTTPServer(("127.0.0.1", 0), Callback)
        server.timeout = 1
        redirect = f"http://127.0.0.1:{server.server_port}/oauth/callback"
        try:
            url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
                "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
                "state": state, "code_challenge": challenge, "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent"})
            if not webbrowser.open(url):
                raise GateError("Could not open the system browser for calendar authorization")
            deadline = time.monotonic() + 180
            while not result and time.monotonic() < deadline and not self.cancel_connect.is_set():
                server.handle_request()
            if not result.get("code"):
                raise GateError("Calendar authorization was cancelled or timed out; no account was connected")
            tokens = request_json("https://oauth2.googleapis.com/token", method="POST", form={
                "client_id": client_id, "client_secret": installed.get("client_secret", ""), "code": result["code"],
                "code_verifier": verifier, "redirect_uri": redirect, "grant_type": "authorization_code"})
            if not tokens.get("refresh_token"):
                raise GateError("Google did not supply offline calendar access; reconnect with consent")
            tokens.update({"client_id": client_id, "client_secret": installed.get("client_secret", ""),
                           "expires_at": time.time() + tokens.get("expires_in", 3600)})
            self.vault.set("google_calendar_tokens", json.dumps(tokens))
            return {"connected": True, "calendar": "Google primary", "scope": "calendar events", "billing": "no paid API enabled by JARVIS"}
        finally:
            server.server_close()

    def token(self):
        with self.lock:
            tokens = json.loads(self.vault.get("google_calendar_tokens"))
            if tokens.get("expires_at", 0) <= time.time() + 60:
                fresh = request_json("https://oauth2.googleapis.com/token", method="POST", form={
                    "client_id": tokens["client_id"], "client_secret": tokens.get("client_secret", ""),
                    "refresh_token": tokens["refresh_token"], "grant_type": "refresh_token"})
                tokens.update(fresh); tokens["expires_at"] = time.time() + fresh.get("expires_in", 3600)
                self.vault.set("google_calendar_tokens", json.dumps(tokens))
            return tokens["access_token"]

    def sync(self):
        organizer, token = self.organizer, self.token()
        now = datetime.now(organizer.config.tz())
        params = {"timeMin": (now - timedelta(days=1)).isoformat(), "timeMax": (now + timedelta(days=90)).isoformat(),
                  "singleEvents": "true", "showDeleted": "true", "maxResults": 250}
        count = 0
        for _ in range(10):
            page = request_json("https://www.googleapis.com/calendar/v3/calendars/primary/events?" + urllib.parse.urlencode(params), bearer=token)
            for event in page.get("items", []):
                remote = event["id"]
                existing = organizer.store.rows("SELECT id FROM appointments WHERE remote_id=?", (remote,))
                if event.get("status") == "cancelled":
                    for row in existing:
                        organizer.cancel_appointment(row["id"])
                    continue
                start, end = event.get("start", {}), event.get("end", {})
                if not (start.get("dateTime") or start.get("date")) or not (end.get("dateTime") or end.get("date")):
                    continue
                a, b = organizer.parse_time(start.get("dateTime") or start["date"]), organizer.parse_time(end.get("dateTime") or end["date"])
                title = str(event.get("summary", "Appointment"))[:500]
                if existing:
                    identifier = existing[0]["id"]
                    with organizer.store.transaction() as db:
                        db.execute("UPDATE appointments SET title=?,start=?,end=?,location=?,state='active',updated=? WHERE id=?",
                            (title, a, b, str(event.get("location", ""))[:1000], time.time(), identifier))
                        db.execute("UPDATE reminders SET title=?,due=?,updated=? WHERE appointment=? AND state='pending'",
                            ("Appointment: " + title, a - 1800, time.time(), identifier))
                else:
                    created = organizer.appointment(title, organizer.local_time(a), organizer.local_time(b), event.get("location", ""), allow_conflict=True)
                    identifier = created["appointment"]
                    with organizer.store.lock:
                        organizer.store.db.execute("UPDATE appointments SET remote_id=? WHERE id=?", (remote, identifier))
                count += 1
            cursor = page.get("nextPageToken")
            if not cursor:
                return {"synced": count, "window_days": 90}
            params["pageToken"] = cursor
        return {"synced": count, "partial": True, "message": "Calendar page limit reached"}

    def push(self, identifier):
        organizer = self.organizer
        event = organizer.store.one("SELECT * FROM appointments WHERE id=? AND state='active'", (identifier,))
        remote = event["remote_id"] or hashlib.sha256(("jarvis:" + identifier).encode()).hexdigest()
        payload = {"id": remote, "summary": event["title"], "location": event["location"],
                   "start": {"dateTime": organizer.local_time(event["start"])}, "end": {"dateTime": organizer.local_time(event["end"])}}
        base = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
        result = request_json(base + ("/" + urllib.parse.quote(remote, safe="") if event["remote_id"] else ""),
            method="PUT" if event["remote_id"] else "POST", payload=payload, bearer=self.token())
        with organizer.store.lock:
            organizer.store.db.execute("UPDATE appointments SET remote_id=? WHERE id=?", (result["id"], identifier))
        return {"appointment": identifier, "google_event": result["id"], "confirmation_url": result.get("htmlLink")}

    def cancel(self, identifier):
        event = self.organizer.store.one("SELECT * FROM appointments WHERE id=?", (identifier,))
        if event["remote_id"]:
            request_json("https://www.googleapis.com/calendar/v3/calendars/primary/events/" + urllib.parse.quote(event["remote_id"], safe=""),
                         method="DELETE", bearer=self.token())
        result = self.organizer.cancel_appointment(identifier)
        result["scope"] = "Google and local calendar" if event["remote_id"] else "local calendar"
        return result

    def reschedule(self, identifier, start, end, remind_minutes=30):
        preview = self.organizer.reschedule(identifier, start, end, remind_minutes, preview=True)
        if not preview.get("would_reschedule"): return preview
        event = self.organizer.store.one("SELECT * FROM appointments WHERE id=?", (identifier,))
        if event["remote_id"]:
            request_json("https://www.googleapis.com/calendar/v3/calendars/primary/events/" + urllib.parse.quote(event["remote_id"], safe=""),
                method="PATCH", payload={"start": {"dateTime": self.organizer.local_time(self.organizer.parse_time(start))},
                                         "end": {"dateTime": self.organizer.local_time(self.organizer.parse_time(end))}}, bearer=self.token())
        return self.organizer.reschedule(identifier, start, end, remind_minutes, allow_conflict=True)
