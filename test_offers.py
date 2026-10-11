import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from offers import OfferStore, command
import desktop


class Commands(unittest.TestCase):
    def test_discount_and_currency(self):
        self.assertEqual(command('Gift-123', '100 XTR'), '.buy "https://t.me/nft/Gift-123" "95" stars')
        self.assertIn('"95"', command('Gift-123', '101 XTR'))
        self.assertIn('"190"', command('Gift-123', '1.0000 TON / 200 XTR'))
        for price in ('1.0000 TON', '—', '0 XTR', '10 XTR / 20 XTR'):
            with self.subTest(price=price), self.assertRaises(ValueError):
                command('Gift-123', price)

    def test_round_robin_persistence_and_duplicates(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'offers.sqlite'
            store = OfferStore(path)
            self.assertEqual([store.reserve(str(i), 'owner', [0, 2], 'text') for i in range(3)], [0, 2, 0])
            self.assertIsNone(store.reserve('0', 'owner', [0, 2], 'text'))
            store.db.close()
            store = OfferStore(path)
            self.assertEqual(store.reserve('next', 'owner', [0, 2], 'text'), 2)
            store.db.close()


class Integration(unittest.IsolatedAsyncioTestCase):
    async def check_mode(self, live):
        app = desktop.App.__new__(desktop.App)
        clients = {i: SimpleNamespace(is_user_authorized=AsyncMock(return_value=True), send_message=AsyncMock()) for i in range(2)}
        app.clients, app.paused, app.log = clients, False, lambda text: None
        async def found(client, gift_id, seen, listings, stats):
            for i in range(3):
                listings.append(dict(slug=f'Gift-{i}', price='100 XTR', owner_id=SimpleNamespace(user_id=i), owner_name='Owner'))
        async def sleep(delay):
            if delay == desktop.watcher.POLL_INTERVAL:
                raise asyncio.CancelledError()
        info = {i: dict(username=f'owner{i}', paid=False, level=0) for i in range(3)}
        with tempfile.TemporaryDirectory() as folder, patch.object(desktop, 'DATA', Path(folder)), patch.object(desktop.watcher, 'get_gift_ids_by_names', AsyncMock(return_value=[1])), patch.object(desktop.watcher, 'check_gift', found), patch.object(desktop.watcher, 'resolve_owner_info', AsyncMock(return_value=info)), patch.object(desktop.asyncio, 'sleep', sleep):
            with self.assertRaises(asyncio.CancelledError):
                await app.scan(live)
            if live:
                self.assertEqual(clients[0].send_message.await_count, 2)
                self.assertEqual(clients[1].send_message.await_count, 1)
                self.assertEqual(clients[1].send_message.call_args.args, ('owner1', '.buy "https://t.me/nft/Gift-1" "95" stars'))
            else:
                for client in clients.values():
                    client.send_message.assert_not_awaited()

    async def test_preview(self):
        await self.check_mode(False)

    async def test_send(self):
        await self.check_mode(True)


if __name__ == '__main__':
    unittest.main()
