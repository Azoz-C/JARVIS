import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

from jarvis.config import Config
from jarvis.desktop import save_config
from jarvis.platform import startup_xml
from jarvis.transport import NativeTransport


class DesktopTests(unittest.TestCase):
    def test_config_roundtrip_preserves_paths_and_call_gates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            config = replace(Config(), owner_id=123, whisper_bin='C:\\Project folder\\whisper-cli.exe')
            save_config(path, config)
            self.assertEqual(Config.load(path), config)
            self.assertFalse(Config.load(path).calls_enabled)
            self.assertFalse(Config.load(path).free_cloud_confirmed)

    def test_startup_is_non_elevated_and_does_not_wake_laptop(self):
        with tempfile.TemporaryDirectory() as directory:
            xml = startup_xml(Path(directory) / "config.toml", Path(directory))
            self.assertIn("LeastPrivilege", xml)
            self.assertIn("<WakeToRun>false</WakeToRun>", xml)
            self.assertIn("--tray", xml)
            self.assertNotIn("HighestAvailable", xml)


class NativeCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_incoming_session_cannot_silently_dial_out(self):
        transport = NativeTransport(replace(Config(), owner_id=123, calls_enabled=True), Path("."))
        adapter = type("FakeAdapter", (), {})()
        adapter.request_call = AsyncMock()
        original = adapter.request_call
        transport.guard_outbound_requests(adapter)
        transport.peer, transport.call_id, transport.incoming_mode = 123, "fixture", True
        from jarvis.security import GateError
        with self.assertRaises(GateError):
            await adapter.request_call(123, b"fixture", None, False)
        original.assert_not_called()
        transport.incoming_mode = False
        await adapter.request_call(123, b"fixture", None, False)
        original.assert_awaited_once()

    async def test_expired_incoming_offer_does_not_connect(self):
        transport = NativeTransport(replace(Config(), owner_id=123, calls_enabled=True), Path("."))
        transport.connect = AsyncMock()
        from jarvis.security import GateError
        with self.assertRaises(GateError):
            await transport.command({"type": "connect", "peer": 123, "call_id": "fixture", "incoming": True, "offer": "expired"})
        transport.connect.assert_not_called()

    async def test_stale_hangup_does_not_end_a_new_call(self):
        transport = NativeTransport(replace(Config(), owner_id=123, calls_enabled=True), Path("."))
        transport.peer = 123
        transport.call_id = "new-call"
        transport.calls = type("FakeCalls", (), {"leave_call": AsyncMock()})()
        await transport.command({"type": "hangup", "call_id": "old-call"})
        self.assertEqual(transport.peer, 123)
        transport.calls.leave_call.assert_not_called()

    async def test_hangup_can_cancel_while_outgoing_call_is_still_ringing(self):
        transport = NativeTransport(replace(Config(), owner_id=123, calls_enabled=True), Path("."))
        started = asyncio.Event()
        async def connect(peer):
            transport.peer = peer
            started.set()
            await asyncio.sleep(60)
        transport.connect = connect
        transport.emit = AsyncMock()
        transport.calls = type("FakeCalls", (), {"leave_call": AsyncMock()})()
        await transport.command({"type": "connect", "peer": 123})
        await asyncio.wait_for(started.wait(), .5)
        await asyncio.wait_for(transport.command({"type": "hangup"}), .5)
        self.assertIsNone(transport.peer)
        self.assertIsNone(transport.connection)
        transport.calls.leave_call.assert_awaited_once_with(123)
