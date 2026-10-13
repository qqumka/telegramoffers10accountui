import re
import sqlite3


def command(title, number, price):
    title = ' '.join(str(title or '').split())
    if not title or not re.fullmatch(r'[1-9][0-9]*', str(number)):
        raise ValueError('Нет названия или номера подарка')
    match = re.fullmatch(r'([1-9][0-9]*) XTR', price)
    if not match:
        raise ValueError('Лот должен иметь только цену в Stars')
    amount = int(match[1]) * 95 // 100
    if amount < 1:
        raise ValueError('Цена предложения меньше одной звезды')
    return f'jdcsnjdc {title} #{number} {amount} stars'


class OfferStore:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS offers (slug TEXT PRIMARY KEY, recipient TEXT, account INTEGER, command TEXT, status TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value INTEGER)')
        self.db.execute('CREATE TABLE IF NOT EXISTS owners (owner_key TEXT PRIMARY KEY, recipient TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS baseline_gifts (slug TEXT PRIMARY KEY)')
        if not self.db.execute("SELECT 1 FROM state WHERE key='owners_migrated'").fetchone():
            self.db.execute("INSERT OR IGNORE INTO owners SELECT 'name:' || lower(ltrim(recipient, '@')), lower(ltrim(recipient, '@')) FROM offers")
            self.db.execute("INSERT INTO state VALUES ('owners_migrated', 1)")
        self.db.commit()

    def known_slugs(self):
        return {row[0] for row in self.db.execute('SELECT slug FROM offers UNION SELECT slug FROM baseline_gifts')}

    def remember_baseline(self, slugs):
        with self.db:
            self.db.executemany('INSERT OR IGNORE INTO baseline_gifts VALUES (?)', ((slug,) for slug in slugs))

    def seen_owner(self, owner_id, recipient=None):
        key = f'id:{owner_id}' if owner_id is not None else None
        if key and self.db.execute('SELECT 1 FROM owners WHERE owner_key=?', (key,)).fetchone():
            return True
        if recipient:
            legacy = 'name:' + recipient.lstrip('@').lower()
            return bool(self.db.execute('SELECT 1 FROM owners WHERE owner_key=?', (legacy,)).fetchone())
        return False

    def reserve(self, slug, recipient, slots, text, owner_id=None):
        if not slots:
            raise ValueError('Нет подключённых аккаунтов')
        if self.db.execute('SELECT 1 FROM offers WHERE slug=?', (slug,)).fetchone():
            return None
        if self.seen_owner(owner_id, recipient):
            # Upgrade legacy username history when a stable Telegram ID becomes known.
            if owner_id is not None:
                with self.db:
                    self.db.execute('INSERT OR IGNORE INTO owners VALUES (?,?)', (f'id:{owner_id}', recipient.lower()))
            return None
        row = self.db.execute("SELECT value FROM state WHERE key='cursor'").fetchone()
        cursor = row[0] if row else 0
        account = slots[cursor % len(slots)]
        with self.db:
            self.db.execute('INSERT INTO offers VALUES (?,?,?,?,?)', (slug, recipient, account, text, 'pending'))
            key = f'id:{owner_id}' if owner_id is not None else 'name:' + recipient.lstrip('@').lower()
            self.db.execute('INSERT INTO owners VALUES (?,?)', (key, recipient.lower()))
            self.db.execute("INSERT OR REPLACE INTO state VALUES ('cursor',?)", (cursor + 1,))
        return account

    def status(self, slug, status):
        with self.db:
            self.db.execute('UPDATE offers SET status=? WHERE slug=?', (status, slug))
