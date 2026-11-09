import asyncio
import hashlib
import json
import os
import struct
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from jarvis.approvals import ApprovalBroker, safe_path, sha
from jarvis.audio import Playback, Resampler, Utterances
from jarvis.calls import CallPolicy
from jarvis.config import Config
from jarvis.security import GateError, redact, _dpapi
from jarvis.store import Store
from jarvis.supervisor import Supervisor


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.store = Store(self.root / "state.sqlite3")
        self.task = self.store.create_task(self.project, "Fix the example")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()


class ApprovalTests(DatabaseTest):
    def proposal(self, content="after\n", ttl=3600):
        path = self.project / "sample.txt"
        path.write_bytes(b"before\r\n")
        broker = ApprovalBroker(self.store)
        identifier = broker.propose(self.task, [{"path": "sample.txt", "before_sha256": sha(path.read_bytes()), "content": content}], ttl)
        return broker, identifier, path

    def test_no_edits_before_approval_and_exact_readback(self):
        broker, identifier, path = self.proposal()
        self.assertEqual(path.read_bytes(), b"before\r\n")
        self.assertIn("+after", broker.preview(identifier))
        broker.approve_and_apply(identifier, broker.get(identifier)["digest"])
        self.assertEqual(path.read_bytes(), b"after\n")
        self.assertEqual(self.store.task(self.task)["state"], "needs_input")
        self.assertEqual(broker.get(identifier)["state"], "applied")

    def test_wrong_digest_rejected_without_edits(self):
        broker, identifier, path = self.proposal()
        with self.assertRaises(GateError):
            broker.approve_and_apply(identifier, "bad")
        self.assertEqual(path.read_bytes(), b"before\r\n")

    def test_stale_snapshot_rejected(self):
        broker, identifier, path = self.proposal()
        path.write_bytes(b"concurrent edit")
        with self.assertRaises(GateError):
            broker.approve_and_apply(identifier, broker.get(identifier)["digest"])
        self.assertEqual(path.read_bytes(), b"concurrent edit")

    def test_rejection_and_replay(self):
        broker, identifier, path = self.proposal()
        digest = broker.get(identifier)["digest"]
        broker.reject(identifier)
        with self.assertRaises(GateError):
            broker.approve_and_apply(identifier, digest)
        other = broker.propose(self.task, [{"path": "new.txt", "before_sha256": None, "content": "new"}])
        digest = broker.get(other)["digest"]
        broker.approve_and_apply(other, digest)
        with self.assertRaises(GateError):
            broker.approve_and_apply(other, digest)

    def test_expiration(self):
        broker, identifier, _ = self.proposal()
        self.store.db.execute("UPDATE proposals SET expires=0 WHERE id=?", (identifier,))
        with self.assertRaises(GateError):
            broker.approve_and_apply(identifier, broker.get(identifier)["digest"])

    def test_traversal_reserved_and_sensitive_paths(self):
        for path in ("../escape", "/absolute", "C:/file", "a\\b", ".git/config", "a/.env", "x:stream", "NUL.txt", "foo.", "a//b"):
            with self.subTest(path=path), self.assertRaises(GateError):
                safe_path(self.project, path)

    def test_case_insensitive_duplicate_and_stale_creation(self):
        broker = ApprovalBroker(self.store)
        with self.assertRaises(GateError):
            broker.propose(self.task, [{"path": p, "before_sha256": None, "content": "x"} for p in ("A.txt", "a.txt")])
        identifier = broker.propose(self.task, [{"path": "new.txt", "before_sha256": None, "content": "x"}])
        (self.project / "new.txt").write_text("other")
        with self.assertRaises(GateError):
            broker.approve_and_apply(identifier, broker.get(identifier)["digest"])

    def test_hardlink_rejected(self):
        target = self.project / "a.txt"
        target.write_text("a")
        os.link(target, self.project / "b.txt")
        with self.assertRaises(GateError):
            safe_path(self.project, "a.txt")

    def test_multi_file_validation_before_writes(self):
        (self.project / "a").write_bytes(b"a")
        (self.project / "b").write_bytes(b"b")
        broker = ApprovalBroker(self.store)
        identifier = broker.propose(self.task, [{"path": p, "before_sha256": sha(p.encode()), "content": "new"} for p in ("a", "b")])
        (self.project / "b").write_bytes(b"changed")
        with self.assertRaises(GateError):
            broker.approve_and_apply(identifier, broker.get(identifier)["digest"])
        self.assertEqual((self.project / "a").read_bytes(), b"a")


class CallTests(DatabaseTest):
    def policy(self, **kwargs):
        return CallPolicy(replace(Config(), owner_id=123, calls_enabled=True, automatic_calls=True, **kwargs), self.store)

    def due_blocker(self):
        blocker = self.store.blocker(self.task, "Need a decision")
        self.store.db.execute("UPDATE blockers SET created=? WHERE id=?", (time.time() - 120, blocker))
        return blocker

    def test_owner_only_and_disabled(self):
        with self.assertRaises(GateError):
            self.policy().reserve(999, "wrong")
        with self.assertRaises(GateError):
            CallPolicy(Config(), self.store).reserve(123, "disabled")

    def test_requested_call_bypasses_quiet_hours_but_not_single_call(self):
        policy = self.policy()
        midnight = datetime(2026, 11, 8, 22, tzinfo=timezone.utc)
        self.assertTrue(policy.quiet(midnight))
        identifier = policy.reserve(123, "explicit", now=midnight)
        with self.assertRaises(GateError):
            policy.reserve(123, "second")
        policy.finish(identifier)

    def test_auto_quiet_and_resolved_blockers(self):
        policy = self.policy()
        blocker = self.due_blocker()
        with self.assertRaises(GateError):
            policy.reserve(123, "auto", automatic=True, blockers=[blocker], now=datetime(2026, 11, 8, 22, tzinfo=timezone.utc))
        self.store.db.execute("UPDATE blockers SET resolved=1 WHERE id=?", (blocker,))
        with self.assertRaises(GateError):
            policy.reserve(123, "auto", automatic=True, blockers=[blocker], now=datetime.now(timezone.utc).replace(hour=12))

    def test_no_automatic_redial_and_frequency_limit(self):
        policy = self.policy(call_cooldown_seconds=0, hourly_calls=1)
        blocker = self.due_blocker()
        now = datetime.now(timezone.utc).replace(hour=12)
        self.store.db.execute("UPDATE blockers SET created=? WHERE id=?", (now.timestamp() - 120, blocker))
        identifier = policy.reserve(123, "blocker", automatic=True, blockers=[blocker], now=now)
        policy.finish(identifier, "unanswered")
        self.assertEqual(self.store.one("SELECT * FROM blockers WHERE id=?", (blocker,))["attempted"], 1)
        with self.assertRaises(GateError):
            policy.reserve(123, "retry", automatic=True, blockers=[blocker], now=now)
        other = self.due_blocker()
        with self.assertRaises(GateError):
            policy.reserve(123, "new", automatic=True, blockers=[other], now=now)

    def test_restart_does_not_replay_calls_tasks_or_patch(self):
        policy = self.policy()
        call = policy.reserve(123, "requested")
        self.store.transition(self.task, "running")
        self.store.recover()
        self.assertEqual(self.store.one("SELECT state FROM calls WHERE id=?", (call,))["state"], "interrupted")
        self.assertEqual(self.store.task(self.task)["state"], "needs_input")

    def test_command_deduplication(self):
        self.assertTrue(self.store.claim_command("telegram:1"))
        self.assertFalse(self.store.claim_command("telegram:1"))

    def test_pending_local_commands_are_not_replayed_after_restart(self):
        self.store.db.execute("INSERT INTO inbox(id,command,created) VALUES(?,?,?)", ("old", "/call", time.time()))
        self.store.recover()
        self.assertEqual(self.store.one("SELECT * FROM inbox WHERE id='old'")["state"], "uncertain")


class AudioTests(unittest.TestCase):
    def test_chunked_resampling_matches_whole_stream(self):
        pcm = struct.pack("<4800h", *[int(10000 * __import__('math').sin(i / 20)) for i in range(4800)])
        whole = Resampler(48000, 16000).push(pcm)
        converter = Resampler(48000, 16000)
        chunks = b"".join(converter.push(pcm[i:i + 960]) for i in range(0, len(pcm), 960))
        self.assertEqual(chunks, whole)

    def test_interrupt_erases_audio_and_buffer_is_bounded(self):
        playback = Playback()
        playback.push(b"\x01\0" * 480)
        playback.clear()
        self.assertEqual(playback.frame(), b"\0" * 960)
        with self.assertRaises(BufferError):
            playback.push(b"\0" * 25000)

    def test_vad_still_hears_new_speech(self):
        vad = Utterances()
        speech = struct.pack("<1600h", *([2000] * 1600))
        for _ in range(5):
            self.assertIsNone(vad.push(speech))
        utterance = None
        for _ in range(8):
            utterance = vad.push(b"\0" * 3200) or utterance
        self.assertIsNotNone(utterance)


class SecurityTests(unittest.TestCase):
    def test_redaction(self):
        self.assertNotIn("abcdef", redact("api_key=abcdef password: abcdef"))

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_dpapi_roundtrip(self):
        encrypted = _dpapi(b"fixture-not-real-secret", True)
        self.assertNotIn(b"fixture-not-real-secret", encrypted)
        self.assertEqual(_dpapi(encrypted, False), b"fixture-not-real-secret")


if __name__ == "__main__":
    unittest.main()
