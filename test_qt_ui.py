import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
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


if __name__ == '__main__': unittest.main()
