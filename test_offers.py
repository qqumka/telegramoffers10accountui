import asyncio
from pathlib import Path
import tempfile
import queue
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from offers import OfferStore, command
import desktop
from telethon.tl.types import StarsAmount, StarsTonAmount


class Commands(unittest.TestCase):
    def test_collection_selection_and_settings_persistence(self):
        catalog = [{'id': 1, 'title': 'Mirage Lamp'}, {'id': 2, 'title': 'Resistance Dog'}, {'id': 3, 'title': 'Toy Bear'}]
        self.assertEqual(desktop.selected_catalog(catalog, {'all': True, 'names': []}), [1, 2, 3])
        selection = {'all': False, 'names': ['Mirage Lamp']}
        self.assertEqual(desktop.selected_catalog(catalog, selection), [1])
        self.assertEqual(desktop.selected_catalog(catalog, {'all': False, 'names': []}), [])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'gifts.json'
            desktop.write_settings(path, selection)
            self.assertEqual(desktop.read_settings(path, {}), selection)
        self.assertTrue({'Mirage Lamp', 'Resistance Dog'} <= desktop.watcher.GIFT_NAME_WHITELIST)

    def test_owner_memory_survives_restart_and_username_change(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'offers.sqlite'
            store = OfferStore(path)
            self.assertEqual(store.reserve('gift1', 'seller', [0, 1], 'text', owner_id=123), 0)
            self.assertIsNone(store.reserve('gift2', 'seller', [0, 1], 'text', owner_id=123))
            store.db.close()
            store = OfferStore(path)
            self.assertIsNone(store.reserve('gift3', 'renamed', [0, 1], 'text', owner_id=123))
            self.assertEqual(store.reserve('gift4', 'other', [0, 1], 'text', owner_id=456), 1)
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM offers').fetchone()[0], 2)
            store.db.close()

    def test_legacy_history_is_migrated(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'offers.sqlite'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE offers (slug TEXT PRIMARY KEY, recipient TEXT, account INTEGER, command TEXT, status TEXT)')
                db.execute("INSERT INTO offers VALUES ('oldgift', 'Seller', 0, 'old-command', 'sent')")
            store = OfferStore(path)
            self.assertIsNone(store.reserve('newgift', 'seller', [0], 'text', owner_id=123))
            self.assertTrue(store.seen_owner(123))
            store.db.close()

    def test_empty_account_list_does_not_remember_owner(self):
        with tempfile.TemporaryDirectory() as folder:
            store = OfferStore(Path(folder) / 'offers.sqlite')
            with self.assertRaises(ValueError):
                store.reserve('gift', 'seller', [], 'text', owner_id=123)
            self.assertFalse(store.seen_owner(123, 'seller'))
            store.db.close()

    def test_discount_and_currency(self):
        self.assertEqual(command('Mirage Lamp', 333, '100 XTR'), 'jdcsnjdc Mirage Lamp #333 95 stars ru')
        self.assertIn(' 95 stars ru', command('Mirage Lamp', 333, '101 XTR'))
        self.assertEqual(command('Mirage Lamp', 333, '400 XTR'), 'jdcsnjdc Mirage Lamp #333 380 stars ru')
        for price in ('1.0000 TON', '—', '0 XTR', '10 XTR / 20 XTR', '1.0000 TON / 200 XTR'):
            with self.subTest(price=price), self.assertRaises(ValueError):
                command('Mirage Lamp', 333, price)

    def test_round_robin_persistence_and_duplicates(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'offers.sqlite'
            store = OfferStore(path)
            self.assertEqual([store.reserve(str(i), f'owner{i}', [0, 2], 'text') for i in range(3)], [0, 2, 0])
            self.assertIsNone(store.reserve('0', 'owner', [0, 2], 'text'))
            store.db.close()
            store = OfferStore(path)
            self.assertEqual(store.reserve('next', 'nextowner', [0, 2], 'text'), 2)
            store.db.close()


class Integration(unittest.IsolatedAsyncioTestCase):
    async def test_scan_uses_only_selected_collection(self):
        app = desktop.App.__new__(desktop.App)
        client = SimpleNamespace(is_user_authorized=AsyncMock(return_value=True))
        app.clients, app.paused, app.log, app.logs = {0: client}, False, lambda text: None, queue.Queue()
        catalog = [{'id': 1, 'title': 'Mirage Lamp'}, {'id': 2, 'title': 'Resistance Dog'}]
        async def sleep(delay):
            if delay == desktop.watcher.POLL_INTERVAL:
                raise asyncio.CancelledError()
        with tempfile.TemporaryDirectory() as folder, patch.object(desktop, 'DATA', Path(folder)), patch.object(desktop.watcher, 'get_gift_catalog', AsyncMock(return_value=catalog)), patch.object(desktop.watcher, 'check_gift', AsyncMock()) as scan, patch.object(desktop.asyncio, 'sleep', sleep):
            with self.assertRaises(asyncio.CancelledError):
                await app.scan(False, {'all': False, 'names': ['Mirage Lamp']})
            self.assertEqual(scan.await_count, 1)
            self.assertEqual(scan.call_args.args[1], 1)

    async def test_peerflood_marks_account_and_stops_sending(self):
        app = desktop.App.__new__(desktop.App)
        first = SimpleNamespace(is_user_authorized=AsyncMock(return_value=True), send_message=AsyncMock(side_effect=desktop.PeerFloodError(request=None)))
        other = SimpleNamespace(is_user_authorized=AsyncMock(return_value=True), send_message=AsyncMock())
        app.clients, app.paused, app.log, app.logs = {0: first, 1: other}, False, lambda text: None, queue.Queue()
        passes = 0
        async def found(client, gift_id, seen, listings, stats):
            if passes:
                for i in range(2):
                    listings.append(dict(slug=f'Gift-{i}', title='Mirage Lamp', num=333, price='400 XTR', owner_id=SimpleNamespace(user_id=123+i), owner_name='Owner'))
        async def sleep(delay):
            nonlocal passes
            if delay == desktop.watcher.POLL_INTERVAL:
                passes += 1
                if passes > 1:
                    raise AssertionError('PeerFlood must stop the scan')
        info = {123+i: dict(username=f'owner{i}', paid=False, level=0) for i in range(2)}
        with tempfile.TemporaryDirectory() as folder, patch.object(desktop, 'DATA', Path(folder)), patch.object(desktop.watcher, 'get_gift_ids_by_names', AsyncMock(return_value=[1])), patch.object(desktop.watcher, 'check_gift', found), patch.object(desktop.watcher, 'resolve_owner_info', AsyncMock(return_value=info)), patch.object(desktop.asyncio, 'sleep', sleep):
            await app.scan(True)
            first.send_message.assert_awaited_once()
            other.send_message.assert_not_awaited()
            self.assertEqual(app.restricted_accounts, {0})
            self.assertEqual(app.logs.get_nowait(), ('status', (0, '● PeerFloodError')))
            self.assertEqual(desktop.read_settings(Path(folder)/'account_limits.json', []), [0])

    async def test_peerflood_during_scanning_marks_correct_slot(self):
        app = desktop.App.__new__(desktop.App)
        app.clients = {i: SimpleNamespace(is_user_authorized=AsyncMock(return_value=True)) for i in range(2)}
        app.paused, app.log, app.logs = False, lambda text: None, queue.Queue()
        async def found(client, gift_id, seen, listings, stats):
            if gift_id == 2:
                raise desktop.PeerFloodError(request=None)
        with tempfile.TemporaryDirectory() as folder, patch.object(desktop, 'DATA', Path(folder)), patch.object(desktop.watcher, 'get_gift_ids_by_names', AsyncMock(return_value=[1, 2])), patch.object(desktop.watcher, 'check_gift', found), patch.object(desktop.asyncio, 'sleep', AsyncMock()):
            await app.scan(False)
            self.assertEqual(app.restricted_accounts, {1})

    async def test_parser_requests_and_accepts_only_stars(self):
        prices = [
            [StarsAmount(400, 0)],
            [StarsTonAmount(1000000000)],
            [StarsAmount(400, 0), StarsTonAmount(1000000000)],
            [StarsAmount(400, 1)],
            [],
            [SimpleNamespace(amount=400)],
        ]
        gifts = [SimpleNamespace(slug=f'Gift-{i}', title='Mirage Lamp', num=333, resell_amount=price) for i, price in enumerate(prices)]
        client = AsyncMock(return_value=SimpleNamespace(gifts=gifts))
        listings, seen = [], set()
        stats = dict(total_seen_in_responses=0, new_slugs=0, errors=0, filtered_by_name=0, filtered_names_sample=[])
        await desktop.watcher.check_gift(client, 1, seen, listings, stats)
        self.assertTrue(client.call_args.args[0].stars_only)
        self.assertEqual([gift['slug'] for gift in listings], ['Gift-0', 'Gift-2'])
        self.assertEqual(listings[0]['price'], '400 XTR')
        self.assertEqual(command(listings[0]['title'], listings[0]['num'], listings[0]['price']), 'jdcsnjdc Mirage Lamp #333 380 stars ru')
        self.assertNotIn('Gift-1', seen)
        self.assertEqual(listings[1]['price'], '400 XTR')
        self.assertEqual(stats['stars_listings'], 2)
        self.assertEqual(stats['filtered_currency'], 4)
        known_stats = dict(total_seen_in_responses=0, new_slugs=0, errors=0, filtered_by_name=0, filtered_names_sample=[])
        repeated = []
        await desktop.watcher.check_gift(client, 1, seen, repeated, known_stats)
        self.assertEqual(repeated, [])
        self.assertEqual(known_stats['stars_listings'], 2)
        self.assertEqual(known_stats['known_gifts'], 2)

    async def test_ton_only_flag_overrides_advertised_stars_price(self):
        gift = SimpleNamespace(slug='Gift-1', resale_ton_only=True, resell_amount=[StarsAmount(400, 0), StarsTonAmount(1000000000)])
        client = AsyncMock(return_value=SimpleNamespace(gifts=[gift]))
        listings = []
        stats = dict(total_seen_in_responses=0, new_slugs=0, errors=0)
        await desktop.watcher.check_gift(client, 1, set(), listings, stats)
        self.assertEqual(listings, [])
        self.assertEqual(stats['filtered_currency'], 1)

    async def test_logout_removes_only_selected_account_after_confirmation(self):
        for success in (True, False):
            with self.subTest(success=success):
                app = desktop.App.__new__(desktop.App)
                client = SimpleNamespace(log_out=AsyncMock(return_value=success))
                other = object()
                app.clients, app.hashes = {0: client, 1: other}, {0: ('phone', 'code')}
                app.connecting, app.logs, app.task = set(), queue.Queue(), None
                app.log = lambda text: None
                await app.logout_account(0)
                client.log_out.assert_awaited_once()
                self.assertIs(app.clients[1], other)
                self.assertEqual(0 in app.clients, not success)
                self.assertEqual(0 in app.hashes, not success)
                self.assertFalse(app.connecting)
                if success:
                    self.assertEqual(app.logs.get_nowait(), ('signed_out', 0))

    async def test_first_full_pass_remembers_gifts_without_sending(self):
        for initial_error in (False, True):
            with self.subTest(initial_error=initial_error):
                app = desktop.App.__new__(desktop.App)
                client = SimpleNamespace(is_user_authorized=AsyncMock(return_value=True), send_message=AsyncMock())
                app.clients, app.paused, app.log = {0: client}, False, lambda text: None
                passes = 0
                final_pass = 3 if initial_error else 2
                async def found(client, gift_id, seen, listings, stats):
                    stats['errors'] = int(initial_error and passes == 0)
                    for i in range(passes + 1):
                        slug = f'Gift-{i}'
                        if slug not in seen:
                            seen.add(slug)
                            listings.append(dict(slug=slug, price='100 XTR', title='Mirage Lamp', num=333, owner_id=SimpleNamespace(user_id=100 + i), owner_name='Owner'))
                async def sleep(delay):
                    nonlocal passes
                    if delay == desktop.watcher.POLL_INTERVAL:
                        passes += 1
                        if passes < final_pass:
                            client.send_message.assert_not_awaited()
                        else:
                            raise asyncio.CancelledError()
                info = {100 + i: dict(username=f'owner{i}', paid=False, level=0) for i in range(3)}
                with tempfile.TemporaryDirectory() as folder, patch.object(desktop, 'DATA', Path(folder)), patch.object(desktop.watcher, 'get_gift_ids_by_names', AsyncMock(return_value=[1])), patch.object(desktop.watcher, 'check_gift', found), patch.object(desktop.watcher, 'resolve_owner_info', AsyncMock(return_value=info)) as resolve, patch.object(desktop.asyncio, 'sleep', sleep):
                    with self.assertRaises(asyncio.CancelledError):
                        await app.scan(True)
                    i = final_pass - 1
                    client.send_message.assert_awaited_once_with(f'owner{i}', 'jdcsnjdc Mirage Lamp #333 95 stars ru', parse_mode=None)
                    self.assertEqual(resolve.await_count, 1)
                    store = OfferStore(Path(folder) / 'offers.sqlite')
                    self.assertEqual(store.db.execute('SELECT COUNT(*) FROM baseline_gifts').fetchone()[0], final_pass - 1)
                    self.assertEqual(store.db.execute('SELECT account FROM offers').fetchone()[0], 0)
                    self.assertFalse(store.seen_owner(100, 'owner0'))
                    self.assertIn('Gift-0', store.known_slugs())
                    store.db.close()

    async def test_transient_owner_failures_are_retried(self):
        for initial_info in ({}, {123: dict(username='owner', paid=None, level=0)}):
            with self.subTest(initial_info=initial_info):
                app = desktop.App.__new__(desktop.App)
                client = SimpleNamespace(is_user_authorized=AsyncMock(return_value=True), send_message=AsyncMock())
                app.clients, app.paused, app.log = {0: client}, False, lambda text: None
                passes = 0
                fetches = 0
                async def found(client, gift_id, seen, listings, stats):
                    nonlocal fetches
                    fetches += 1
                    if fetches == 1:
                        return
                    if 'Retry-1' not in seen:
                        seen.add('Retry-1')
                        listings.append(dict(slug='Retry-1', price='100 XTR', title='Mirage Lamp', num=333, owner_id=SimpleNamespace(user_id=123), owner_name='Owner'))
                async def sleep(delay):
                    nonlocal passes
                    if delay == desktop.watcher.POLL_INTERVAL:
                        passes += 1
                        if passes == 3:
                            raise asyncio.CancelledError()
                final_info = {123: dict(username='owner', paid=False, level=0)}
                with tempfile.TemporaryDirectory() as folder, patch.object(desktop, 'DATA', Path(folder)), patch.object(desktop.watcher, 'get_gift_ids_by_names', AsyncMock(return_value=[1])), patch.object(desktop.watcher, 'check_gift', found), patch.object(desktop.watcher, 'resolve_owner_info', AsyncMock(side_effect=[initial_info, final_info])) as resolve, patch.object(desktop.asyncio, 'sleep', sleep):
                    with self.assertRaises(asyncio.CancelledError):
                        await app.scan(True)
                    self.assertEqual(resolve.await_count, 2)
                    client.send_message.assert_awaited_once_with('owner', 'jdcsnjdc Mirage Lamp #333 95 stars ru', parse_mode=None)

    async def test_floodwait_during_scanning_stops_all_accounts(self):
        app = desktop.App.__new__(desktop.App)
        clients = {i: SimpleNamespace(is_user_authorized=AsyncMock(return_value=True), send_message=AsyncMock()) for i in range(2)}
        messages = []
        app.clients, app.paused, app.log = clients, False, messages.append
        async def limited(request):
            raise desktop.FloodWaitError(request=request, capture=30)
        client = AsyncMock(side_effect=limited)
        with self.assertRaises(desktop.FloodWaitError):
            await desktop.watcher.check_gift(client, 1, set(), [], {'errors': 0})
        with tempfile.TemporaryDirectory() as folder, patch.object(desktop, 'DATA', Path(folder)), patch.object(desktop.watcher, 'get_gift_ids_by_names', AsyncMock(return_value=[1])), patch.object(desktop.watcher, 'check_gift', AsyncMock(side_effect=desktop.FloodWaitError(request=None, capture=30))):
            await app.scan(True)
        for client in clients.values():
            client.send_message.assert_not_awaited()
        self.assertTrue(any('30 сек' in message for message in messages))

    async def test_changed_api_credentials_replace_cached_client(self):
        app = desktop.App.__new__(desktop.App)
        old = SimpleNamespace(api_id=1, api_hash='old', disconnect=AsyncMock())
        new = SimpleNamespace(connect=AsyncMock(), is_user_authorized=AsyncMock(return_value=False),
                              send_code_request=AsyncMock(return_value=SimpleNamespace(phone_code_hash='new-code')))
        app.clients, app.hashes, app.connecting = {0: old}, {0: ('old-phone', 'old-code')}, set()
        app.logs = queue.Queue()
        app.log = lambda text: None
        with patch.object(desktop, 'TelegramClient', return_value=new) as factory:
            await app.connect_account(0, 2, 'new', '+10000000000')
        old.disconnect.assert_awaited_once()
        self.assertIs(app.clients[0], new)
        self.assertEqual(factory.call_args.args[1:], (2, 'new'))
        new.send_code_request.assert_awaited_once_with('+10000000000')
        self.assertEqual(app.hashes[0], ('+10000000000', 'new-code'))
        self.assertFalse(app.connecting)

    async def check_mode(self, live, same_owner=False):
        app = desktop.App.__new__(desktop.App)
        clients = {i: SimpleNamespace(is_user_authorized=AsyncMock(return_value=True), send_message=AsyncMock()) for i in range(2)}
        app.clients, app.paused, app.log = clients, False, lambda text: None
        fetches = 0
        passes = 0
        async def found(client, gift_id, seen, listings, stats):
            nonlocal fetches
            fetches += 1
            if fetches == 1:
                return
            for i in range(3):
                listings.append(dict(slug=f'Gift-{i}', price='100 XTR', title='Mirage Lamp', num=333, owner_id=SimpleNamespace(user_id=0 if same_owner else i), owner_name='Owner'))
        async def sleep(delay):
            nonlocal passes
            if delay == desktop.watcher.POLL_INTERVAL:
                passes += 1
                if passes == 2:
                    raise asyncio.CancelledError()
        info = {i: dict(username=f'owner{i}', paid=False, level=0) for i in range(3)}
        with tempfile.TemporaryDirectory() as folder, patch.object(desktop, 'DATA', Path(folder)), patch.object(desktop.watcher, 'get_gift_ids_by_names', AsyncMock(return_value=[1])), patch.object(desktop.watcher, 'check_gift', found), patch.object(desktop.watcher, 'resolve_owner_info', AsyncMock(return_value=info)), patch.object(desktop.asyncio, 'sleep', sleep):
            with self.assertRaises(asyncio.CancelledError):
                await app.scan(live)
            if live:
                self.assertEqual(clients[0].send_message.await_count, 1 if same_owner else 2)
                self.assertEqual(clients[1].send_message.await_count, 0 if same_owner else 1)
                if not same_owner:
                    self.assertEqual(clients[1].send_message.call_args.args, ('owner1', 'jdcsnjdc Mirage Lamp #333 95 stars ru'))
            else:
                for client in clients.values():
                    client.send_message.assert_not_awaited()

    async def test_preview(self):
        await self.check_mode(False)

    async def test_send(self):
        await self.check_mode(True)

    async def test_multiple_gifts_of_one_owner_send_only_once(self):
        await self.check_mode(True, same_owner=True)


if __name__ == '__main__':
    unittest.main()
