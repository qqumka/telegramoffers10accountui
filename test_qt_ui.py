import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import asyncio
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
from PySide6.QtWidgets import QApplication
from telethon.tl.types import User
from telethon.errors import SessionPasswordNeededError
import bot_core
import ui_qt
from test_manual_queue import RESULTS, PAIRS


class DesktopUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle('Fusion'); cls.app.setStyleSheet(ui_qt.STYLE)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(bot_core,'DATA',Path(self.tmp.name)), patch.object(ui_qt,'DATA',Path(self.tmp.name))]
        for item in self.patches: item.start()
        self.w = ui_qt.Window(); self.errors = []; self.w.warn = self.errors.append; self.w.show()
        self.wait(lambda: bool(self.w.state))
        self.backend = self.w.backend
        self.client = SimpleNamespace(get_entity=AsyncMock(), send_message=AsyncMock(), disconnect=AsyncMock())
        async def resolve(name):
            names = [n.lower() for n,_ in PAIRS]
            return User(id=100+names.index(name) if name in names else 999,first_name='小猫')
        self.client.get_entity.side_effect = resolve
        async def install():
            self.backend.service.clients[0] = self.client
            self.backend.emit_snapshot()
        asyncio.run_coroutine_threadsafe(install(),self.backend.loop).result(3)
        self.wait(lambda: any(item['slot']==0 for item in self.w.state['accounts']))

    def wait(self, condition, timeout=4):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            self.app.processEvents(); self.w.drain()
            if condition(): return
            time.sleep(.005)
        self.fail('Timed out waiting for desktop state')

    def close_window(self):
        self.w.close(); self.wait(lambda:self.w.closed)
        self.assertFalse(self.w.backend.thread.is_alive())
        self.assertIsNone(self.w.shutdown_future.exception())

    def tearDown(self):
        if not self.w.closed: self.close_window()
        for item in reversed(self.patches): item.stop()
        self.tmp.cleanup()

    def import_results(self, text=RESULTS):
        self.w.input_text.setPlainText(text); self.w.price.setText('425 gram'); self.w.import_button.click()
        self.wait(lambda: self.w.draft_table.rowCount()>0)

    def test_import_edit_confirm_and_send_are_separate(self):
        self.import_results()
        self.assertEqual(self.w.draft_table.rowCount(),14)
        self.assertEqual(self.w.draft_table.cellWidget(0,4).currentText(),'cn')
        self.assertEqual(self.w.state['queue'],[])
        self.client.send_message.assert_not_awaited()
        self.w.draft_table.cellWidget(0,4).setCurrentText('ar')
        self.w.draft_table.item(0,3).setText('430')
        self.assertIn('430 gram ar',self.w.draft_table.item(0,5).text())
        self.w.enqueue_button.click()
        self.wait(lambda:self.w.draft_table.rowCount()==0)
        self.assertEqual(self.w.state['counts']['pending'],14)
        self.assertIn('430 gram ar',self.w.state['queue'][-1][2])
        self.client.send_message.assert_not_awaited()
        self.w.navigate(1); self.assertTrue(self.w.start_button.isEnabled())

    def test_run_sends_only_confirmed_queue(self):
        self.import_results('✅ Результаты (1)\n1. [T] @octalend_vault - Scared Cat (#16951)')
        self.w.enqueue_button.click(); self.wait(lambda:self.w.state['counts'].get('pending')==1)
        self.w.start_button.click(); self.wait(lambda:self.w.state['counts'].get('sent')==1)
        self.client.send_message.assert_awaited_once()
        args = self.client.send_message.await_args
        self.assertEqual(args.args[1],'jdcsnjdc Scared Cat #16951 425 gram cn')
        self.assertIsNone(args.kwargs['parse_mode'])
        self.assertEqual(self.w.history_table.rowCount(),1)

    def test_wait_for_price_cancel_and_invalid_price(self):
        self.w.input_text.setPlainText(RESULTS); self.w.import_button.click()
        self.wait(lambda:self.w.state.get('pending')==14)
        self.assertFalse(self.w.enqueue_button.isEnabled())
        self.client.get_entity.assert_not_awaited()
        self.w.price.setText('-5'); self.w.apply_price()
        self.wait(lambda:bool(self.errors))
        self.assertEqual(self.w.state['pending'],14)
        with patch.object(ui_qt.QMessageBox,'question',return_value=ui_qt.QMessageBox.StandardButton.Yes): self.w.cancel_button.click()
        self.wait(lambda:self.w.state['pending']==0)
        self.assertTrue(self.w.import_button.isEnabled())

    def test_draft_is_preserved_across_window_restart(self):
        self.import_results(); self.close_window()
        self.w = ui_qt.Window(); self.errors = []; self.w.warn = self.errors.append; self.w.show()
        self.wait(lambda:self.w.draft_table.rowCount()==14)
        self.assertIn('425 gram cn',self.w.draft_table.item(0,5).text())
        self.assertFalse(self.w.start_button.isEnabled())

    def test_save_unchanged_table_clears_dirty_flag(self):
        self.import_results()
        combo = self.w.draft_table.cellWidget(0,4); combo.setCurrentText('ar'); combo.setCurrentText('cn')
        self.assertTrue(self.w.dirty)
        self.w.save_button.click(); self.wait(lambda:not self.w.dirty)

    def test_six_pages_without_tracker_controls(self):
        self.assertEqual(self.w.stack.count(),6)
        self.assertEqual([self.w.nav_group.button(i).text() for i in range(6)],['Подготовка','Очередь','Аккаунты','История','Логи','Настройки'])
        self.w.navigate(2); self.w.add_account()
        self.assertIn(1,self.w.auth_dialogs)
        self.assertFalse(hasattr(self.w,'live'))
        self.assertFalse(hasattr(self.w,'catalog'))

    def test_two_factor_second_step_does_not_reuse_code(self):
        client = SimpleNamespace(sign_in=AsyncMock(side_effect=[SessionPasswordNeededError(request=None),None]),disconnect=AsyncMock())
        async def install():
            self.backend.auth_clients[1]=client
            self.backend.hashes[1]='dummy'; self.backend.phones[1]='+10000000000'
        asyncio.run_coroutine_threadsafe(install(),self.backend.loop).result(3)
        self.backend.submit('login',1,'12345','').result(3)
        self.assertEqual(self.backend.account_status[1],'Введите пароль 2FA')
        self.backend.submit('login',1,'12345','example-password').result(3)
        self.assertIs(self.backend.service.clients[1],client)
        self.assertEqual(client.sign_in.await_count,2)
        self.assertEqual(client.sign_in.await_args.kwargs,{'password':'example-password'})


if __name__ == '__main__': unittest.main()
