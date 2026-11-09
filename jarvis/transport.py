"""Isolated native Telegram process. Stdout is an internal JSON-lines channel."""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import sys
import time
import uuid
from pathlib import Path

from .audio import Playback
from .config import Config
from .security import GateError, Vault


class NativeTransport:
    def __init__(self, config: Config, root: Path):
        self.config, self.root = config, root
        self.peer = None
        self.player = None
        self.playback = Playback()
        self.output_lock = asyncio.Lock()
        self.accepting = False
        self.tele = self.calls = None
        self.started_at = time.time()
        self.connection = None
        self.call_id = None
        self.pending_offer = None
        self.incoming_mode = False

    def guard_outbound_requests(self, adapter):
        original = adapter.request_call
        async def request_call(user_id, *args, **kwargs):
            if (self.incoming_mode or self.peer != self.config.owner_id
                    or user_id != self.config.owner_id or not self.call_id):
                raise GateError("An incoming call cannot become an unsolicited outgoing call")
            return await original(user_id, *args, **kwargs)
        adapter.request_call = request_call

    async def emit(self, event: dict):
        if event.get("type") in {"connected", "ended", "pcm"}:
            event.setdefault("call_id", self.call_id)
        async with self.output_lock:
            await asyncio.to_thread(self._emit_sync, event)

    @staticmethod
    def _emit_sync(event):
        sys.stdout.write(json.dumps(event) + "\n")
        sys.stdout.flush()

    async def start(self):
        from telethon import TelegramClient, events
        from telethon.sessions import StringSession
        from pytgcalls import PyTgCalls
        from pytgcalls.types import ChatUpdate, StreamFrames, Direction
        from .security import _dpapi

        if not self.config.owner_id or not self.config.telegram_api_id:
            raise GateError("Configure owner_id and telegram_api_id")
        session = self.root / "telegram.dpapi"
        if not session.is_file():
            raise GateError("Authenticate the spare Telegram account first")
        api_hash = Vault(self.root / "secrets").get("telegram_api_hash")
        self.tele = TelegramClient(StringSession(_dpapi(session.read_bytes(), False).decode()),
                                   self.config.telegram_api_id, api_hash)
        await self.tele.connect()
        if not await self.tele.is_user_authorized():
            raise GateError("Telegram authentication expired")
        me = await self.tele.get_me()
        if me.id == self.config.owner_id:
            raise GateError("Use the spare account, not the owner's account")
        # StringSession retains authentication but not Telegram entity hashes.
        # Restore only the configured owner's encrypted peer information.
        owner_file = self.root / "telegram-owner.dpapi"
        if owner_file.is_file():
            from telethon.tl.types import User
            owner = json.loads(_dpapi(owner_file.read_bytes(), False))
            if owner["id"] == self.config.owner_id:
                self.tele.session.process_entities([User(id=owner["id"], access_hash=owner["access_hash"])])
        self.calls = PyTgCalls(self.tele)
        # This adapter method is verified against pinned PyTgCalls 3.0.0. The
        # library's play/record convenience methods can otherwise dial after an
        # incoming call expires. Fail closed if this integration surface changes.
        adapter = getattr(self.calls, "_app", None)
        if adapter is None or not callable(getattr(adapter, "request_call", None)):
            raise GateError("Calling library does not expose the required direction guard")
        self.guard_outbound_requests(adapter)

        @self.tele.on(events.NewMessage(incoming=True))
        async def message(event):
            if (event.is_private and event.sender_id == self.config.owner_id
                    and event.date.timestamp() >= self.started_at - 5):
                peer = await event.get_input_sender()
                owner_file.write_bytes(_dpapi(json.dumps({
                    "id": peer.user_id, "access_hash": peer.access_hash}).encode(), True))
                await self.emit({"type": "command", "sender": event.sender_id,
                                 "id": f"tg:{event.chat_id}:{event.id}", "text": event.raw_text})

        @self.calls.on_update()
        async def update(_, event):
            if isinstance(event, ChatUpdate):
                if event.status & ChatUpdate.Status.INCOMING_CALL:
                    if event.chat_id == self.config.owner_id and self.config.calls_enabled and self.peer is None:
                        peer = await self.tele.get_input_entity(self.config.owner_id)
                        owner_file.write_bytes(_dpapi(json.dumps({
                            "id": peer.user_id, "access_hash": peer.access_hash}).encode(), True))
                        self.pending_offer = uuid.uuid4().hex
                        await self.emit({"type": "incoming", "peer": event.chat_id, "offer": self.pending_offer})
                    # Foreign calls are never accepted or bridged.
                elif event.status & ChatUpdate.Status.DISCARDED_CALL and event.chat_id == self.config.owner_id:
                    self.pending_offer = None
                    if event.chat_id == self.peer:
                        await self.hangup()
                        await self.emit({"type": "ended"})
            elif isinstance(event, StreamFrames) and event.chat_id == self.peer and event.direction == Direction.INCOMING:
                for frame in event.frames:
                    await self.emit({"type": "pcm", "data": base64.b64encode(bytes(frame.frame)).decode(), "rate": 48000})

        await self.calls.start()
        await self.emit({"type": "ready"})

    async def connect(self, peer: int):
        if not self.config.calls_enabled or peer != self.config.owner_id or self.peer is not None:
            raise GateError("Call request refused")
        if self.incoming_mode and not self.pending_offer:
            raise GateError("Incoming call has expired")
        from pytgcalls.types import Device, ExternalMedia, MediaStream, RecordStream, CallConfig
        from pytgcalls.types.raw import AudioParameters
        self.peer = peer
        try:
            await self.calls.play(peer, MediaStream(media_path=ExternalMedia.AUDIO,
                                                   audio_parameters=AudioParameters(48000, 1)), config=CallConfig(timeout=45))
            if self.peer != peer:
                raise GateError("Call ended during connection")
            # Keep the outbound clock alive before enabling private-call capture.
            self.player = asyncio.create_task(self._play(Device.MICROPHONE))
            await self.calls.record(peer, RecordStream(audio=True, audio_parameters=AudioParameters(48000, 1)))
            await self.emit({"type": "connected"})
        except BaseException:
            await self.hangup()
            raise

    async def _play(self, device):
        target = time.perf_counter()
        try:
            while self.peer is not None:
                consumed = min(len(self.playback.buffer), self.playback.frame_bytes)
                await self.calls.send_frame(self.peer, device, self.playback.frame())
                if consumed:
                    await self.emit({"type": "audio_ready", "call_id": self.call_id, "consumed": consumed})
                target += .01
                delay = target - time.perf_counter()
                if delay < -.1:
                    target = time.perf_counter()
                # Windows asyncio timers can round 10 ms to ~16 ms. Python's
                # time.sleep uses a high-resolution waitable timer on Windows.
                if delay > 0:
                    await asyncio.to_thread(time.sleep, delay)
                else:
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.emit({"type": "fault", "component": "audio_output", "call_id": self.call_id})

    async def cleanup(self):
        self.peer = None
        self.playback.clear()
        if self.player:
            self.player.cancel()
            await asyncio.gather(self.player, return_exceptions=True)
            self.player = None

    async def hangup(self):
        connection, self.connection = self.connection, None
        if connection and connection is not asyncio.current_task() and not connection.done():
            connection.cancel()
            await asyncio.gather(connection, return_exceptions=True)
        await self._disconnect()

    async def _disconnect(self):
        peer = self.peer
        await self.cleanup()
        if peer is not None:
            try:
                await self.calls.leave_call(peer)
            except Exception:
                pass

    async def command(self, data: dict):
        kind = data.get("type")
        if kind == "connect":
            if self.connection and not self.connection.done():
                raise GateError("A connection request is already pending")
            if self.peer is not None:
                raise GateError("A call is already active")
            self.call_id = data.get("call_id")
            self.incoming_mode = bool(data.get("incoming"))
            if self.incoming_mode and (not self.pending_offer or data.get("offer") != self.pending_offer):
                raise GateError("Incoming call has expired")
            self.connection = asyncio.create_task(self._connect_guarded(int(data["peer"]), self.call_id))
        elif kind == "hangup":
            if data.get("call_id") != self.call_id:
                return
            await self.hangup()
            await self.emit({"type": "ended"})
        elif kind == "audio" and self.peer is not None and data.get("call_id") == self.call_id:
            self.playback.push(base64.b64decode(data["data"], validate=True))
        elif kind == "interrupt":
            if data.get("call_id") == self.call_id:
                consumed = len(self.playback.buffer)
                self.playback.clear()
                # A cleared frame will never be consumed; release its producer.
                await self.emit({"type": "audio_ready", "call_id": self.call_id, "consumed": consumed})
        elif kind == "text":
            # Recipient is never accepted from the model or parent payload.
            text = str(data.get("text", ""))
            for offset in range(0, min(len(text), 32000), 3500):
                await self.tele.send_message(self.config.owner_id, text[offset:offset + 3500], parse_mode=None)

    async def _connect_guarded(self, peer, call_id):
        try:
            await self.connect(peer)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self.emit({"type": "fault", "component": "connect", "call_id": call_id, "error_type": type(error).__name__})

    async def run(self):
        await self.start()
        try:
            while line := await asyncio.to_thread(sys.stdin.readline):
                data = json.loads(line)
                try:
                    await self.command(data)
                except Exception as error:
                    await self.emit({"type": "fault", "component": str(data.get("type", "unknown")),
                                     "call_id": data.get("call_id", self.call_id),
                                     "error_type": type(error).__name__})
        finally:
            await self.hangup()
            if self.tele:
                await self.tele.disconnect()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(NativeTransport(Config.load(args.config), args.state).run())
    except Exception as error:
        print(json.dumps({"type": "fault", "component": "startup", "error_type": type(error).__name__}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
