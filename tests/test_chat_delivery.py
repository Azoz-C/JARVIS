"""Existing-chat routing and acceptance receipts without accounts or API use."""
import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from jarvis.config import Config
from jarvis.projects import ProjectRegistry
from jarvis.security import GateError
from jarvis.service import Service
from jarvis.store import Store
from jarvis.supervisor import Supervisor

THREAD = "11111111-2222-3333-4444-555555555555"
OTHER = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class ExistingChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.store = Store(self.root / "state.db")
        self.config = replace(Config(), codex_task_mode="visible", existing_chat_transport="app_server")
        self.service = Service(self.config, self.root / "config.toml", self.root, self.store)
        self.client = type("FakeCodex", (), {})()
        self.client.notifications = asyncio.Queue()
        for name in ("start", "close", "require_subscription", "name_thread", "interrupt", "steer"):
            setattr(self.client, name, AsyncMock())
        self.client.start_worker = AsyncMock(side_effect=AssertionError("An existing-chat send must not create a new chat"))
        self.client.read_thread = AsyncMock(return_value={"thread": {"id": THREAD, "name": "Original supervisor", "cwd": str(self.root), "status": {"type": "idle"}}})
        self.client.resume = AsyncMock(return_value=THREAD)
        self.client.list_threads = AsyncMock(return_value={"data": [{"id": THREAD, "name": "Original supervisor", "cwd": str(self.root)}], "nextCursor": None})
        async def turn(thread, project, prompt, **kwargs):
            report = {"summary": "Read-only follow-up completed; no tests run", "question": "", "changes": []}
            self.client.notifications.put_nowait({"method": "item/completed", "params": {"threadId": thread, "turnId": "accepted-turn", "item": {"type": "agentMessage", "text": json.dumps(report)}}})
            self.client.notifications.put_nowait({"method": "turn/completed", "params": {"threadId": thread, "turn": {"id": "accepted-turn", "status": "completed"}}})
            return {"turn": {"id": "accepted-turn"}}
        self.client.turn = AsyncMock(side_effect=turn)
        self.supervisor = Supervisor(self.config, self.store, self.client, registry=ProjectRegistry(self.root / "empty-codex"))
        self.service.supervisor = self.supervisor
        self.service.text = AsyncMock(return_value=True)
        self.supervisor.notify = self.service.text
        self.opened = patch("jarvis.frontend.open_link")
        self.open = self.opened.start()
        await self.supervisor.start()

    async def asyncTearDown(self):
        await self.supervisor.close(); await self.service.personal.close()
        self.opened.stop(); self.store.close(); self.temp.cleanup()

    async def finish(self):
        await asyncio.gather(*list(self.supervisor.runners.values()))

    async def test_existing_chat_send_needs_no_watch_and_preserves_title_and_id(self):
        receipt = await self.supervisor.send_existing(THREAD, "Inspect the existing work without editing")
        await self.finish()
        self.assertTrue(receipt["sent"])
        self.assertEqual(receipt["thread"], THREAD)
        self.assertEqual(receipt["turn"], "accepted-turn")
        self.client.start_worker.assert_not_awaited()
        self.client.name_thread.assert_not_awaited()
        self.client.resume.assert_awaited_once_with(THREAD, str(self.root))
        self.assertEqual(self.store.rows("SELECT * FROM watched"), [])
        self.assertEqual(self.supervisor.task_evidence(receipt["task"])["prompt_acceptance"][0]["thread"], THREAD)

    async def test_select_title_then_send_uses_same_chat(self):
        selected = json.loads(await self.service.dispatch("/chat Original supervisor"))
        self.assertFalse(selected["sent"])
        receipt = json.loads(await self.service.dispatch("/send Please review the current plan"))
        await self.finish()
        self.assertEqual(receipt["thread"], THREAD)
        self.client.turn.assert_awaited_once_with(THREAD, str(self.root), "Please review the current plan")

    async def test_send_by_deep_link_and_legacy_handoff_work_without_watch(self):
        result = json.loads(await self.service.dispatch("/send codex://threads/" + THREAD + " | Review safely"))
        await self.finish()
        self.assertTrue(result["sent"])
        result = json.loads(await self.service.dispatch("/handoff " + THREAD + " Continue read-only"))
        await self.finish()
        self.assertTrue(result["sent"])
        self.assertEqual(self.client.turn.await_count, 2)

    async def test_voice_routes_exact_existing_chat(self):
        self.supervisor.converse = AsyncMock(return_value={"action": "send_chat", "thread_id": THREAD, "project_id": "", "task_id": "", "instruction": "Keep this exact prompt"})
        result = json.loads(await self.service.voice_command("Send my prompt to Original supervisor"))
        await self.finish()
        self.assertTrue(result["sent"])
        self.client.turn.assert_awaited_once_with(THREAD, str(self.root), "Keep this exact prompt")

    async def test_selected_chat_followup_is_not_a_new_worker(self):
        await self.supervisor.select_chat(THREAD)
        self.supervisor.converse = AsyncMock(return_value={"action": "submit", "thread_id": "", "task_id": "", "instruction": "Continue the review"})
        result = json.loads(await self.service.voice_command("Continue the review"))
        await self.finish()
        self.assertEqual(result["thread"], THREAD)
        self.client.start_worker.assert_not_awaited()

    async def test_ambiguous_titles_do_not_pick_a_chat(self):
        self.client.list_threads.return_value = {"data": [{"id": THREAD, "name": "Supervisor"}, {"id": OTHER, "name": "Supervisor"}], "nextCursor": None}
        with self.assertRaisesRegex(GateError, "ambiguous"):
            await self.supervisor.send_existing("Supervisor", "Do work")
        self.client.turn.assert_not_awaited()
        self.assertEqual(self.store.rows("SELECT * FROM tasks"), [])

    async def test_missing_selection_does_not_create_a_chat(self):
        with self.assertRaisesRegex(GateError, "Select an existing chat"):
            await self.supervisor.send_existing("", "Continue")
        self.client.start_worker.assert_not_awaited()

    async def test_busy_foreign_chat_does_not_send(self):
        self.client.read_thread.return_value["thread"]["status"] = {"type": "active"}
        with self.assertRaisesRegex(GateError, "busy"):
            await self.supervisor.send_existing(THREAD, "Continue")
        self.client.turn.assert_not_awaited(); self.client.resume.assert_not_awaited()

    async def test_resume_failure_is_not_reported_as_sent(self):
        self.client.resume.side_effect = GateError("Server refused resume")
        with self.assertRaisesRegex(GateError, "Server refused resume"):
            await self.supervisor.send_existing(THREAD, "Continue")
        await self.finish()
        self.assertEqual(self.store.rows("SELECT * FROM events WHERE kind='codex_prompt_accepted'"), [])
        self.client.turn.assert_not_awaited()

    async def test_send_receipt_waits_until_codex_accepts_the_turn(self):
        original = self.client.turn.side_effect
        started, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args, **kwargs):
            started.set(); await release.wait()
            return await original(*args, **kwargs)
        self.client.turn.side_effect = delayed
        sender = asyncio.create_task(self.supervisor.send_existing(THREAD, "Continue"))
        await asyncio.wait_for(started.wait(), 1)
        self.assertFalse(sender.done())
        self.assertEqual(self.store.rows("SELECT * FROM events WHERE kind='codex_prompt_accepted'"), [])
        release.set()
        self.assertTrue((await sender)["sent"])
        await self.finish()

    async def test_switching_projects_clears_the_old_selected_chat(self):
        await self.supervisor.select_chat(THREAD)
        other = self.root / "other-project"; other.mkdir()
        self.supervisor.project_catalog = AsyncMock(return_value={"projects": [{"id": "other", "name": "Other project", "path": str(other)}]})
        await self.supervisor.select_voice_project("other")
        self.assertEqual(self.store.setting("selected_thread"), "")
        self.assertEqual(self.store.setting("selected_project"), str(other))

    async def test_active_jarvis_chat_receives_steer_instead_of_duplicate_turn(self):
        task = self.store.create_task(self.root, "Original", THREAD)
        self.store.transition(task, "running")
        self.supervisor.active[task] = (THREAD, "running-turn")
        receipt = await self.supervisor.send_existing(THREAD, "Clarification")
        self.assertEqual(receipt["kind"], "steer")
        self.client.steer.assert_awaited_once_with(THREAD, "running-turn", "Clarification")
        self.client.turn.assert_not_awaited()
        self.assertEqual(len(self.store.rows("SELECT * FROM tasks")), 1)

    async def test_draft_mode_does_not_open_a_new_chat_for_an_existing_target(self):
        self.supervisor.config = replace(self.config, codex_task_mode="frontend")
        with self.assertRaisesRegex(GateError, "Draft-only"):
            await self.supervisor.send_existing(THREAD, "Continue")
        self.open.assert_not_called()
        self.assertEqual(self.store.rows("SELECT * FROM tasks"), [])

    async def test_unknown_acceptance_blocks_replay_until_owner_resolves(self):
        self.client.turn.side_effect = TimeoutError()
        with self.assertRaisesRegex(GateError, "uncertain"):
            await self.supervisor.send_existing(THREAD, "Continue")
        await self.finish()
        task = self.store.one("SELECT * FROM tasks")["id"]
        with self.assertRaisesRegex(GateError, "uncertain"):
            await self.supervisor.send_existing(THREAD, "Continue again")
        self.assertEqual(self.client.turn.await_count, 1)
        result = self.supervisor.resolve_submission(task, "not-sent")
        self.assertFalse(result["resent"])
        self.assertEqual(self.client.turn.await_count, 1)

    async def test_restart_reconciles_interrupted_submission_without_replay(self):
        task = self.store.create_task(self.root, "Continue", THREAD)
        self.store.set_setting("submission_state:" + task, "attempting")
        await self.supervisor.close()
        restarted = Supervisor(self.config, self.store, self.client)
        await restarted.start()
        try:
            self.assertEqual(self.store.setting("uncertain_chat:" + THREAD), task)
            with self.assertRaisesRegex(GateError, "uncertain"):
                await restarted.send_existing(THREAD, "Continue")
            self.client.turn.assert_not_awaited()
        finally: await restarted.close()


if __name__ == "__main__": unittest.main()
