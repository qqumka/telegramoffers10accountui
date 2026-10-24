import asyncio
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import bot_core as desktop
from telethon.tl.types import User
from bot_runner import Controller, DEFAULT_ADMIN_ID
from manual_queue import ManualQueue, language, parse_line, render, parse_results, parse_price


PAIRS = [('octalend_vault',16951), ('jilly_springva',839), ('poliiiii1na',1789), ('Antakov_Mixail',2137), ('ridicuIed',2619), ('zeroxrandom',2912), ('ge_sofa',3492), ('Anonim_35',9911), ('Shauryaax',14895), ('ChiuyanHo',15057), ('Shy_fluffy',15905), ('Yz080804',16351), ('deaaddd',17876), ('meloooch',18750)]
RESULTS = '✅ **Результаты (14)**\n*Страница 1 из 1*\n\n' + '\n'.join(
    f'**{i}.** [\\[T\\]](https://telegra.ph/template) [⭐ Lv.1] @{name} - [Scared Cat (#{number})' + (' (+1)' if i == 1 else '') + f'](https://t.me/nft/ScaredCat-{number})'
    for i,(name,number) in enumerate(PAIRS,1)) + '\n🚫 Скрыто ранее просмотренных: **3**\n🗑 Скрыто магазинов/ботов: **28**\n🔍 Бот-фильтр: проверено 103, подошли 14, ошибки 77 (9.7s)'


class ResultsTests(unittest.TestCase):
    def test_complete_markdown_message(self):
        items = parse_results(RESULTS)
        self.assertEqual(len(items),14)
        self.assertEqual([(item['recipient'], int(item['number'])) for item in items], [(name.lower(),number) for name,number in PAIRS])
        self.assertTrue(all(item['title'] == 'Scared Cat' and item['price'] is None for item in items))

    def test_plain_telegram_text(self):
        items = parse_results('✅ Результаты (1)\n1. [T] [⭐ Lv.-1] @octalend_vault - Scared Cat (#16951) (+1)\n🚫 Скрыто: 3')
        self.assertEqual(items[0]['number'], '16951')
        self.assertEqual(items[0]['title'], 'Scared Cat')

    def test_invalid_rows_are_not_silently_skipped(self):
        with self.assertRaises(ValueError): parse_results(RESULTS.replace('@ge_sofa -', 'invalid -'))
        with self.assertRaises(ValueError): parse_results(RESULTS.replace('ScaredCat-16951', 'ScaredCat-999'))
        self.assertIsNone(parse_results('@person1 Scared Cat #16951 425 gram'))
        self.assertEqual(parse_price('425,5 gram'), '425.5')
        with self.assertRaises(ValueError): parse_price('0 gram')


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'offers.sqlite'
        self.q = ManualQueue(self.path)

    def tearDown(self):
        self.q.db.close()
        self.tmp.cleanup()

    def item(self, name='person1', owner=100):
        item = parse_line(f'@{name} Scared Cat #16951 425 gram')
        item.update(owner_id=owner, lang='ru')
        return item

    def test_exact_final_price_and_language(self):
        item = self.item()
        self.assertEqual(render(item), 'jdcsnjdc Scared Cat #16951 425 gram ru')
        self.assertEqual(language('小猫'), 'cn')
        self.assertEqual(language('محمد'), 'ar')
        self.assertEqual(language('Name Имя'), 'ru')
        self.assertEqual(language('𠀀'), 'cn')
        item = parse_line('@person1 jdcsnjdc Scared Cat #16951 425,5 gram ar')
        self.assertEqual(render(item), 'jdcsnjdc Scared Cat #16951 425.5 gram ar')
        for line in ('@person1 Cat #1 0 gram', '@person1 Cat #1 -5 gram', '@person1 Cat #1 100 stars', '@person1 Cat #1 100 gram en'):
            with self.assertRaises(ValueError): parse_line(line)

    def test_restart_queue_and_uncertain_delivery_and_round_robin(self):
        self.assertEqual(self.q.add([self.item(), self.item('person2', 200)]), 2)
        row = self.q.next()
        self.assertEqual(self.q.claim(row, [0, 1]), 0)
        self.q.db.close()
        self.q = ManualQueue(self.path)
        self.assertEqual(self.q.db.execute('SELECT status FROM manual_queue WHERE id=?', (row[0],)).fetchone()[0], 'uncertain')
        self.assertEqual(self.q.next()[1], 'person2')
        self.assertEqual(self.q.claim(self.q.next(), [0, 1]), 1)
        self.assertGreater(self.q.delay(), 13)
        self.assertEqual(self.q.add([self.item('renamed', 100)]), 0)

    def test_old_history_and_duplicate_rows(self):
        self.q.store.reserve('old', 'person1', [0], 'old', owner_id=100)
        self.assertEqual(self.q.add([self.item('renamed', 100), self.item('person1', 999)]), 0)
        self.assertEqual(self.q.add([self.item('person2', 200), self.item('alias2', 200)]), 1)
        self.q.log('durable log')
        self.q.db.close()
        self.q = ManualQueue(self.path)
        self.assertEqual(self.q.db.execute('SELECT message FROM manual_log ORDER BY id DESC LIMIT 1').fetchone()[0], 'durable log')


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = patch.object(desktop, 'DATA', Path(self.tmp.name)); self.data.start()
        self.api = SimpleNamespace(call=AsyncMock(return_value={}))
        self.clients = {}
        for slot in (0, 1):
            client = SimpleNamespace(get_entity=AsyncMock(return_value=User(id=100+slot, first_name='小猫' if slot == 0 else 'User', last_name='', bot=False, is_self=False)), send_message=AsyncMock())
            self.clients[slot] = client
        self.service = SimpleNamespace(clients=self.clients, restricted_accounts=set(), connecting=set(), task=None, test_task=None, testing=False, paused=False, logs=None, finished=Mock(), peer_failure=Mock(), peer_success=Mock(), send_with_cooldown=AsyncMock())
        self.c = Controller(self.api, self.service, DEFAULT_ADMIN_ID, 123, 'unused')

    async def asyncTearDown(self):
        await self.c.stop()
        self.c.queue.db.close()
        self.data.stop(); self.tmp.cleanup()

    async def message(self, text):
        await self.c.handle({'message': {'from': {'id': DEFAULT_ADMIN_ID}, 'chat': {'id': DEFAULT_ADMIN_ID, 'type': 'private'}, 'date': time.time(), 'text': text}})

    async def test_preview_edit_confirm_and_restart(self):
        await self.message('@person1 Scared Cat #16951 425 gram')
        self.assertEqual(self.c.draft[0]['lang'], 'cn')
        self.service.send_with_cooldown.assert_not_awaited()
        self.assertIsNone(self.c.queue.next())
        await self.message('/lang 1 ar')
        await self.message('/enqueue')
        self.assertEqual(self.c.queue.next()[3], 'jdcsnjdc Scared Cat #16951 425 gram ar')
        self.assertFalse(self.c.draft)
        self.c.queue.db.close()
        self.c = Controller(self.api, self.service, DEFAULT_ADMIN_ID, 123, 'unused')
        self.assertIsNotNone(self.c.queue.next())
        await self.c.send_queue()
        self.service.send_with_cooldown.assert_awaited_once()
        self.assertIn('sent: 1', self.c.queue.summary())

    async def test_draft_survives_restart_and_stop(self):
        await self.message('@person1 Scared Cat #16951 425 gram ru')
        self.c.queue.db.close()
        self.c = Controller(self.api, self.service, DEFAULT_ADMIN_ID, 123, 'unused')
        self.assertEqual(self.c.draft[0]['lang'], 'ru')
        await self.message('/enqueue')
        await self.message('/stop')
        self.assertIsNotNone(self.c.queue.next())

    async def test_changed_username_is_never_sent(self):
        await self.message('@person1 Scared Cat #16951 425 gram')
        await self.message('/enqueue')
        self.clients[0].get_entity.return_value.id = 999
        await self.c.send_queue()
        self.service.send_with_cooldown.assert_not_awaited()
        self.assertIn('failed: 1', self.c.queue.summary())

    async def test_results_wait_for_price_persist_then_allow_manual_changes(self):
        await self.message(RESULTS)
        self.assertEqual(len(self.c.pending_import),14)
        self.clients[0].get_entity.assert_not_awaited()
        self.assertIsNone(self.c.queue.next())
        self.c.queue.db.close()
        self.c = Controller(self.api, self.service, DEFAULT_ADMIN_ID, 123, 'unused')
        self.assertEqual(len(self.c.pending_import),14)
        async def resolve(name):
            return User(id=1000+[n.lower() for n,_ in PAIRS].index(name), first_name='小猫')
        self.clients[0].get_entity.side_effect = resolve
        await self.message('425 gram')
        self.assertFalse(self.c.pending_import)
        self.assertEqual(len(self.c.draft),14)
        self.assertEqual(render(self.c.draft[0]),'jdcsnjdc Scared Cat #16951 425 gram cn')
        await self.message('/lang 1 ar')
        await self.message('/price 430 gram')
        self.assertEqual(render(self.c.draft[0]),'jdcsnjdc Scared Cat #16951 430 gram ar')
        await self.message('/enqueue')
        self.assertIn('pending: 14',self.c.queue.summary())
        self.service.send_with_cooldown.assert_not_awaited()

    async def test_invalid_price_preserves_import_and_cancel_clears_it(self):
        await self.message(RESULTS)
        await self.message('wrong price')
        await self.message('/enqueue')
        self.assertEqual(len(self.c.pending_import),14)
        self.assertIsNone(self.c.queue.next())
        await self.message('/cancel')
        self.assertFalse(self.c.pending_import)

    async def test_worker_rotates_accounts_without_reordering(self):
        items = []
        for i in range(3):
            item = parse_line(f'@person{i} Cat #{i+1} 425 gram ru')
            item['owner_id'] = 100+i
            items.append(item)
        self.c.queue.add(items)
        async def resolve(name): return User(id=100+int(name[-1]), first_name='Name')
        for client in self.clients.values(): client.get_entity.side_effect = resolve
        with patch.object(self.c.queue, 'delay', return_value=0):
            await self.c.send_queue()
        calls = self.service.send_with_cooldown.await_args_list
        self.assertEqual([call.args[0] for call in calls], [self.clients[0], self.clients[1], self.clients[0]])
        self.assertEqual([call.args[1].id for call in calls], [100,101,102])
        self.assertIn('sent: 3', self.c.queue.summary())

    async def test_stop_during_delivery_keeps_next_recipient_pending(self):
        items = []
        for i in range(2):
            item = parse_line(f'@person{i} Cat #{i+1} 425 gram ru')
            item['owner_id'] = 100+i
            items.append(item)
        self.c.queue.add(items)
        entered = asyncio.Event()
        async def blocked(*args):
            entered.set()
            await asyncio.Event().wait()
        self.service.send_with_cooldown.side_effect = blocked
        self.service.task = asyncio.create_task(self.c.send_queue())
        await entered.wait()
        await self.c.stop()
        self.assertIn('uncertain: 1', self.c.queue.summary())
        self.assertEqual(self.c.queue.next()[1], 'person1')

    async def test_ambiguous_result_not_retried(self):
        await self.message('@person1 Scared Cat #16951 425 gram')
        await self.message('/enqueue')
        self.service.send_with_cooldown.side_effect = TimeoutError()
        await self.c.send_queue()
        await self.c.send_queue()
        self.service.send_with_cooldown.assert_awaited_once()
        self.assertIn('uncertain: 1', self.c.queue.summary())


if __name__ == '__main__': unittest.main()
