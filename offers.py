import re
import sqlite3


def command(slug, price):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', slug):
        raise ValueError('Некорректная ссылка подарка')
    matches = re.findall(r'(?:^| / )(\d+) XTR(?=$| / )', price)
    if len(matches) != 1 or int(matches[0]) <= 0:
        raise ValueError('Нет однозначной цены в Stars')
    amount = int(matches[0]) * 95 // 100
    if amount < 1:
        raise ValueError('Цена предложения меньше одной звезды')
    return f'.buy https://t.me/nft/{slug} {amount} stars'


class OfferStore:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS offers (slug TEXT PRIMARY KEY, recipient TEXT, account INTEGER, command TEXT, status TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value INTEGER)')
        self.db.commit()

    def reserve(self, slug, recipient, slots, text):
        if self.db.execute('SELECT 1 FROM offers WHERE slug=?', (slug,)).fetchone():
            return None
        row = self.db.execute("SELECT value FROM state WHERE key='cursor'").fetchone()
        cursor = row[0] if row else 0
        account = slots[cursor % len(slots)]
        with self.db:
            self.db.execute('INSERT INTO offers VALUES (?,?,?,?,?)', (slug, recipient, account, text, 'pending'))
            self.db.execute("INSERT OR REPLACE INTO state VALUES ('cursor',?)", (cursor + 1,))
        return account

    def status(self, slug, status):
        with self.db:
            self.db.execute('UPDATE offers SET status=? WHERE slug=?', (status, slug))
