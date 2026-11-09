"""Async boundary for native frontend delivery; never resumes a Codex writer."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import hashlib
from pathlib import Path

from .security import GateError


def prompt_digest(prompt):
    return hashlib.sha256(prompt.replace("\r\n", "\n").rstrip("\n").encode("utf-8")).hexdigest()


class FrontendDeliveryError(GateError):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


class FrontendSender:
    def __init__(self, root: Path):
        self.root = root
        self.lock = asyncio.Lock()
        self.active = None
        self.cancel_path = None

    async def send(self, thread, title, prompt, identifier):
        if self.lock.locked():
            raise FrontendDeliveryError("Another frontend delivery is in progress; no prompt was submitted")
        async with self.lock:
            payload = prompt
            executable = Path(sys.executable).with_name("python.exe") if os.name == "nt" else Path(sys.executable)
            folder = self.root / "frontend-ui"; folder.mkdir(parents=True, exist_ok=True)
            cancel = folder / (identifier + ".cancel")
            cancel.unlink(missing_ok=True)
            request = {"thread": thread, "title": title, "payload": payload, "request_id": identifier,
                       "state": str(self.root), "cancel_file": str(cancel)}
            env = {key: value for key, value in os.environ.items() if "API_KEY" not in key.upper() and key.upper() != "OPENAI_ACCESS_TOKEN"}
            env["PYTHONIOENCODING"] = "utf-8"
            process = await asyncio.create_subprocess_exec(str(executable), "-B", "-m", "jarvis.ui_sender",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                creationflags=0x08000000 if os.name == "nt" else 0, env=env)
            self.active, self.cancel_path = process, cancel
            communication = asyncio.create_task(process.communicate(json.dumps(request).encode()))
            try:
                try:
                    output, _ = await asyncio.wait_for(asyncio.shield(communication), 45)
                except (asyncio.TimeoutError, asyncio.CancelledError) as error:
                    cancel.touch()
                    try: await asyncio.wait_for(asyncio.shield(communication), 5)
                    except Exception:
                        if process.returncode is None: process.kill()
                        await process.wait()
                    if isinstance(error, asyncio.CancelledError): raise
                    raise FrontendDeliveryError("Frontend delivery timed out; inspect the chat before resending", True) from error
                lines = [line for line in output.decode("utf-8", errors="replace").splitlines() if line.startswith("{")]
                if not lines:
                    raise FrontendDeliveryError("Frontend helper did not return a receipt; inspect the chat", True)
                result = json.loads(lines[-1])
                if not result.get("sent"):
                    raise FrontendDeliveryError(result.get("message", "Frontend submission failed"), bool(result.get("uncertain")))
                if (result.get("thread") != thread or result.get("request_id") != identifier
                        or result.get("prompt_sha256") != prompt_digest(prompt) or not result.get("ui_echo_observed")):
                    raise FrontendDeliveryError("Frontend receipt did not match the requested chat and message", True)
                return result
            finally:
                self.active = self.cancel_path = None
                cancel.unlink(missing_ok=True)

    async def close(self):
        if self.cancel_path: self.cancel_path.touch()
        if self.active:
            try: await asyncio.wait_for(self.active.wait(), 5)
            except asyncio.TimeoutError:
                self.active.kill(); await self.active.wait()
