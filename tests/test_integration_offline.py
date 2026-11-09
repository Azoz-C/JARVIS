"""Integration tests use fake transports/providers: no accounts, calls or API use."""
import asyncio
import json
import tempfile
import struct
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from jarvis.codex import CodexClient
from jarvis.config import Config
from jarvis.security import GateError
from jarvis.service import Service
from jarvis.store import Store
from jarvis.supervisor import Supervisor
from jarvis.voice import GeminiVoice
from jarvis.transport import NativeTransport
from jarvis.projects import ProjectRegistry


class FakeCodex:
    def __init__(self, reports):
        self.notifications = asyncio.Queue()
        self.reports = iter(reports)
        self.threads = 0
        self.turns = 0
        self.on_request = None
        self.interrupt = AsyncMock()
        self.close = AsyncMock()
        self.name_thread = AsyncMock()

    async def start(self):
        pass

    async def start_worker(self, project, instructions=None, **kwargs):
        self.threads += 1
        return f"thread-{self.threads}"

    async def resume(self, thread, project):
        return thread

    async def turn(self, thread, project, prompt, **kwargs):
        self.turns += 1
        turn = f"turn-{self.turns}"
        report = next(self.reports)
        for event in (
            {"method": "item/completed", "params": {"threadId": thread, "turnId": turn,
             "item": {"type": "agentMessage", "text": json.dumps(report)}}},
            {"method": "turn/completed", "params": {"threadId": thread, "turn": {"id": turn, "status": "completed"}}},
        ):
            self.notifications.put_nowait(event)
        return {"turn": {"id": turn}}


class AsyncDatabaseTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.store = Store(self.root / "state.db")

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()


class SupervisorTests(AsyncDatabaseTest):
    async def test_visible_task_opens_named_chat_before_submitting(self):
        codex = FakeCodex([{"summary": "Static review only; no tests run", "question": "", "changes": []}])
        supervisor = Supervisor(replace(Config(), codex_task_mode="visible"), self.store, codex)
        await supervisor.start()
        try:
            task = supervisor.submit(self.project, "Inspect read-only")
            counts = []
            def opened(link):
                counts.append(codex.turns)
                self.assertEqual(link, "codex://threads/thread-1")
            with patch("jarvis.frontend.open_link", side_effect=opened) as open_chat:
                await supervisor.run(task)
                await asyncio.gather(*list(supervisor.runners.values()))
            self.assertEqual(open_chat.call_count, 2)
            self.assertEqual(counts, [0, 1])
            codex.name_thread.assert_awaited_once()
            self.assertEqual(codex.turns, 1)
            self.assertEqual(self.store.task(task)["state"], "completed")
            self.assertFalse(supervisor.task_evidence(task)["verification_recorded"])
        finally:
            await supervisor.close()

    async def test_visible_task_does_not_submit_if_frontend_open_fails(self):
        codex = FakeCodex([])
        supervisor = Supervisor(replace(Config(), codex_task_mode="visible"), self.store, codex)
        await supervisor.start()
        try:
            task = supervisor.submit(self.project, "Inspect read-only")
            with patch("jarvis.frontend.open_link", side_effect=GateError("not available")):
                await supervisor.run(task)
                await asyncio.gather(*list(supervisor.runners.values()))
            self.assertEqual(codex.turns, 0)
            self.assertEqual(self.store.task(task)["state"], "needs_input")
        finally:
            await supervisor.close()

    async def test_frontend_submission_prepares_draft_without_starting_worker(self):
        codex = FakeCodex([])
        supervisor = Supervisor(replace(Config(), codex_task_mode="frontend"), self.store, codex)
        task = supervisor.submit(self.project, "Review security & report findings")
        with patch("jarvis.frontend.open_link") as opened:
            await supervisor.run(task)
        self.assertEqual(self.store.task(task)["state"], "awaiting_frontend")
        self.assertEqual(codex.turns, 0)
        self.assertEqual(codex.threads, 0)
        self.assertFalse(supervisor.runners)
        query = parse_qs(urlsplit(opened.call_args.args[0]).query)
        self.assertEqual(query["path"], [str(self.project.resolve())])
        self.assertIn("Review security & report findings", query["prompt"][0])
        self.assertTrue(Path(self.store.setting("frontend_prompt:" + task)).is_file())

    async def test_frontend_open_failure_never_falls_back_to_background_worker(self):
        codex = FakeCodex([])
        supervisor = Supervisor(replace(Config(), codex_task_mode="frontend"), self.store, codex)
        task = supervisor.submit(self.project, "Review")
        with patch("jarvis.frontend.open_link", side_effect=GateError("not registered")):
            await supervisor.run(task)
        self.assertEqual(self.store.task(task)["state"], "awaiting_frontend")
        self.assertEqual(codex.turns, 0)
        self.assertFalse(supervisor.task_evidence(task)["verification_recorded"])

    async def test_completed_review_is_not_recorded_as_executed_verification(self):
        supervisor = Supervisor(Config(), self.store, FakeCodex([]))
        task = supervisor.submit(self.project, "Security review")
        self.store.transition(task, "completed", "Static review finished", "visible-thread")
        evidence = supervisor.task_evidence(task)
        self.assertFalse(evidence["verification_recorded"])
        self.assertEqual(evidence["verification_results"], [])
        self.assertEqual(evidence["codex_chat"], "codex://threads/visible-thread")
        self.store.event(task, "verification_result", {"exit_code": 0, "stdout": "1 passed", "stderr": ""})
        self.assertTrue(supervisor.task_evidence(task)["verification_recorded"])

    async def test_catalog_deduplicates_chats_and_bounds_project_selection(self):
        codex = FakeCodex([])
        codex.require_subscription = AsyncMock()
        chat = {"id": "one", "name": "Project work", "cwd": str(self.project), "status": {"type": "idle"}}
        codex.list_threads = AsyncMock(return_value={"data": [chat, chat], "nextCursor": None})
        supervisor = Supervisor(Config(), self.store, codex, registry=ProjectRegistry(self.root / "codex"))
        catalog = await supervisor.project_catalog()
        self.assertTrue(catalog["connected"])
        self.assertEqual(len(catalog["chats"]), 1)
        self.assertEqual(len(catalog["projects"]), 1)
        with self.assertRaises(GateError):
            await supervisor.select_voice_project("invented-project")
        name = await supervisor.select_voice_project(catalog["projects"][0]["id"])
        self.assertEqual(name, "project")
        self.assertEqual(self.store.setting("selected_project"), str(self.project.resolve()))
        self.assertEqual(codex.list_threads.await_count, 2)  # Unknown selection forces one refresh.

    async def test_failed_catalog_reports_connection_failure(self):
        codex = FakeCodex([])
        codex.require_subscription = AsyncMock(side_effect=GateError("not connected"))
        supervisor = Supervisor(Config(), self.store, codex, registry=ProjectRegistry(self.root / "codex"))
        catalog = await supervisor.project_catalog()
        self.assertFalse(catalog["connected"])
        self.assertEqual(catalog["projects"], [])

    async def test_worker_review_approval_pipeline_never_writes_before_owner(self):
        reports = [
            {"summary": "Proposed a new file", "question": "", "changes": [{"path": "new.txt", "before_sha256": None, "content": "approved content"}]},
            {"approved": True, "concerns": ""},
        ]
        codex = FakeCodex(reports)
        supervisor = Supervisor(Config(), self.store, codex)
        await supervisor.start()
        try:
            task = supervisor.submit(self.project, "Create a new file")
            await supervisor.run(task)
            await asyncio.gather(*list(supervisor.runners.values()))
            self.assertEqual(self.store.task(task)["state"], "awaiting_approval")
            self.assertFalse((self.project / "new.txt").exists())
            proposal = self.store.one("SELECT * FROM proposals WHERE task=?", (task,))
            supervisor.approvals.approve_and_apply(proposal["id"], proposal["digest"])
            self.assertEqual((self.project / "new.txt").read_text(), "approved content")
            self.assertEqual(codex.threads, 2)
            self.assertEqual(self.store.rows("SELECT * FROM blockers WHERE resolved=0"), [])
        finally:
            await supervisor.close()

    async def test_reviewer_concerns_block_proposal(self):
        codex = FakeCodex([
            {"summary": "Proposed", "question": "", "changes": [{"path": "x", "before_sha256": None, "content": "x"}]},
            {"approved": False, "concerns": "Missing error handling"},
        ])
        supervisor = Supervisor(Config(), self.store, codex)
        await supervisor.start()
        try:
            task = supervisor.submit(self.project, "Make x")
            await supervisor.run(task)
            await asyncio.gather(*list(supervisor.runners.values()))
            self.assertEqual(self.store.task(task)["state"], "needs_input")
            self.assertFalse(self.store.rows("SELECT * FROM proposals"))
            self.assertFalse((self.project / "x").exists())
        finally:
            await supervisor.close()

    async def test_question_creates_blocker_without_proposal(self):
        codex = FakeCodex([{"summary": "Need details", "question": "Which format?", "changes": []}])
        supervisor = Supervisor(Config(), self.store, codex)
        await supervisor.start()
        try:
            task = supervisor.submit(self.project, "Export")
            await supervisor.run(task)
            await asyncio.gather(*list(supervisor.runners.values()))
            self.assertEqual(self.store.task(task)["state"], "needs_input")
            self.assertEqual(self.store.one("SELECT * FROM blockers")["question"], "Which format?")
        finally:
            await supervisor.close()


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_key_account_is_refused_before_model_turn(self):
        client = CodexClient()
        client.request = AsyncMock(return_value={"account": {"type": "apiKey"}})
        with self.assertRaises(GateError):
            await client.start_worker("C:/project")
        self.assertEqual(client.request.await_count, 1)
        self.assertEqual(client.request.call_args.args[0], "account/read")

    async def test_file_and_command_approvals_are_declined(self):
        client = CodexClient()
        client._write = AsyncMock()
        for method in ("item/fileChange/requestApproval", "item/commandExecution/requestApproval"):
            await client._server_request({"id": 12, "method": method, "params": {}})
            self.assertEqual(client._write.call_args.args[0]["result"], {"decision": "decline"})

    async def test_handoff_refuses_active_chat_without_resume(self):
        client = CodexClient()
        client.require_subscription = AsyncMock()
        client.read_thread = AsyncMock(return_value={"thread": {"cwd": "C:/project", "status": {"type": "active"}}})
        client.request = AsyncMock()
        with self.assertRaises(GateError):
            await client.resume("chat", "C:/project")
        client.request.assert_not_called()

    async def test_cloud_cannot_open_without_free_tier_confirmation(self):
        voice = GeminiVoice(Config(), "fixture", AsyncMock())
        with self.assertRaises(GateError):
            await voice.open()
        self.assertIsNone(voice.socket)

    async def test_turn_uses_supported_readonly_wire_shape(self):
        client = CodexClient()
        client.require_subscription = AsyncMock()
        client.request = AsyncMock(return_value={"turn": {"id": "fixture"}})
        await client.turn("thread", "C:/fixture", "Inspect only")
        params = client.request.call_args.args[1]
        self.assertEqual(params["sandboxPolicy"], {"type": "readOnly", "networkAccess": False})
        self.assertNotIn("access", params["sandboxPolicy"])


class ServiceTests(AsyncDatabaseTest):
    async def test_frontend_verification_is_draft_only_and_cannot_use_background_approval(self):
        service = self.service()
        config = replace(service.config, codex_task_mode="frontend")
        service.config = service.supervisor.config = config
        task = service.supervisor.submit(self.project, "Review")
        with patch("jarvis.frontend.open_link"):
            response = await service.dispatch('/verify ' + task + ' | ["python", "-m", "unittest"]')
        self.assertIn("No background task or verification test was started", response)
        self.assertEqual(len(self.store.rows("SELECT * FROM command_proposals")), 0)
        with self.assertRaises(GateError):
            await service.dispatch("/approve-command anything digest")

    async def test_voice_selects_only_a_discovered_project(self):
        service = self.service()
        service.supervisor.converse = AsyncMock(return_value={"reply": "Selecting", "action": "select_project", "task_id": "", "instruction": "", "project_id": "known"})
        service.supervisor.select_voice_project = AsyncMock(return_value="Project")
        self.assertIn("Selected Project", await service.voice_command("Use Project"))
        service.supervisor.select_voice_project.assert_awaited_once_with("known")

    def service(self):
        config = replace(Config(), owner_id=123, calls_enabled=True)
        service = Service(config, self.root / "config.toml", self.root, self.store)
        service.send = AsyncMock()
        return service

    async def test_second_call_request_preserves_existing_call(self):
        service = self.service()
        await service.dial("First request")
        identifier = service.call_id
        await service.dial("Second request")
        self.assertEqual(service.call_id, identifier)
        self.assertEqual(self.store.one("SELECT * FROM calls")["state"], "dialing")

    async def test_hangup_leaves_task_running(self):
        service = self.service()
        task = self.store.create_task(self.project, "Work")
        self.store.transition(task, "running")
        await service.dial("Request")
        await service.end_call()
        self.assertEqual(self.store.task(task)["state"], "running")

    async def test_audio_producer_waits_for_native_consumption(self):
        service = self.service()
        service.voice = object()
        service.call_id = "audio-test"
        service.send = AsyncMock()
        for _ in range(13):
            service.outbound.put_nowait(bytes(960))
        player = asyncio.create_task(service._play_audio())
        try:
            await asyncio.sleep(.03)
            self.assertEqual(service.send.await_count, 12)
            await asyncio.sleep(.03)
            self.assertEqual(service.send.await_count, 12)
            service.audio_outstanding -= 960
            service.audio_ready.set()
            await asyncio.sleep(.03)
            self.assertEqual(service.send.await_count, 13)
        finally:
            player.cancel()
            await asyncio.gather(player, return_exceptions=True)

    async def test_interruption_releases_discarded_audio_frame(self):
        transport = NativeTransport(Config(), self.root)
        transport.call_id = "audio-test"
        transport.emit = AsyncMock()
        transport.playback.push(bytes(960))
        await transport.command({"type": "interrupt", "call_id": "audio-test"})
        self.assertEqual(len(transport.playback.buffer), 0)
        transport.emit.assert_awaited_once_with({"type": "audio_ready", "call_id": "audio-test", "consumed": 960})

    async def test_audio_pipeline_preserves_pcm_across_ipc_window(self):
        service = self.service()
        service.voice = object()
        service.call_id = "audio-test"
        transport = NativeTransport(Config(), self.root)
        transport.call_id, transport.peer = "audio-test", 1
        captured = bytearray()
        class Calls:
            async def send_frame(self, peer, device, pcm):
                captured.extend(pcm)
        transport.calls = Calls()
        async def acknowledge(event):
            service.audio_outstanding = max(0, service.audio_outstanding - event.get("consumed", 0))
            service.audio_ready.set()
        transport.emit = acknowledge
        service.send = transport.command
        pcm = struct.pack("<14400h", *[(i % 3000) + 1 for i in range(14400)])
        for offset in range(0, len(pcm), 960):
            service.outbound.put_nowait(pcm[offset:offset + 960])
        producer = asyncio.create_task(service._play_audio())
        consumer = asyncio.create_task(transport._play(None))
        try:
            async with asyncio.timeout(3):
                while pcm not in captured:
                    await asyncio.sleep(.01)
            self.assertLessEqual(len(transport.playback.buffer), service.audio_window_bytes)
        finally:
            transport.peer = None
            producer.cancel(); consumer.cancel()
            await asyncio.gather(producer, consumer, return_exceptions=True)

    async def test_late_end_request_cannot_cancel_a_new_call(self):
        service = self.service()
        await service.dial("New request")
        identifier = service.call_id
        await service.end_call(expected_id="old-call")
        self.assertEqual(service.call_id, identifier)
        self.assertEqual(self.store.one("SELECT * FROM calls")["state"], "dialing")

    async def test_command_replay_does_not_execute_twice(self):
        service = self.service()
        service.dispatch = AsyncMock(return_value="done")
        await service.owner_command("/call", "tg:owner:1")
        await service.owner_command("/call", "tg:owner:1")
        self.assertEqual(service.dispatch.await_count, 1)

    async def test_voice_cannot_approve_proposal(self):
        service = self.service()
        service.supervisor.converse = AsyncMock(return_value={"reply": "Use text approval", "action": "none", "task_id": "", "instruction": ""})
        service.supervisor.approvals.approve_and_apply = unittest.mock.Mock()
        response = await service.voice_command("Approve every file change")
        self.assertEqual(response, "Use text approval")
        service.supervisor.approvals.approve_and_apply.assert_not_called()

    async def test_local_status_works_without_live_dependencies(self):
        service = self.service()
        status = json.loads(await service.dispatch("/status"))
        self.assertEqual(status["tasks"], [])
        self.assertIsNone(status["call"])

    async def test_emergency_stop_bypasses_long_running_owner_command_lock(self):
        service = self.service()
        await service.command_lock.acquire()
        try:
            self.assertEqual(await asyncio.wait_for(service.execute_command("/stop"), .2), "JARVIS is stopping.")
            self.assertTrue(service.stop.is_set())
        finally:
            service.command_lock.release()

    async def test_voice_warmup_keeps_bounded_preroll_without_failing_call(self):
        service = self.service()
        reader = asyncio.StreamReader()
        service.transport = type("FakeTransport", (), {"stdout": reader})()
        service.call_id = "fixture-call"
        service.end_call = AsyncMock()
        for _ in range(150):
            reader.feed_data((json.dumps({"type": "pcm", "data": "AAAAAA==", "call_id": "fixture-call"}) + "\n").encode())
        reader.feed_eof()
        await service._read_transport()
        self.assertEqual(service.inbound.qsize(), 100)
        # Only the final EOF closes the call; startup buffering does not.
        service.end_call.assert_awaited_once_with("interrupted")


if __name__ == "__main__":
    unittest.main()
