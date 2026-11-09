"""Personal features: temporary data and fake accounts; no real bookings/messages."""
import asyncio
import base64
import hashlib
import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from jarvis.browser import PersonalBrowser, public_url
from jarvis.calendar_google import GoogleCalendar
from jarvis.config import Config
from jarvis.documents import Documents
from jarvis.organizer import Organizer
from jarvis.personal import PersonalAssistant
from jarvis.security import GateError
from jarvis.service import Service
from jarvis.store import Store


class Database(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state.db")
        self.config = Config()
        self.organizer = Organizer(self.store, self.config, self.root)

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def future(self, minutes=60):
        return (datetime.now(self.config.tz()) + timedelta(minutes=minutes)).isoformat()


class OrganizerTests(Database):
    def test_preferences_inspect_delete_and_reject_credentials(self):
        self.organizer.remember("diet", "Vegetarian")
        self.assertEqual(self.store.one("SELECT value FROM memories")["value"], "Vegetarian")
        self.assertEqual(self.organizer.forget("diet"), {"deleted": 1})
        with self.assertRaises(GateError): self.organizer.remember("api_key", "fixture")

    def test_conflict_boundary_and_cancellation_reminders(self):
        a = self.organizer.appointment("Dentist", self.future(60), self.future(90))
        b = self.organizer.appointment("Overlap", self.future(70), self.future(80))
        self.assertTrue(a["created"]); self.assertFalse(b["created"])
        self.organizer.cancel_appointment(a["appointment"])
        self.assertEqual(self.store.one("SELECT state FROM reminders")["state"], "dismissed")
        self.assertEqual(self.organizer.agenda(), [])

    def test_naive_dates_use_owner_timezone_and_ics_escapes(self):
        result = self.organizer.appointment("Meeting, one\nsecond line", "2030-01-02T15:00", "2030-01-02T16:00")
        self.assertTrue(result["start"].endswith("+03:00"))
        calendar = Path(self.organizer.export_calendar()["calendar_file"]).read_bytes()
        self.assertIn(b"DTSTART:20300102T120000Z", calendar)
        self.assertIn(b"SUMMARY:Meeting\\, one\\nsecond line", calendar)
        self.assertIn(b"\r\nEND:VCALENDAR\r\n", calendar)

    def test_reschedule_replaces_reminder_and_rejects_conflicting_move(self):
        first = self.organizer.appointment("First", self.future(60), self.future(90))
        second = self.organizer.appointment("Second", self.future(120), self.future(150))
        conflict = self.organizer.reschedule(first["appointment"], self.future(125), self.future(145))
        self.assertFalse(conflict["rescheduled"])
        moved = self.organizer.reschedule(first["appointment"], self.future(180), self.future(210))
        self.assertTrue(moved["rescheduled"])
        reminders = self.store.rows("SELECT state FROM reminders WHERE appointment=?", (first["appointment"],))
        self.assertEqual(sorted(r["state"] for r in reminders), ["dismissed", "pending"])

    def test_due_claim_is_once_and_crash_recovery_is_uncertain(self):
        created = self.organizer.remind("Call dentist", self.future())
        with self.store.lock: self.store.db.execute("UPDATE reminders SET due=?", (time.time() - 1,))
        self.assertEqual(len(self.organizer.claim_due()), 1)
        self.assertEqual(self.organizer.claim_due(), [])
        self.organizer.recover()
        self.assertEqual(self.store.one("SELECT state FROM reminders")["state"], "uncertain")
        self.assertEqual(self.organizer.claim_due(), [])
        self.organizer.change_reminder(created["reminder"], "snooze", self.future(5))
        self.assertEqual(self.store.one("SELECT state FROM reminders")["state"], "pending")

    def test_recurring_missed_reminder_moves_to_future_without_flood(self):
        self.organizer.remind("Daily", self.future(), repeat="daily")
        with self.store.lock: self.store.db.execute("UPDATE reminders SET due=?", (time.time() - 86400 * 3,))
        reminder = self.organizer.claim_due()[0]
        self.organizer.delivered(reminder, True)
        row = self.store.one("SELECT * FROM reminders")
        self.assertEqual(row["state"], "pending"); self.assertGreater(row["due"], time.time())
        self.assertEqual(self.organizer.claim_due(), [])

    def test_completed_task_suppresses_conditional_followup(self):
        task = self.store.create_task(self.root, "Work")
        self.organizer.remind("Follow up", self.future(), task=task)
        self.store.transition(task, "completed")
        with self.store.lock: self.store.db.execute("UPDATE reminders SET due=?", (time.time() - 1,))
        self.assertEqual(self.organizer.claim_due(), [])
        self.assertEqual(self.store.one("SELECT state FROM reminders")["state"], "dismissed")

    def test_document_roots_protected_names_and_word_extraction(self):
        docs = Documents(self.store); docs.add_root(self.root)
        note = self.root / "meeting-notes.txt"; note.write_text("Read me")
        self.assertEqual(docs.read(str(note))["text"], "Read me")
        self.assertEqual(len(docs.find("meeting")["documents"]), 1)
        secret = self.root / "credentials.txt"; secret.write_text("fixture")
        with self.assertRaises(GateError): docs.read(str(secret))
        with tempfile.TemporaryDirectory() as outside:
            file = Path(outside) / "outside.txt"; file.write_text("Outside")
            with self.assertRaises(GateError): docs.read(str(file))
        word = self.root / "meeting.docx"
        with zipfile.ZipFile(word, "w") as archive:
            archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>Meeting decisions</w:t></w:r></w:p></w:document>')
        self.assertIn("Meeting decisions", docs.read(str(word))["text"])

    def test_sync_updates_once_and_cancels_imported_reminder(self):
        calendar = GoogleCalendar(self.root, self.organizer); calendar.token = Mock(return_value="fixture")
        event = {"id": "remote-one", "summary": "Remote meeting", "start": {"dateTime": self.future(60)}, "end": {"dateTime": self.future(90)}}
        with patch("jarvis.calendar_google.request_json", return_value={"items": [event]}):
            calendar.sync(); calendar.sync()
        self.assertEqual(len(self.store.rows("SELECT * FROM appointments")), 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM reminders")), 1)
        with patch("jarvis.calendar_google.request_json", return_value={"items": [{"id": "remote-one", "status": "cancelled"}]}): calendar.sync()
        self.assertEqual(self.store.one("SELECT state FROM reminders")["state"], "dismissed")

    def test_oauth_uses_pkce_and_rejects_wrong_callback_state(self):
        credentials = self.root / "client.json"
        credentials.write_text(json.dumps({"installed": {"client_id": "fixture.apps.googleusercontent.com", "client_secret": "fixture"}}))
        calendar = GoogleCalendar(self.root, self.organizer); calendar.vault = Mock()
        observed = {}; callbacks = []
        def open_browser(url):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query); observed.update(query)
            def callback():
                redirect = query["redirect_uri"][0]
                for state in ["wrong-state", query["state"][0]]:
                    try:
                        with urllib.request.urlopen(redirect + "?" + urllib.parse.urlencode({"state": state, "code": "fixture-code"}), timeout=5) as response: callbacks.append(response.status)
                    except urllib.error.HTTPError as error: callbacks.append(error.code)
            worker = threading.Thread(target=callback); worker.start(); observed["worker"] = worker
            return True
        def token_exchange(url, **kwargs):
            verifier = kwargs["form"]["code_verifier"]
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            self.assertEqual(observed["code_challenge"], [challenge])
            return {"access_token": "fixture", "refresh_token": "fixture-refresh", "expires_in": 3600}
        with patch("jarvis.calendar_google.webbrowser.open", side_effect=open_browser), patch("jarvis.calendar_google.request_json", side_effect=token_exchange):
            self.assertTrue(calendar.connect(credentials)["connected"])
        observed["worker"].join(5)
        self.assertEqual(callbacks, [400, 200])
        calendar.vault.set.assert_called_once()


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    def test_private_urls_and_credential_urls_are_rejected(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch("jarvis.browser.socket.getaddrinfo", return_value=answers):
            with self.assertRaises(GateError): public_url("https://example.com")
        for value in ["file:///C:/secret.txt", "javascript:alert(1)", "https://user:password@example.com"]:
            with self.assertRaises(GateError): public_url(value)

    async def test_payment_and_password_fields_require_manual_input(self):
        browser = PersonalBrowser(Path(".")); browser.page = Mock(url="https://example.com")
        browser.snapshot = {"url": "https://example.com", "elements": [{"id": "1", "tag": "input", "type": "password", "label": "Password"}]}
        with self.assertRaises(GateError): await browser.act("fill", "1", "fixture")
        browser.snapshot["elements"][0].update(type="text", label="Credit card number")
        with self.assertRaises(GateError): await browser.act("fill", "1", "fixture")
        browser.page.locator.assert_not_called()


class PersonalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.store = Store(self.root / "state.db")
        self.service = Service(Config(), self.root / "config.toml", self.root, self.store)
        self.service.text = AsyncMock(return_value=True)
        self.store.set_setting("health", "running")
        self.service.supervisor.personal_reason = AsyncMock()
        self.personal = self.service.personal

    async def asyncTearDown(self):
        await self.personal.close(); self.store.close(); self.temp.cleanup()

    async def test_voice_personal_request_does_not_create_coding_task(self):
        self.service.supervisor.converse = AsyncMock(return_value={"action": "assistant", "task_id": "", "instruction": json.dumps({"op": "remember", "key": "diet", "value": "Vegetarian"})})
        result = json.loads(await self.service.voice_command("Remember I am vegetarian"))
        self.assertEqual(result["state"], "completed")
        self.assertEqual(self.store.rows("SELECT * FROM tasks"), [])

    async def test_voice_cannot_submit_reservation_or_change_document_roots(self):
        for op in ["reservation_submit", "documents_root", "calendar_connect", "browser_attach"]:
            with self.assertRaises(GateError): await self.personal.execute({"op": op}, source="voice")

    async def test_reminder_notification_is_once_and_failed_send_does_not_replay(self):
        future = (datetime.now(self.service.config.tz()) + timedelta(hours=1)).isoformat()
        self.personal.organizer.remind("Fixture reminder", future)
        with self.store.lock: self.store.db.execute("UPDATE reminders SET due=?", (time.time() - 1,))
        self.service.text.return_value = False
        await self.personal.tick(); await self.personal.tick()
        self.service.text.assert_awaited_once()
        self.assertEqual(self.store.one("SELECT state FROM reminders")["state"], "failed")

    async def test_reminder_waits_for_transport_startup(self):
        future = (datetime.now(self.service.config.tz()) + timedelta(hours=1)).isoformat()
        self.personal.organizer.remind("After startup", future)
        with self.store.lock: self.store.db.execute("UPDATE reminders SET due=?", (time.time() - 1,))
        self.store.set_setting("health", "stopped")
        await self.personal.tick()
        self.service.text.assert_not_awaited()
        self.assertEqual(self.store.one("SELECT state FROM reminders")["state"], "pending")
        self.store.set_setting("health", "running")
        await self.personal.tick()
        self.service.text.assert_awaited_once()

    async def test_desktop_only_briefing_does_not_send_telegram(self):
        self.service.desktop_notify = Mock()
        self.store.set_setting("briefing_schedule", json.dumps({"at": "00:00", "channel": "desktop"}))
        await self.personal.tick(); await self.personal.tick()
        self.service.desktop_notify.assert_called_once()
        self.service.text.assert_not_awaited()

    async def test_routine_allowlist_and_partial_failure_receipt(self):
        with self.assertRaises(GateError):
            await self.personal.execute({"op": "routine_save", "name": "Unsafe", "steps": [{"op": "reservation_submit", "id": "x"}]}, source="text")
        await self.personal.execute({"op": "routine_save", "name": "Morning", "steps": [{"op": "briefing"}]}, source="text")
        result = await self.personal.execute({"op": "routine_run", "name": "Morning"}, source="text", background=False)
        self.assertTrue(result["result"]["completed"])

    async def test_document_summary_cites_requested_source(self):
        self.personal.documents.add_root(self.root)
        document = self.root / "meeting.txt"; document.write_text("Decided to meet Tuesday")
        self.service.supervisor.personal_reason.return_value = {"summary": "Meeting is Tuesday."}
        result = await self.personal.execute({"op": "document_summary", "path": str(document)}, source="text", background=False)
        self.assertEqual(result["result"]["source"], str(document))

    async def reservation_fixture(self):
        snapshot = {"url": "https://restaurant.example/book", "title": "Booking", "text": "Book a table",
                    "elements": [{"id": "1", "tag": "button", "type": "submit", "label": "Confirm booking", "href": "", "value": ""}]}
        browser = Mock(lock=asyncio.Lock(), read=AsyncMock(return_value=snapshot), navigate=AsyncMock(), search=AsyncMock(), act=AsyncMock(), close=AsyncMock())
        self.personal.browser = browser
        self.personal.agent = Mock(work=AsyncMock(return_value={"action": "confirm", "element": "1", "message": "Submit booking", "page_url": snapshot["url"], "sources": [snapshot["url"]]}))
        future = datetime.now(self.service.config.tz()) + timedelta(days=1)
        result = await self.personal.execute({"op": "reserve", "venue": "Fixture restaurant", "start": future.isoformat(), "end": (future + timedelta(hours=1)).isoformat(), "party_size": 2}, source="text", background=False)
        return result["result"]["reservation"], browser, snapshot

    async def test_prepared_booking_is_not_confirmation_and_stale_form_cannot_submit(self):
        identifier, browser, snapshot = await self.reservation_fixture()
        self.assertEqual(self.store.one("SELECT state FROM reservations")["state"], "awaiting_confirmation")
        self.assertFalse(self.store.rows("SELECT * FROM appointments"))
        snapshot["elements"][0]["label"] = "Different booking"
        with self.assertRaises(GateError): await self.personal._submit_reservation(identifier)
        browser.act.assert_not_awaited()

    async def test_booking_commit_once_with_concrete_page_evidence(self):
        identifier, browser, snapshot = await self.reservation_fixture()
        async def click(*args, **kwargs):
            snapshot["text"] = "Booking confirmed. Reference REF-1234 for your table."
        browser.act.side_effect = click
        self.personal.agent.work.return_value = {"action": "done", "message": "Booking page shown", "sources": []}
        self.service.supervisor.personal_reason.return_value = {"confirmed": True, "confirmation_id": "REF-1234", "evidence_quote": "Booking confirmed. Reference REF-1234 for your table."}
        result = await self.personal._submit_reservation(identifier)
        self.assertTrue(result["confirmed"])
        with self.assertRaises(GateError): await self.personal._submit_reservation(identifier)
        browser.act.assert_awaited_once()
        await self.personal._check_reservation(identifier)
        self.assertEqual(len(self.store.rows("SELECT * FROM appointments")), 1)

    async def test_model_cannot_invent_booking_confirmation(self):
        identifier, browser, snapshot = await self.reservation_fixture()
        self.service.supervisor.personal_reason.return_value = {"confirmed": True, "confirmation_id": "INVENTED", "evidence_quote": "Booking confirmed INVENTED"}
        result = await self.personal._check_reservation(identifier)
        self.assertFalse(result["confirmed"])
        self.assertEqual(self.store.rows("SELECT * FROM appointments"), [])

    async def test_possible_booking_commit_without_evidence_cannot_be_replayed(self):
        identifier, browser, snapshot = await self.reservation_fixture()
        self.service.supervisor.personal_reason.return_value = {"confirmed": False, "confirmation_id": "", "evidence_quote": ""}
        result = await self.personal._submit_reservation(identifier)
        self.assertFalse(result["confirmed"])
        self.assertEqual(self.store.one("SELECT state FROM reservations")["state"], "uncertain")
        with self.assertRaises(GateError): await self.personal._submit_reservation(identifier)
        browser.act.assert_awaited_once()

    async def test_personal_reasoning_disables_shell_tools_on_wire(self):
        client = self.service.supervisor.codex
        client.require_subscription = AsyncMock()
        client.request = AsyncMock(return_value={"thread": {"id": "planner"}, "sandbox": {"type": "readOnly"}, "approvalPolicy": "on-request"})
        await client.start_worker(str(self.root), "Plan only", ephemeral=True, reasoning_only=True)
        params = client.request.call_args.args[1]
        self.assertFalse(params["config"]["features.shell_tool"])
        self.assertTrue(params["ephemeral"])


if __name__ == "__main__": unittest.main()
