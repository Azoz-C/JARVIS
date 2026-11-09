import tempfile
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from jarvis.security import GateError
from jarvis.store import Store
from jarvis.verification import VerificationBroker


class VerificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "db")
        self.task = self.store.create_task(self.root, "Verify")
        self.codex = type("Fake", (), {})()
        self.codex.request = AsyncMock(return_value={"exitCode": 0, "stdout": "passed", "stderr": ""})
        self.broker = VerificationBroker(self.store, self.codex)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_proposal_does_not_execute_and_bad_digest_cannot_execute(self):
        proposal = self.broker.propose(self.task, ["python", "-m", "unittest"])
        self.codex.request.assert_not_called()
        with self.assertRaises(GateError):
            await self.broker.approve_and_run(proposal["id"], "wrong")
        self.codex.request.assert_not_called()

    async def test_exact_approved_command_is_scoped_and_cannot_replay(self):
        proposal = self.broker.propose(self.task, ["python", "-m", "unittest"])
        result = await self.broker.approve_and_run(proposal["id"], proposal["digest"])
        params = self.codex.request.call_args.args[1]
        self.assertEqual(params["sandboxPolicy"]["type"], "workspaceWrite")
        if os.name == "nt":
            self.assertNotIn("outputBytesCap", params)
        self.assertFalse(params["sandboxPolicy"]["networkAccess"])
        self.assertEqual(params["sandboxPolicy"]["writableRoots"], [str(self.root.resolve())])
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(self.store.task(self.task)["state"], "completed")
        with self.assertRaises(GateError):
            await self.broker.approve_and_run(proposal["id"], proposal["digest"])

    async def test_timeout_is_uncertain_not_retried(self):
        self.codex.request.side_effect = TimeoutError()
        proposal = self.broker.propose(self.task, ["python", "-m", "unittest"])
        with self.assertRaises(TimeoutError):
            await self.broker.approve_and_run(proposal["id"], proposal["digest"])
        self.assertEqual(self.store.one("SELECT * FROM command_proposals")["state"], "uncertain")
        with self.assertRaises(GateError):
            await self.broker.approve_and_run(proposal["id"], proposal["digest"])
        self.assertEqual(self.codex.request.await_count, 2)
        self.assertEqual(self.codex.request.call_args.args[0], "command/exec/terminate")

    async def test_cancelled_task_cannot_run_command(self):
        proposal = self.broker.propose(self.task, ["python", "-m", "unittest"])
        self.store.transition(self.task, "cancelled")
        with self.assertRaises(GateError):
            await self.broker.approve_and_run(proposal["id"], proposal["digest"])
        self.codex.request.assert_not_called()
