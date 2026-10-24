from pathlib import Path
import tempfile
import sqlite3
import unittest
from offers import OfferStore


class LegacyHistory(unittest.TestCase):
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
