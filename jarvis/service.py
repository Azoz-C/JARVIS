from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

from .audio import Resampler
from .calls import CallPolicy
from .codex import CodexClient
from .config import Config
from .security import GateError, Vault, redact
from .store import Store
from .supervisor import Supervisor
from .voice import GeminiVoice, LocalVoice, prepare_local_voice
from .verification import VerificationBroker
from . import frontend
from .personal import PersonalAssistant


class Service:
    def __init__(self, config: Config, config_path: Path, root: Path, store: Store):
        self.config, self.config_path, self.root, self.store = config, config_path.resolve(), root, store
        self.supervisor = Supervisor(config, store, CodexClient(config.codex_bin))
        self.supervisor.notify = self.text
        self.verification = VerificationBroker(store, self.supervisor.codex)
        self.policy = CallPolicy(config, store)
        self.transport = None
        self.transport_reader = self.transport_stderr = None
        self.call_id = None
        self.call_started = 0.0
        self.call_context = ""
        self.voice = None
        self.voice_reader = None
        self.voice_feeder = None
        self.audio_player = None
        self.inbound = asyncio.Queue(maxsize=100)
        self.outbound = asyncio.Queue(maxsize=500)
        self.input_resampler = Resampler(48000, 16000)
        self.output_resamplers = {}
        self.write_lock = asyncio.Lock()
        self.stop = asyncio.Event()
        self.ready = asyncio.Event()
        self.command_lock = asyncio.Lock()
        self.background = set()
        self.last_input = 0.0
        self.call_lock = asyncio.Lock()
        self.voice_opening = False
        self.call_deadline = 0.0
        self.call_warning_sent = False
        self.voice_lifecycle = asyncio.Lock()
        self.media_connected_id = None
        self.audio_ready = asyncio.Event()
        self.audio_outstanding = 0
        self.audio_window_bytes = 960 * 12
        self.desktop_notify = None
        self.personal = PersonalAssistant(self)
        self.supervisor.personal_context = self.personal.context

    def spawn(self, coroutine, critical=False):
        task = asyncio.create_task(coroutine)
        self.background.add(task)
        def finished(done):
            self.background.discard(done)
            if not done.cancelled() and done.exception():
                self.store.event(None, "background_failure", {"error_type": type(done.exception()).__name__})
                if critical:
                    self.stop.set()
        task.add_done_callback(finished)
        return task

    async def send(self, message: dict):
        if not self.transport or self.transport.returncode is not None:
            raise GateError("Telegram transport is unavailable")
        async with self.write_lock:
            self.transport.stdin.write((json.dumps(message) + "\n").encode())
            await self.transport.stdin.drain()

    async def text(self, message: str):
        try:
            await self.send({"type": "text", "text": redact(message)})
            return True
        except (GateError, ConnectionError):
            self.store.event(None, "notification_unavailable", {"kind": "text"})
            return False

    async def start(self):
        self.store.recover()
        self.personal.organizer.recover()
        await self.supervisor.start()
        self.spawn(self._scheduler(), critical=True)
        self.spawn(self._inbox(), critical=True)
        if self.config.piper_model and Path(self.config.piper_model).is_file():
            self.spawn(asyncio.to_thread(prepare_local_voice, self.config.piper_model))
        if (not self.config.owner_id or not self.config.telegram_api_id
                or not (self.root / "telegram.dpapi").is_file()):
            self.store.set_setting("health", "setup_required")
            return
        self.transport = await asyncio.create_subprocess_exec(
            sys.executable, "-B", "-m", "jarvis.transport", "--config", str(self.config_path), "--state", str(self.root),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=0x08000000 if os.name == "nt" else 0, limit=1024 * 1024,
        )
        self.transport_reader = self.spawn(self._read_transport())
        self.transport_stderr = self.spawn(self._drain_stderr())
        try:
            await asyncio.wait_for(self.ready.wait(), 30)
        except asyncio.TimeoutError:
            self.store.set_setting("health", "telegram_unavailable")
            return
        self.store.set_setting("health", "running")
        await self.text("JARVIS is online. Edits require a reviewed proposal and text/local approval. /help lists commands.")

    async def _drain_stderr(self):
        while await self.transport.stderr.read(4096):
            pass

    async def _read_transport(self):
        try:
            while line := await self.transport.stdout.readline():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                kind = event.get("type")
                if kind == "ready":
                    self.ready.set()
                elif kind == "command" and event.get("sender") == self.config.owner_id:
                    self.spawn(self.owner_command(event["text"], event["id"]))
                elif kind == "incoming":
                    if event.get("peer") == self.config.owner_id:
                        self.spawn(self.dial("Incoming owner call", incoming=True, offer=event.get("offer")))
                elif kind == "connected":
                    if self.call_id and event.get("call_id") == self.call_id:
                        self.media_connected_id = self.call_id
                        self.policy.active(self.call_id)
                        self.call_started = time.monotonic()
                        self.call_deadline = self.call_started + self.config.max_call_seconds
                        self.spawn(self.open_voice(expected_id=event["call_id"]))
                elif kind == "pcm" and self.call_id and event.get("call_id") == self.call_id:
                    self.last_input = time.monotonic()
                    try:
                        pcm = base64.b64decode(event["data"], validate=True)
                        if self.inbound.full():
                            # Preserve live latency during provider backpressure.
                            # A full audio queue must not terminate the call.
                            self.inbound.get_nowait()
                        self.inbound.put_nowait(pcm)
                    except (ValueError, asyncio.QueueFull):
                        self.spawn(self.end_call("failed", expected_id=self.call_id))
                elif kind == "audio_ready" and self.call_id and event.get("call_id") == self.call_id:
                    self.audio_outstanding = max(0, self.audio_outstanding - max(0, int(event.get("consumed", 0))))
                    self.audio_ready.set()
                elif kind == "ended":
                    if self.call_id and event.get("call_id") == self.call_id:
                        self.spawn(self.end_call(expected_id=event["call_id"]))
                elif kind == "fault":
                    self.store.event(None, "transport_fault", {"component": event.get("component"), "error_type": event.get("error_type")})
                    if self.call_id and event.get("call_id") == self.call_id and event.get("component") in {"connect", "audio", "audio_output", "hangup"}:
                        self.spawn(self.end_call("failed", expected_id=self.call_id))
                    elif not self.ready.is_set():
                        self.store.set_setting("health", "telegram_unavailable")
        finally:
            self.store.set_setting("health", "telegram_unavailable")
            await self.end_call("interrupted")
            # Local control and Codex supervision remain available after a Telegram fault.

    async def dial(self, reason: str, incoming=False, blockers=None, offer=None):
        async with self.call_lock:
            automatic = blockers is not None
            identifier = None
            try:
                if self.call_id:
                    raise GateError("A call is already active")
                identifier = self.policy.reserve(self.config.owner_id, reason, automatic=automatic, blockers=blockers)
                self.call_id, self.call_context = identifier, reason
                self.media_connected_id = None
                self.call_started = time.monotonic()
                self.call_deadline = self.call_started + self.config.max_call_seconds
                self.call_warning_sent = False
                self.last_input = self.call_started
                self.input_resampler = Resampler(48000, 16000)
                self.output_resamplers.clear()
                self._drain(self.inbound); self._drain(self.outbound)
                self.audio_outstanding = 0
                await self.send({"type": "connect", "peer": self.config.owner_id, "call_id": identifier,
                                 "incoming": incoming, "offer": offer})
            except Exception as error:
                if identifier:
                    self.policy.finish(identifier, "failed")
                    self.call_id = None
                if not automatic:
                    await self.text(str(error) if isinstance(error, GateError) else "Unable to request the call")

    async def open_voice(self, force_local: bool = False, expected_id=None):
        identifier = expected_id or self.call_id
        async with self.voice_lifecycle:
            if not identifier or identifier != self.call_id or self.media_connected_id != identifier or self.voice:
                return
            self.voice_opening = True
            try:
                await self._open_voice(force_local, identifier)
            finally:
                self.voice_opening = False

    async def _open_voice(self, force_local: bool, identifier: str):
        if self.call_id != identifier:
            return
        try:
            if force_local or not self.config.free_cloud_confirmed:
                voice = LocalVoice(self.config, self.voice_command, self.root)
            else:
                catalog = await self.supervisor.project_catalog()
                context = {"connection_checked": True, "codex_connected": catalog["connected"],
                           "personal_features": "browser search/research, local calendar, reminders, reservations, preferences, notes, documents, daily briefing, routines",
                           "google_calendar_connected": self.personal.calendar.connected(),
                           "available_projects": [p["name"] for p in catalog["projects"]],
                           "selected_project": self.store.setting("selected_project"),
                           "available_chat_count": len(catalog["chats"])}
                voice = GeminiVoice(self.config, Vault(self.root / "secrets").get("gemini_api_key"), self.voice_command, context)
            await voice.open()
        except Exception:
            if not force_local:
                await self.text("Cloud voice unavailable; trying local speech. No paid fallback.")
                return await self._open_voice(True, identifier)
            await self.text("Local voice is not ready. Telegram text control remains available.")
            self.spawn(self.end_call("failed", expected_id=identifier))
            return
        if self.call_id != identifier:
            await voice.close()
            return
        self.voice = voice
        self.store.set_setting("voice_mode", "local" if isinstance(voice, LocalVoice) else "free_cloud")
        self.voice_reader = self.spawn(self._voice_events())
        self.voice_feeder = self.spawn(self._feed_voice())
        self.audio_player = self.spawn(self._play_audio())
        self.spawn(self._greeting(voice))

    async def _greeting(self, voice):
        try:
            await voice.greet(self.call_context)
        except Exception:
            if self.voice is voice:
                await self.fallback(expected_id=self.call_id)

    async def _feed_voice(self):
        identifier = self.call_id
        try:
            buffer = bytearray()
            while self.voice:
                buffer.extend(self.input_resampler.push(await self.inbound.get()))
                if len(buffer) >= 3200:
                    await self.voice.send_audio(bytes(buffer))
                    buffer.clear()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.spawn(self.fallback(expected_id=identifier))

    async def _voice_events(self):
        identifier = self.call_id
        try:
            async for event in self.voice.events():
                if event["type"] == "interrupt":
                    self._drain(self.outbound)
                    self.output_resamplers.clear()
                    await self.send({"type": "interrupt", "call_id": self.call_id})
                elif event["type"] == "audio":
                    rate = event["rate"]
                    resampler = self.output_resamplers.setdefault(rate, Resampler(rate, 48000))
                    pcm = resampler.push(event["pcm"])
                    for offset in range(0, len(pcm), 960):
                        if isinstance(self.voice, LocalVoice) and event.get("generation") != self.voice.generation:
                            self._drain(self.outbound)
                            await self.send({"type": "interrupt", "call_id": self.call_id})
                            break
                        await self.outbound.put(pcm[offset:offset + 960])
                elif event["type"] == "failure":
                    raise GateError("Voice provider failed")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.spawn(self.fallback(expected_id=identifier))

    async def _play_audio(self):
        identifier = self.call_id
        try:
            while self.voice:
                pcm = await self.outbound.get()
                while self.audio_outstanding + len(pcm) > self.audio_window_bytes:
                    self.audio_ready.clear()
                    await asyncio.wait_for(self.audio_ready.wait(), 5)
                self.audio_outstanding += len(pcm)
                await self.send({"type": "audio", "data": base64.b64encode(pcm).decode(), "call_id": self.call_id})
                # A bounded 120 ms window absorbs IPC jitter while native audio
                # consumption supplies backpressure. No second playback clock.
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.store.event(None, "audio_playback_failure", {"error_type": type(error).__name__})
            self.spawn(self.end_call("failed", expected_id=identifier))

    async def fallback(self, expected_id=None):
        identifier = expected_id or self.call_id
        if not self.voice or identifier != self.call_id:
            return
        was_local = isinstance(self.voice, LocalVoice)
        await self.close_voice()
        if identifier != self.call_id:
            return
        if was_local:
            await self.text("Local voice failed; use Telegram text. Coding tasks were not cancelled.")
            await self.end_call("failed")
        else:
            await self.text("Switching to local voice after cloud voice became unavailable.")
            await self.open_voice(force_local=True, expected_id=identifier)

    @staticmethod
    def _drain(queue):
        while not queue.empty():
            queue.get_nowait()

    async def close_voice(self):
        async with self.voice_lifecycle:
            await self._close_voice()

    async def _close_voice(self):
        voice, self.voice = self.voice, None
        current = asyncio.current_task()
        tasks = [t for t in (self.voice_reader, self.voice_feeder, self.audio_player) if t and t is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if voice:
            await voice.close()
        self._drain(self.inbound); self._drain(self.outbound)
        self.store.set_setting("voice_mode", "none")
        try:
            await self.send({"type": "interrupt", "call_id": self.call_id})
        except Exception:
            pass

    async def end_call(self, result="ended", expected_id=None):
        if expected_id is not None and expected_id != self.call_id:
            return
        identifier, self.call_id = self.call_id, None
        self.media_connected_id = None
        if identifier:
            try:
                await self.send({"type": "hangup", "call_id": identifier})
            except Exception:
                pass
        await self.close_voice()
        if identifier:
            self.policy.finish(identifier, result)
            if result != "ended":
                await self.text("Call ended without a reliable voice connection. No automatic redial; use /call when ready.")

    async def voice_command(self, message: str, source="voice") -> str:
        # The provider is untrusted. A restricted reasoning thread interprets the
        # request; only bounded application actions are allowed, never edit approval.
        clean = message.strip().lower()
        if clean in {"hang up", "hangup", "end call", "goodbye"}:
            self.spawn(self.end_call())
            return "Ending this call. Workers continue running."
        project = self.store.setting("selected_project")
        self.store.event(None, "voice_supervisor_request", {"message_chars": len(message)})
        try:
            result = await self.supervisor.converse(message, Path(__file__).resolve().parent)
        except Exception as error:
            self.store.event(None, "voice_supervisor_failure", {"error_type": type(error).__name__,
                "message": str(error)[:500] if isinstance(error, GateError) else "Reasoning connection failed"})
            raise
        action, identifier, instruction = result["action"], result["task_id"], result["instruction"]
        self.store.event(None, "voice_supervisor_response", {"action": action})
        if action == "assistant":
            try:
                result = await self.personal.execute(json.loads(instruction), source=source)
                return json.dumps(result, ensure_ascii=False)
            except GateError as error:
                return str(error)
            except (KeyError, ValueError, TypeError):
                return "That request is missing valid details. Please specify the date/time and required information; no completion is confirmed."
        if action == "select_project":
            name = await self.supervisor.select_voice_project(result.get("project_id", ""))
            return "Selected " + name + ". What would you like me to work on?"
        if action == "select_chat":
            try:
                selected = await self.supervisor.select_chat(result.get("thread_id", ""), result.get("project_id", ""))
                return "Selected existing chat: " + selected["title"] + ". No prompt has been sent yet."
            except GateError as error: return str(error)
        if action == "send_chat" or (action == "submit" and self.store.setting("selected_thread")):
            try:
                receipt = await self.supervisor.send_existing(result.get("thread_id", ""), instruction, result.get("project_id", ""))
                return json.dumps(receipt, ensure_ascii=False)
            except GateError as error: return str(error)
        if action in {"submit", "submit_new"}:
            if not project:
                return "Select a project with /project in Telegram first. No task was started."
            identifier = self.supervisor.submit(Path(project), instruction)
            await self.supervisor.run(identifier)
            return self.task_receipt(identifier)
        if action == "answer":
            await self.supervisor.answer(identifier, instruction)
            return "Your clarification was recorded. It does not approve edits."
        if action == "cancel":
            await self.supervisor.cancel(identifier)
            return "Task " + identifier + " was cancelled."
        if action == "hangup":
            self.spawn(self.end_call())
            return "Ending the call. Workers continue running."
        return result["reply"]

    def task_receipt(self, identifier: str) -> str:
        task = self.store.task(identifier)
        if self.store.setting("execution_transport:" + identifier) == "interface":
            return json.dumps(self.supervisor.task_evidence(identifier), ensure_ascii=False)
        if task["state"] == "awaiting_frontend":
            return ("Prompt prepared for Codex frontend, task " + identifier +
                    ". Press Send in Codex. No background task or verification test was started. "
                    "Saved prompt: " + self.store.setting("frontend_prompt:" + identifier))
        if task["state"] == "queued":
            return "Task accepted: " + identifier + ". Preparing its Codex chat; execution is not confirmed yet. Use /task-status " + identifier + " for evidence."
        if task["state"] in {"failed", "needs_input", "cancelled"}:
            return "Task " + identifier + ": " + task["summary"]
        return "Read-only Codex task started: " + identifier + ". This does not mean any verification test ran."

    async def owner_command(self, text: str, command_id: str):
        if not self.store.claim_command(command_id):
            return
        try:
            answer = await self.execute_command(text)
        except (GateError, ValueError, KeyError) as error:
            answer = str(error)
        except Exception:
            answer = "Operation failed. No automatic retry; inspect local status."
        await self.text(answer)

    async def execute_command(self, text: str):
        if text.strip().split(" ", 1)[0].lower() in {"/stop", "/hangup", "/cancel", "/status", "/action-cancel"}:
            return await self.dispatch(text)
        async with self.command_lock:
            return await self.dispatch(text)

    async def dispatch(self, text: str) -> str:
        command, _, args = text.strip().partition(" ")
        command = command.lower()
        if command in {"/help", "help"}:
            return ("/help-personal for browser, appointments, reminders, reservations, notes and routines\n"
                    "/status | /task-status TASK | /open TASK | /call | /hangup | /extend SECONDS | /quiet on|off | /stop\n"
                    "/project PATH | /task PROJECT_PATH | task description\n/run TASK | /cancel TASK | /answer TASK message\n"
                    "/projects [refresh] | /chats [CURSOR] | /chat TITLE_OR_ID | /send [CHAT |] PROMPT\n"
                    "/watch THREAD | /unwatch THREAD | /handoff THREAD instruction\n"
                    "/chat-resolve TASK sent|not-sent (only after inspecting an uncertain submission)\n"
                    "/approvals | /preview PROPOSAL | /approve PROPOSAL SHA256 | /reject PROPOSAL\n"
                    '/verify TASK | ["python", "-m", "unittest"]\n/approve-command ID SHA256')
        if command in {"/status", "status"}:
            return json.dumps({"service": self.store.setting("health"), "voice": self.store.setting("voice_mode", "none"),
                               "execution_mode": self.config.codex_task_mode,
                               "personal": {"google_calendar_connected": self.personal.calendar.connected(), "browser_running": self.personal.browser.context is not None,
                                            "running_actions": len(self.personal.jobs), "pending_reminders": len(self.store.rows("SELECT id FROM reminders WHERE state='pending'"))},
                               "call": self.call_id, "quiet_override": self.store.setting("quiet", "false"),
                               "selected_chat": {"id": self.store.setting("selected_thread"), "title": self.store.setting("selected_chat_title")},
                               "tasks": self.store.rows("SELECT id,state,summary FROM tasks ORDER BY updated DESC LIMIT 10"),
                               "watched": self.store.rows("SELECT * FROM watched")}, indent=2)
        personal_command = await self.personal_command(command, args)
        if personal_command is not None:
            return personal_command
        if command == "/call":
            await self.dial(args or "Owner-requested conversation")
            return "Call request processed."
        if command == "/task-status":
            return json.dumps(self.supervisor.task_evidence(args.strip()), indent=2)
        if command == "/open":
            task = self.store.task(args.strip())
            if not task["thread"]:
                raise GateError("No running Codex chat is linked to this task; use its saved frontend prompt")
            link = frontend.chat_link(task["thread"])
            await asyncio.to_thread(frontend.open_link, link)
            return "Requested Codex chat opening: " + link
        if command == "/hangup":
            await self.end_call()
            return "Call ended. Coding tasks continue."
        if command == "/extend":
            seconds = int(args or "300")
            if not self.call_id or not 30 <= seconds <= 900:
                raise GateError("An active call and an extension of 30–900 seconds are required")
            self.call_deadline = min(self.call_deadline + seconds, self.call_started + 3600)
            self.call_warning_sent = False
            return "Call extended, subject to the one-hour hard limit."
        if command == "/quiet":
            if args not in {"on", "off"}:
                raise GateError("Use /quiet on or /quiet off")
            self.store.set_setting("quiet", str(args == "on").lower())
            return "Quiet override " + args + ". Scheduled quiet hours still apply."
        if command == "/task":
            project, separator, prompt = args.partition("|")
            if not separator:
                raise GateError("Use /task PROJECT_PATH | task description")
            identifier = self.supervisor.submit(Path(project.strip().strip('"')), prompt.strip())
            self.store.set_setting("selected_project", self.store.task(identifier)["project"])
            await self.supervisor.run(identifier)
            return self.task_receipt(identifier)
        if command == "/project":
            catalog = await self.supervisor.project_catalog()
            matches = [p for p in catalog["projects"] if p["name"].casefold() == args.strip().casefold()]
            if len(matches) == 1:
                return "Voice tasks will use project: " + await self.supervisor.select_voice_project(matches[0]["id"])
            if len(matches) > 1:
                raise GateError("Several project roots share that name; select an exact path")
            project = Path(args.strip().strip('"')).resolve(strict=True)
            if not project.is_dir():
                raise GateError("Select a local project directory")
            if self.store.setting("selected_chat_project").casefold() != str(project).casefold():
                self.store.set_setting("selected_thread", "")
                self.store.set_setting("selected_chat_title", "")
            self.store.set_setting("selected_project", str(project))
            return "Voice tasks will use project: " + str(project)
        if command == "/run":
            await self.supervisor.run(args.strip())
            return self.task_receipt(args.strip())
        if command == "/cancel":
            await self.verification.cancel(args.strip())
            await self.supervisor.cancel(args.strip())
            return "Task cancelled."
        if command == "/answer":
            identifier, _, message = args.partition(" ")
            if not message:
                raise GateError("Use /answer TASK message")
            await self.supervisor.answer(identifier, message)
            return "Clarification recorded. Use /run TASK to resume an idle task."
        if command == "/chats":
            result = await self.supervisor.codex.list_threads(args.strip() or None)
            return json.dumps({"chats": [{"id": r["id"], "title": r.get("name"), "status": r.get("status")} for r in result.get("data", [])], "nextCursor": result.get("nextCursor")}, indent=2)
        if command == "/chat":
            return json.dumps(await self.supervisor.select_chat(args.strip()), ensure_ascii=False, indent=2)
        if command == "/send":
            reference, separator, prompt = args.partition("|")
            if not separator: reference, prompt = "", args
            return json.dumps(await self.supervisor.send_existing(reference.strip(), prompt.strip()), ensure_ascii=False, indent=2)
        if command == "/chat-resolve":
            identifier, _, observation = args.strip().partition(" ")
            return json.dumps(self.supervisor.resolve_submission(identifier, observation.strip()), indent=2)
        if command == "/projects":
            catalog = await self.supervisor.project_catalog(refresh=args.strip().casefold() == "refresh")
            return json.dumps({k: v for k, v in catalog.items() if k != "chats"}, ensure_ascii=False, indent=2)
        if command == "/watch":
            return "Monitoring selected chat: " + await self.supervisor.watch(args.strip())
        if command == "/unwatch":
            thread = args.strip()
            with self.store.lock:
                self.store.db.execute("DELETE FROM watched WHERE thread=?", (thread,))
                blocker = self.store.setting("watch_blocker:" + thread)
                if blocker:
                    self.store.db.execute("UPDATE blockers SET resolved=1 WHERE id=?", (blocker,))
                    self.store.set_setting("watch_blocker:" + thread, "")
            return "Stopped monitoring the selected chat."
        if command == "/handoff":
            thread, _, prompt = args.partition(" ")
            if not prompt:
                raise GateError("Use /handoff THREAD instruction")
            return json.dumps(await self.supervisor.send_existing(thread.strip(), prompt.strip()), ensure_ascii=False, indent=2)
        if command == "/approvals":
            return json.dumps(self.store.rows("SELECT id,task,digest,expires,state FROM proposals WHERE state IN ('pending','uncertain')"), indent=2)
        if command == "/preview":
            preview = self.supervisor.approvals.preview(args.strip())
            if len(preview) > 28000:
                return "Proposal is too large to review completely in Telegram; use the local preview command."
            return preview
        if command == "/approve":
            identifier, _, digest = args.partition(" ")
            self.supervisor.approvals.approve_and_apply(identifier, digest.strip())
            return "Exact approved changes applied and read back. Tests have not been run."
        if command == "/reject":
            self.supervisor.approvals.reject(args.strip())
            return "Proposal rejected; project unchanged."
        if command == "/verify":
            identifier, separator, raw_command = args.partition("|")
            if not separator:
                raise GateError('Use /verify TASK | ["python", "-m", "unittest"]')
            if self.config.codex_task_mode == "frontend":
                command_args = json.loads(raw_command)
                if (not isinstance(command_args, list) or not command_args or len(command_args) > 100
                        or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in command_args)):
                    raise GateError("Command must be a nonempty JSON array of arguments")
                task = self.store.task(identifier.strip())
                draft = self.supervisor.submit(Path(task["project"]),
                    "Run this exact verification command, represented as a JSON argv array, in the project directory. "
                    "Report actual output and exit code; if it cannot run, say so. Do not substitute a static review.\n"
                    + json.dumps(command_args))
                await self.supervisor.run(draft)
                return self.task_receipt(draft)
            return json.dumps(self.verification.propose(identifier.strip(), json.loads(raw_command)), indent=2)
        if command == "/approve-command":
            if self.config.codex_task_mode == "frontend":
                raise GateError("Frontend mode is active. Prepare verification with /verify and send it from Codex; no background command was executed")
            identifier, _, digest = args.partition(" ")
            return json.dumps(await self.verification.approve_and_run(identifier, digest.strip()), indent=2)
        if command == "/stop":
            self.stop.set()
            return "JARVIS is stopping."
        if text.strip() and not text.startswith("/"):
            return await self.voice_command(text, source="text")
        return "Use /help for owner controls. Voice conversations can discuss task status."

    async def personal_command(self, command, args):
        if command == "/help-personal":
            return ("Personal assistant — dates use Europe/Istanbul unless an offset is supplied.\n"
                "/search QUERY | /research QUERY | /browse URL | /browser-read | /browser-close\n"
                "/browser-attach http://127.0.0.1:9222 | /browser-action JSON\n"
                "/remember KEY | VALUE | /memories | /forget KEY\n"
                "/note TITLE | BODY | /notes [QUERY] | /delete-note ID\n"
                "/remind ISO_TIME | TITLE | /reminders | /snooze ID | ISO_TIME | /dismiss ID\n"
                "/appointment START | END | TITLE | LOCATION | /agenda | /cancel-appointment ID\n/reschedule ID | START | END\n"
                "/calendar-export | /calendar-connect CLIENT_JSON_PATH | /calendar-sync | /calendar-push ID\n"
                "/briefing | /briefing-at HH:MM | /briefing-at off\n"
                "/documents QUERY | /document-summary ABSOLUTE_PATH | /documents-root FOLDER\n"
                "/reserve VENUE | START | END | PARTY_SIZE | WEBSITE(optional) | REQUIREMENTS(optional)\n"
                "/reservations | /reservation-submit ID | /reservation-check ID | /reservation-continue ID\n"
                "/routines | /routine NAME | /routine-save NAME | JSON_STEPS | /routine-delete NAME\n"
                "/activity [ID] | /action-cancel ID | /assistant JSON for recurrence and task-linked follow-ups.\n"
                "Login/CAPTCHA is completed by you in the visible browser. Prepared reservations are not bookings; confirmation evidence is required.")
        fields = [x.strip() for x in args.split("|")]
        simple = {"/search": ("search", "query"), "/research": ("research", "query"), "/browse": ("browse", "url"),
            "/browser-attach": ("browser_attach", "endpoint"), "/forget": ("forget", "key"), "/notes": ("notes", "query"),
            "/delete-note": ("delete_note", "id"), "/dismiss": ("dismiss", "id"), "/retry-reminder": ("retry_reminder", "id"),
            "/cancel-appointment": ("cancel_appointment", "id"), "/calendar-connect": ("calendar_connect", "path"),
            "/calendar-push": ("calendar_push", "id"), "/briefing-at": ("briefing_schedule", "at"),
            "/documents": ("documents", "query"), "/document-summary": ("document_summary", "path"), "/documents-root": ("documents_root", "path"),
            "/routine": ("routine_run", "name"), "/routine-delete": ("routine_delete", "name"),
            "/reservation-submit": ("reservation_submit", "id"), "/reservation-check": ("reservation_check", "id"), "/activity": ("activity", "id")}
        simple["/action-cancel"] = ("cancel_action", "id")
        simple["/reservation-continue"] = ("reservation_continue", "id")
        no_args = {"/browser-read": "browser_read", "/browser-close": "browser_close", "/memories": "memories", "/reminders": "reminders",
            "/agenda": "agenda", "/calendar-export": "calendar_export", "/calendar-sync": "calendar_sync", "/briefing": "briefing", "/routines": "routines", "/reservations": "reservations"}
        if command in simple:
            op, key = simple[command]; payload = {"op": op, key: args.strip().strip('"')}
        elif command in no_args: payload = {"op": no_args[command]}
        elif command in {"/assistant", "/browser-action"}:
            payload = json.loads(args)
            if command == "/browser-action": payload["op"] = "browser_action"
        elif command in {"/remember", "/note", "/remind", "/snooze", "/routine-save"}:
            a, separator, b = args.partition("|")
            if not separator: raise GateError("Use " + command + " FIRST_VALUE | SECOND_VALUE")
            a, b = a.strip(), b.strip()
            if command == "/remember": payload = {"op": "remember", "key": a, "value": b}
            elif command == "/note": payload = {"op": "note", "title": a, "body": b}
            elif command == "/remind": payload = {"op": "remind", "due": a, "title": b}
            elif command == "/snooze": payload = {"op": "snooze", "id": a, "due": b}
            else: payload = {"op": "routine_save", "name": a, "steps": json.loads(b)}
        elif command == "/appointment":
            if len(fields) not in {3, 4}: raise GateError("Use /appointment START | END | TITLE | LOCATION(optional)")
            payload = {"op": "appointment", "start": fields[0], "end": fields[1], "title": fields[2], "location": fields[3] if len(fields) == 4 else ""}
        elif command == "/reschedule":
            if len(fields) != 3: raise GateError("Use /reschedule ID | START | END")
            payload = {"op": "reschedule", "id": fields[0], "start": fields[1], "end": fields[2]}
        elif command == "/reserve":
            if not 4 <= len(fields) <= 6: raise GateError("Use /reserve VENUE | START | END | PARTY_SIZE | WEBSITE(optional) | REQUIREMENTS(optional)")
            payload = {"op": "reserve", "venue": fields[0], "start": fields[1], "end": fields[2], "party_size": int(fields[3]),
                "url": fields[4] if len(fields) > 4 else "", "requirements": fields[5] if len(fields) > 5 else ""}
        else: return None
        return json.dumps(await self.personal.execute(payload, source="text"), ensure_ascii=False, indent=2)

    async def _scheduler(self):
        last_poll = last_purge = 0.0
        while not self.stop.is_set():
            now = time.monotonic()
            self.store.set_setting("heartbeat", str(time.time()))
            if self.call_id:
                if now >= self.call_deadline:
                    await self.text("Maximum call duration reached; request another call if needed.")
                    await self.end_call()
                elif self.call_deadline - now <= 60 and not self.call_warning_sent:
                    await self.text("This call ends in one minute. Use /extend 300 to add five minutes.")
                    self.call_warning_sent = True
                elif self.voice and now - self.last_input > 15:
                    await self.text("Incoming call audio stopped; ending the call safely.")
                    await self.end_call("failed")
                elif not self.voice and now - self.call_started > 50:
                    await self.end_call("unanswered")
            if now - last_poll >= 15:
                await self.supervisor.poll_watched()
                await self.supervisor.poll_frontend()
                last_poll = now
            if not self.personal.tick_lock.locked():
                self.spawn(self.personal.tick())
            if now - last_purge >= 3600:
                self.store.purge()
                self.personal.organizer.purge()
                last_purge = now
            due = self.policy.due()
            if due and not self.call_id:
                await self.dial("Your input is needed: " + "; ".join(b["question"] for b in due)[:1800],
                                blockers=[b["id"] for b in due])
            try:
                await asyncio.wait_for(self.stop.wait(), 2)
            except asyncio.TimeoutError:
                pass

    async def _inbox(self):
        while not self.stop.is_set():
            for row in self.store.rows("SELECT * FROM inbox WHERE state='pending' ORDER BY created LIMIT 10"):
                with self.store.transaction() as db:
                    claimed = db.execute("UPDATE inbox SET state='running' WHERE id=? AND state='pending'", (row["id"],)).rowcount
                if not claimed:
                    continue
                self.spawn(self._handle_inbox(row))
            await asyncio.sleep(.25)

    async def _handle_inbox(self, row):
        try:
            result = await self.execute_command(row["command"])
        except Exception as error:
            result = str(error) if isinstance(error, GateError) else "Command failed; inspect locally"
        with self.store.lock:
            self.store.db.execute("UPDATE inbox SET state='done',response=? WHERE id=?", (redact(result), row["id"]))

    async def close(self):
        self.stop.set()
        await self.end_call()
        await self.personal.close()
        for task in list(self.verification.active):
            try:
                await self.verification.cancel(task)
            except Exception:
                pass
        await self.supervisor.close()
        if self.transport:
            self.transport.stdin.close()
            try:
                await asyncio.wait_for(self.transport.wait(), 5)
            except asyncio.TimeoutError:
                self.transport.kill()
                await self.transport.wait()
        for task in list(self.background):
            task.cancel()
        await asyncio.gather(*self.background, return_exceptions=True)
        self.store.set_setting("health", "stopped")
