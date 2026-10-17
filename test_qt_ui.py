import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import asyncio
import queue
import time
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
from types import SimpleNamespace
from PySide6.QtWidgets import QApplication
import desktop
import ui_qt


class DesktopUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle('Fusion')
        cls.app.setStyleSheet(ui_qt.STYLE)

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.patches = [patch.object(ui_qt, 'DATA', Path(self.folder.name)), patch.object(desktop, 'DATA', Path(self.folder.name))]
        for item in self.patches: item.start()
        self.window = ui_qt.Window()
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        for _ in range(300):
            self.app.processEvents()
            if self.window.closed: break
            time.sleep(0.001)
        self.assertTrue(self.window.closed, 'Backend must disconnect on close')
        self.assertFalse(self.window.backend.thread.is_alive())
        for item in reversed(self.patches): item.stop()
        self.folder.cleanup()

    def test_account_dialog_status_and_logout_event(self):
        self.window.add_account()
        self.assertIn(0, self.window.auth_dialogs)
        self.window.backend.rows[0][0].set('+10000000000')
        self.window.backend.logs.put(('status', (0, 'Подключён')))
        self.window.drain()
        self.assertEqual(self.window.connected, {0})
        self.assertNotIn(0, self.window.auth_dialogs)
        self.window.backend.mark_peer_flood(0)
        self.window.drain()
        self.assertEqual(self.window.metric_values[0].text(), '0')
        self.window.backend.logs.put(('signed_out', 0))
        self.window.drain()
        self.assertFalse(self.window.accounts)
        self.assertEqual(self.window.backend.rows[0][0].get(), '')

    def test_gift_selection_search_and_save(self):
        self.window.all_gifts.setChecked(False)
        self.window.toggle_all()
        self.window.gift_checks['Mirage Lamp'].click()
        self.window.gift_search.setText('Mirage')
        self.assertFalse(self.window.gift_cards['Mirage Lamp'].isHidden())
        self.assertTrue(self.window.gift_cards['Resistance Dog'].isHidden())
        self.window.save_gifts()
        self.assertEqual(self.window.selection, {'all': False, 'names': ['Mirage Lamp']})
        self.assertEqual(desktop.read_settings(Path(self.folder.name)/'gifts.json', {}), self.window.selection)

    def test_tracker_switch_persists_without_logging_out(self):
        self.window.backend.logs.put(('status', (0, 'Подключён')))
        self.window.drain()
        self.window.toggle_tracker(0, False)
        self.assertEqual(self.window.tracker_disabled, {0})
        self.assertEqual(desktop.read_settings(Path(self.folder.name)/'tracker_disabled.json', []), [0])
        self.assertEqual(self.window.connected, {0})
        self.window.toggle_tracker(0, True)
        self.assertEqual(self.window.tracker_disabled, set())

    def test_detached_log_survives_reopen_and_escapes_html(self):
        self.window.open_logs()
        self.window.backend.logs.put(('log', ('info', 'Gift <name> & owner')))
        self.window.drain()
        self.assertIn('Gift <name> & owner', self.window.detached.log_text.toPlainText())
        self.window.detached.reject()
        self.assertIsNone(self.window.detached)
        self.window.open_logs()
        self.assertIn('Gift <name> & owner', self.window.detached.log_text.toPlainText())
        self.window.clear_logs()
        self.assertFalse(self.window.history)
        self.assertEqual(self.window.detached.log_text.toPlainText(), '')

    def test_navigation_preserves_selection(self):
        for i in (1, 2, 3, 4, 0):
            self.window.navigate(i)
            self.app.processEvents()
            self.assertEqual(self.window.stack.currentIndex(), i)
        self.assertTrue(self.window.selection['all'])

    def test_collection_metric_shows_requested_and_available_counts(self):
        self.window.selection = {'all': False, 'names': ['Mirage Lamp', 'Missing Collection']}
        self.window.catalog = [{'id': 1, 'title': 'Mirage Lamp'}]
        self.window.update_metrics()
        self.assertEqual(self.window.metric_values[1].text(), '1 / 2')


class TrialSending(unittest.IsolatedAsyncioTestCase):
    def backend(self, clients):
        backend = ui_qt.Backend.__new__(ui_qt.Backend)
        backend.clients, backend.restricted_accounts, backend.connecting = clients, set(), set()
        backend.task, backend.testing, backend.test_task = None, False, None
        backend.logs = queue.Queue()
        backend.log = lambda text: None
        return backend

    async def test_each_authorized_account_sends_exact_text_once(self):
        clients = {i: SimpleNamespace(is_user_authorized=AsyncMock(return_value=i != 2), send_message=AsyncMock()) for i in range(4)}
        backend = self.backend(clients)
        backend.restricted_accounts = {3}
        with patch.object(ui_qt.asyncio, 'sleep', AsyncMock()):
            await backend.send_test()
        for i in (0, 1):
            clients[i].send_message.assert_awaited_once_with('rus81quuodh', 'dcsnjdc Mirage Lamp #51160 1098 stars ru', parse_mode=None)
        for i in (2, 3): clients[i].send_message.assert_not_awaited()
        self.assertFalse(backend.testing)

    async def test_peerflood_stops_trial_without_using_next_account(self):
        clients = {i: SimpleNamespace(is_user_authorized=AsyncMock(return_value=True), send_message=AsyncMock()) for i in range(2)}
        clients[0].send_message.side_effect = desktop.PeerFloodError(request=None)
        backend = self.backend(clients)
        with tempfile.TemporaryDirectory() as folder, patch.object(desktop, 'DATA', Path(folder)):
            await backend.send_test()
        clients[1].send_message.assert_not_awaited()
        self.assertEqual(backend.restricted_accounts, {0})
        self.assertFalse(backend.testing)

    async def test_duplicate_trial_is_rejected(self):
        client = SimpleNamespace(is_user_authorized=AsyncMock(return_value=True), send_message=AsyncMock())
        backend = self.backend({0: client}); backend.testing = True
        await backend.send_test()
        client.send_message.assert_not_awaited()


if __name__ == '__main__': unittest.main()
