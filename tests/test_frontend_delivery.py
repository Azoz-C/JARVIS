import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from jarvis.config import Config
from jarvis.store import Store
from jarvis.supervisor import Supervisor
from jarvis.ui_transport import FrontendDeliveryError, FrontendSender, prompt_digest
from jarvis.ui_sender import NativeComposer, trusted_executable

THREAD = '11111111-2222-3333-4444-555555555555'


class FrontendDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'state.db')
        self.codex = type('Reader', (), {})()
        self.codex.read_thread = AsyncMock(return_value={'thread': {
            'id': THREAD, 'name': 'Existing desktop chat', 'cwd': str(self.root),
            'status': {'type': 'active'}}})
        self.codex.history = AsyncMock(return_value={'data': []})
        self.codex.resume = AsyncMock(side_effect=AssertionError('Desktop writer must never be resumed'))
        self.codex.turn = AsyncMock(side_effect=AssertionError('No backend submission'))
        self.supervisor = Supervisor(Config(), self.store, self.codex)
        self.sender = self.supervisor.frontend_sender
        async def send(thread, title, prompt, identifier):
            return {'sent': True, 'transport': 'interface', 'thread': thread,
                    'ui_echo_observed': True, 'request_id': identifier, 'prompt_sha256': prompt_digest(prompt)}
        self.sender.send = AsyncMock(side_effect=send)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_active_desktop_chat_uses_composer_without_backend_writer(self):
        result = await self.supervisor.send_existing(THREAD, 'Exact owner prompt')
        self.assertTrue(result['ui_echo_observed'])
        self.assertEqual(result['state'], 'frontend_submitted')
        self.assertIsNone(result['turn'])
        self.sender.send.assert_awaited_once_with(THREAD, 'Existing desktop chat', 'Exact owner prompt', result['task'])
        self.codex.resume.assert_not_awaited()
        self.codex.turn.assert_not_awaited()

    async def test_native_history_tracks_matching_turn_and_response(self):
        result = await self.supervisor.send_existing(THREAD, 'Review')
        self.codex.history.return_value = {'data': [{'id': 'native-turn', 'status': 'completed', 'items': [
            {'type': 'userMessage', 'content': [{'type': 'text', 'text': 'Review\n'}]},
            {'type': 'agentMessage', 'text': 'Actual frontend response'}]}]}
        await self.supervisor.poll_frontend()
        self.assertEqual(self.store.task(result['task'])['state'], 'completed')
        self.assertEqual(self.store.setting('frontend_turn:' + result['task']), 'native-turn')
        self.assertIn('Actual frontend response', self.store.task(result['task'])['summary'])

    async def test_uncertain_send_is_not_automatically_replayed(self):
        self.sender.send.side_effect = FrontendDeliveryError('Echo unavailable', True)
        with self.assertRaises(FrontendDeliveryError):
            await self.supervisor.send_existing(THREAD, 'Review')
        with self.assertRaisesRegex(Exception, 'prior send is uncertain'):
            await self.supervisor.send_existing(THREAD, 'Review')
        self.assertEqual(self.sender.send.await_count, 1)

    async def test_user_draft_failure_has_no_backend_fallback(self):
        self.sender.send.side_effect = FrontendDeliveryError('Unsent draft')
        with self.assertRaisesRegex(FrontendDeliveryError, 'Unsent draft'):
            await self.supervisor.send_existing(THREAD, 'Review')
        self.codex.turn.assert_not_awaited()
        self.assertEqual(self.store.rows('SELECT state FROM tasks')[0]['state'], 'needs_input')
        self.assertFalse(self.store.setting('uncertain_chat:' + THREAD))

    async def test_unrelated_history_cannot_confirm_completion(self):
        self.codex.history.return_value = {'data': [{'id': 'other', 'status': 'completed', 'items': []}]}
        result = await self.supervisor.send_existing(THREAD, 'Review')
        self.assertEqual(result['state'], 'frontend_submitted')

    async def test_old_identical_prompt_cannot_confirm_new_submission(self):
        old = {'id': 'old', 'status': 'completed', 'items': [
            {'type': 'userMessage', 'content': [{'type': 'text', 'text': 'Review'}]}]}
        self.codex.history.return_value = {'data': [old]}
        result = await self.supervisor.send_existing(THREAD, 'Review')
        self.assertIsNone(result['turn'])
        self.codex.history.return_value = {'data': [{**old, 'id': 'new'}, old]}
        await self.supervisor.poll_frontend()
        self.assertEqual(self.store.setting('frontend_turn:' + result['task']), 'new')

    async def test_transport_passes_only_owner_text_to_composer(self):
        import json
        prompt = 'make a second hand verification of the AI security and integrity.'
        async def communicate(data):
            request = json.loads(data)
            self.assertEqual(request['payload'], prompt)
            self.assertNotIn('marker', request)
            return (json.dumps({'sent': True, 'thread': THREAD, 'request_id': 'test',
                'prompt_sha256': prompt_digest(prompt), 'ui_echo_observed': True}).encode(), b'')
        process = Mock(communicate=AsyncMock(side_effect=communicate))
        with patch('asyncio.create_subprocess_exec', AsyncMock(return_value=process)):
            result = await FrontendSender(self.root).send(THREAD, 'Chat', prompt, 'test')
        self.assertTrue(result['sent'])

    async def test_answer_to_frontend_task_uses_same_visible_chat(self):
        result = await self.supervisor.send_existing(THREAD, 'Initial request')
        followup = await self.supervisor.answer(result['task'], 'Exact clarification')
        self.assertEqual(followup['thread'], THREAD)
        self.assertEqual(self.sender.send.await_count, 2)
        self.codex.turn.assert_not_awaited()

    async def test_run_cannot_replay_frontend_task_via_backend(self):
        result = await self.supervisor.send_existing(THREAD, 'Review')
        with self.assertRaisesRegex(Exception, 'not /run to replay'):
            await self.supervisor.run(result['task'])
        self.codex.resume.assert_not_awaited()

    async def test_transient_interrupted_history_is_reconciled_without_resending(self):
        result = await self.supervisor.send_existing(THREAD, 'Review')
        turn = {'id': 'native', 'status': 'interrupted', 'items': [
            {'type': 'userMessage', 'content': [{'type': 'text', 'text': 'Review\n'}]}]}
        self.codex.history.return_value = {'data': [turn]}
        await self.supervisor.poll_frontend()
        self.assertEqual(self.store.task(result['task'])['state'], 'frontend_submitted')
        turn['status'] = 'completed'
        await self.supervisor.poll_frontend()
        self.assertEqual(self.store.task(result['task'])['state'], 'completed')
        self.assertEqual(self.sender.send.await_count, 1)

    def test_composer_placeholder_and_untrusted_executable(self):
        editor = type('Edit', (), {'get_value': lambda _: '\u200b Work with Codex '})()
        self.assertEqual(NativeComposer.value(editor), '')
        self.assertFalse(trusted_executable(Path('C:/Downloads/ChatGPT.exe')))


class NativeGuardTests(unittest.TestCase):
    def composer(self, draft=''):
        c = NativeComposer.__new__(NativeComposer)
        c.navigate = Mock()
        c.composer = Mock(return_value=Mock(get_value=Mock(return_value=draft)))
        c.paste = Mock()
        c.rendered_message = Mock(return_value=0)
        c.current_thread = Mock(return_value=THREAD)
        c.document = Mock()
        c.foreground = Mock()
        c.clicked = False
        return c

    def test_unsent_user_draft_is_preserved(self):
        c = self.composer('User draft')
        with self.assertRaisesRegex(Exception, 'unsent draft'):
            c.send(THREAD, 'Chat', 'payload', 'marker')
        c.paste.assert_not_called()
        c.document.assert_not_called()

    def test_changed_chat_after_staging_cannot_press_send(self):
        c = self.composer()
        c.current_thread.return_value = 'wrong-chat'
        with self.assertRaisesRegex(Exception, 'Target chat changed'):
            c.send(THREAD, 'Chat', 'payload', 'marker')
        c.document.assert_not_called()
        self.assertFalse(c.clicked)

    def test_stop_button_cannot_be_used_as_send(self):
        c = self.composer()
        stop = Mock(); stop.element_info.name = 'Stop'
        c.document.return_value.descendants.return_value = [stop]
        with self.assertRaisesRegex(Exception, 'Send control'):
            c.send(THREAD, 'Chat', 'payload', 'marker')
        stop.invoke.assert_not_called()
