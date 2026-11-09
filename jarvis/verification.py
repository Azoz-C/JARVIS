from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from .approvals import canonical, sha
from .security import GateError, redact
from .store import Store


class VerificationBroker:
    """Exact owner-approved argv; execution stays inside the Codex sandbox."""
    def __init__(self, store: Store, codex):
        self.store, self.codex = store, codex
        self.active = {}

    def propose(self, task: str, command: list[str]) -> dict:
        if (not isinstance(command, list) or not command or len(command) > 100
                or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in command)):
            raise GateError("Command must be a nonempty JSON array of arguments")
        project = self.store.task(task)["project"]
        payload = canonical({"command": command, "project": project, "timeout_ms": 120000,
                             "scope": "Project writes allowed; network and outside-project writes denied"})
        identifier = uuid.uuid4().hex[:16]
        digest = sha(payload.encode())
        with self.store.lock:
            self.store.db.execute("INSERT INTO command_proposals VALUES(?,?,?,?,?,?,?)",
                                  (identifier, task, payload, digest, "pending", time.time() + 3600, time.time()))
        return {"id": identifier, "digest": digest, "scope": json.loads(payload)}

    async def approve_and_run(self, identifier: str, digest: str) -> dict:
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM command_proposals WHERE id=?", (identifier,)).fetchone()
            if not row or row["state"] != "pending" or row["expires"] <= time.time():
                raise GateError("Command proposal is missing, expired, or already handled")
            row = dict(row)
            if digest != row["digest"] or sha(row["payload"].encode()) != digest:
                raise GateError("Command approval digest mismatch")
            if self.store.task(row["task"])["state"] in {"cancelled", "running", "verifying"}:
                raise GateError("Task must be idle and not cancelled")
            if db.execute("SELECT 1 FROM proposals WHERE task=? AND state IN ('pending','applying','uncertain')", (row["task"],)).fetchone():
                raise GateError("Resolve pending change proposals before running verification")
            payload = json.loads(row["payload"])
            if payload["project"] != self.store.task(row["task"])["project"]:
                raise GateError("Command project mismatch")
            db.execute("UPDATE command_proposals SET state='running' WHERE id=?", (identifier,))
        self.store.transition(row["task"], "verifying", "Running exact owner-approved verification command")
        process_id = "jarvis-verify-" + identifier
        self.active[row["task"]] = process_id
        try:
            params = {
                "command": payload["command"], "cwd": payload["project"],
                "processId": process_id,
                "timeoutMs": payload["timeout_ms"],
                "sandboxPolicy": {"type": "workspaceWrite", "writableRoots": [payload["project"]],
                                  "networkAccess": False, "excludeSlashTmp": True, "excludeTmpdirEnvVar": True},
            }
            # The native Windows sandbox rejects a custom cap; its own default
            # remains active, and persisted output is bounded below.
            if os.name != "nt":
                params["outputBytesCap"] = 16384
            result = await self.codex.request("command/exec", params, timeout=135)
        except BaseException:
            try:
                await self.codex.request("command/exec/terminate", {"processId": process_id}, timeout=5)
            except Exception:
                pass
            with self.store.lock:
                self.store.db.execute("UPDATE command_proposals SET state='uncertain' WHERE id=?", (identifier,))
            self.store.transition(row["task"], "needs_input", "Verification outcome uncertain; inspect before retrying")
            raise
        finally:
            self.active.pop(row["task"], None)
        safe = {"exit_code": result["exitCode"], "stdout": redact(result.get("stdout", ""))[:16384],
                "stderr": redact(result.get("stderr", ""))[:16384]}
        with self.store.lock:
            self.store.db.execute("UPDATE command_proposals SET state='completed' WHERE id=?", (identifier,))
        self.store.event(row["task"], "verification_result", safe)
        if self.store.task(row["task"])["state"] == "cancelled":
            return safe
        state = "completed" if safe["exit_code"] == 0 else "needs_input"
        self.store.transition(row["task"], state, f"Owner-approved verification command exited {safe['exit_code']}; this verifies only that command")
        return safe

    async def cancel(self, task: str):
        process_id = self.active.get(task)
        if process_id:
            await self.codex.request("command/exec/terminate", {"processId": process_id}, timeout=5)
