import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import urllib.error
import ssl
import socket
import io

import desktop
from bot_runner import Controller, DEFAULT_ADMIN_ID, BotAPI, BotError


class SendingInterval(unittest.IsolatedAsyncioTestCase):
    async def test_global_interval_between_different_accounts_and_failed_attempts(self):
        service = desktop.App.__new__(desktop.App)
        service.log = lambda text: None
        clock = [100.0]
        service.send_clock = lambda: clock[0]
        async def sleep(delay): clock[0] += delay
        service.send_sleep = sleep
        attempts = []
        async def send(*args, **kwargs): attempts.append(clock[0])
        async def failed(*args, **kwargs):
            attempts.append(clock[0]); raise desktop.UserPrivacyRestrictedError(request=None)
        first, second = SimpleNamespace(send_message=send), SimpleNamespace(send_message=failed)
        await service.send_with_cooldown(first, 'owner1', 'one')
        with self.assertRaises(desktop.UserPrivacyRestrictedError):
            await service.send_with_cooldown(second, 'owner2', 'two')
        await service.send_with_cooldown(first, 'owner3', 'three')
        self.assertEqual(attempts, [100.0, 115.0, 130.0])


class BotControl(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.patch = patch.object(desktop, 'DATA', Path(self.folder.name)); self.patch.start()
        self.api = SimpleNamespace(call=AsyncMock(return_value={}))
        self.service = SimpleNamespace(task=None, test_task=None, testing=False, paused=False,
                                       clients={}, restricted_accounts=set(), tracker_disabled=set(),
                                       begin=Mock(), stop=Mock(), log=Mock(), submit=Mock())
        self.control = Controller(self.api, self.service, DEFAULT_ADMIN_ID, 123, 'not-a-real-key')

    def tearDown(self):
        self.patch.stop(); self.folder.cleanup()

    def message(self, text, sender=DEFAULT_ADMIN_ID, chat=DEFAULT_ADMIN_ID, kind='private'):
        return {'message': {'from': {'id': sender}, 'chat': {'id': chat, 'type': kind}, 'text': text, 'date': time.time()}}

    def query(self, data, sender=DEFAULT_ADMIN_ID):
        return {'callback_query': {'id': 'query1', 'from': {'id': sender}, 'message': {'chat': {'id': DEFAULT_ADMIN_ID, 'type': 'private'}}, 'data': data}}

    async def test_unknown_users_and_groups_cannot_read_or_control(self):
        for update in (self.message('/status', sender=1), self.message('/run', chat=-100, kind='group'), self.query('mode:live', sender=1)):
            await self.control.handle(update)
        self.api.call.assert_not_awaited(); self.service.begin.assert_not_called()

    async def test_live_mode_requires_one_time_owner_confirmation(self):
        await self.control.handle(self.query('mode:live'))
        self.assertFalse(self.control.live)
        nonce = next(iter(self.control.confirmations))
        await self.control.handle(self.query('confirm:'+nonce))
        self.assertTrue(self.control.live)
        self.assertNotIn(nonce, self.control.confirmations)
        self.control.live = False
        await self.control.handle(self.query('confirm:'+nonce))
        self.assertFalse(self.control.live)

    async def test_start_captures_selected_collections_in_preview_by_default(self):
        self.service.clients = {0: object()}
        self.control.selection = {'all': False, 'names': ['Mirage Lamp']}
        await self.control.handle(self.message('/run'))
        self.service.begin.assert_called_once_with(False, {'all': False, 'names': ['Mirage Lamp']})

    async def test_remote_gifts_and_tracker_switch_persist(self):
        self.control.catalog = [{'id': 1, 'title': 'Mirage Lamp'}, {'id': 2, 'title': 'Resistance Dog'}]
        await self.control.handle(self.query('gift:1:0'))
        self.assertEqual(self.control.selection, {'all': False, 'names': ['Resistance Dog']})
        self.service.clients = {0: object()}
        await self.control.handle(self.query('search:0'))
        self.assertEqual(self.service.tracker_disabled, {0})
        self.assertEqual(desktop.read_settings(Path(self.folder.name)/'tracker_disabled.json', []), [0])

    async def test_expired_confirmation_cannot_enable_sending(self):
        self.control.confirmations['old'] = ('live', time.monotonic()-1)
        await self.control.handle(self.query('confirm:old'))
        self.assertFalse(self.control.live)

    async def test_http_errors_do_not_expose_bot_token(self):
        token = 'dummy-private-token'
        error = urllib.error.HTTPError('https://api.telegram.org/bot'+token+'/getMe', 401, 'Unauthorized', {}, None)
        with patch('urllib.request.urlopen', side_effect=error):
            with self.assertRaises(BotError) as failure:
                await BotAPI(token).call('getMe')
        self.assertNotIn(token, str(failure.exception))
        self.assertEqual(failure.exception.code, 401)

    async def test_temporary_read_failure_retries_with_verified_tls(self):
        api = BotAPI('dummy-private-token')
        response = io.BytesIO(b'{"ok":true,"result":{"id":1}}')
        with patch('urllib.request.urlopen', side_effect=[urllib.error.URLError(TimeoutError()), response]) as request, patch('bot_runner.asyncio.sleep', AsyncMock()) as wait:
            self.assertEqual(await api.call('getMe'), {'id': 1})
        self.assertEqual(request.call_count, 2)
        wait.assert_awaited_once_with(1)
        self.assertEqual(request.call_args.kwargs['context'].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(request.call_args.kwargs['context'].check_hostname)

    async def test_delivery_timeout_is_not_automatically_retried(self):
        with patch('urllib.request.urlopen', side_effect=urllib.error.URLError(TimeoutError())) as request:
            with self.assertRaises(BotError) as failure:
                await BotAPI('dummy-private-token').call('sendMessage', chat_id=1, text='test')
        self.assertEqual(request.call_count, 1)
        self.assertIn('Таймаут', str(failure.exception))

    def test_tls_and_dns_failures_have_distinct_safe_diagnostics(self):
        tls = BotAPI.connection_error(urllib.error.URLError(ssl.SSLCertVerificationError(1, 'dummy-private-token')))
        dns = BotAPI.connection_error(urllib.error.URLError(socket.gaierror()))
        self.assertIn('TLS', str(tls)); self.assertFalse(tls.retryable)
        self.assertIn('DNS', str(dns)); self.assertTrue(dns.retryable)
        self.assertNotIn('dummy-private-token', str(tls))


if __name__ == '__main__': unittest.main()
