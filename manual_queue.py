"""Durable manual offers; ambiguous sends are never retried automatically."""
import re
import sqlite3
import time
from decimal import Decimal
from offers import OfferStore


def language(name):
    if re.search('[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af]', name): return 'cn'
    if re.search('[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff\U0001ee00-\U0001eeff]', name): return 'ar'
    return 'ru'


def parse_line(line):
    if len(line) > 350: raise ValueError('Строка слишком длинная (максимум 350 символов).')
    match = re.fullmatch(r'@?([A-Za-z][A-Za-z0-9_]{3,31})\s+(?:jdcsnjdc\s+)?(.+?)\s+#([1-9][0-9]*)\s+([0-9]+(?:[.,][0-9]+)?)\s+gram(?:\s+(ru|cn|ar))?', line.strip())
    if not match: raise ValueError('Формат: @username Scared Cat #16951 425 gram [ru|cn|ar]')
    recipient, title, number, price, lang = match.groups()
    if Decimal(price.replace(',', '.')) <= 0: raise ValueError('Цена должна быть больше нуля')
    return dict(recipient=recipient.lower(), title=title, number=number, price=price.replace(',', '.'), lang=lang)


def parse_price(text):
    match = re.fullmatch(r'([0-9]+(?:[.,][0-9]+)?)\s+gram', text.strip(), re.IGNORECASE)
    if not match or len(text) > 100 or Decimal(match[1].replace(',', '.')) <= 0:
        raise ValueError('Укажите итоговую цену: 425 gram')
    return match[1].replace(',', '.')


def parse_results(text):
    """Parse copied Markdown or Telegram plain text; never follow embedded links."""
    lines = text.replace('**', '').splitlines()
    numbered = [(n, line.strip()) for n, line in enumerate(lines, 1)
                if re.match(r'^\s*[0-9]+\.\s', line)]
    if not numbered and not re.search(r'Результаты\s*\(', text): return None
    if not 1 <= len(numbered) <= 100:
        raise ValueError('В результатах должно быть от 1 до 100 строк получателей.')
    items = []
    for line_number, line in numbered:
        match = re.search(r'@([A-Za-z][A-Za-z0-9_]{3,31})\s*-\s*\[?(.+?)\s*\(#([1-9][0-9]*)\)', line)
        if not match:
            raise ValueError(f'Не удалось разобрать строку {line_number}. Список не загружен.')
        recipient, title, number = match.groups()
        title = title.strip()
        if not title or len(title) > 200:
            raise ValueError(f'Некорректное название NFT в строке {line_number}.')
        link = re.search(r'https://t\.me/nft/[^\s/)]+-([0-9]+)', line)
        if link and link[1] != number:
            raise ValueError(f'Номер NFT и ссылка различаются в строке {line_number}.')
        items.append(dict(recipient=recipient.lower(), title=title, number=number, price=None, lang=None))
    return items


def render(item):
    return f'jdcsnjdc {item["title"]} #{item["number"]} {item["price"]} gram {item["lang"]}'


class ManualQueue:
    def __init__(self, path):
        self.store = OfferStore(path)
        self.db = self.store.db
        self.db.execute('CREATE TABLE IF NOT EXISTS manual_queue (id INTEGER PRIMARY KEY, recipient TEXT, owner_id INTEGER UNIQUE, text TEXT, status TEXT, account INTEGER)')
        self.db.execute('CREATE TABLE IF NOT EXISTS manual_log (id INTEGER PRIMARY KEY, time REAL, message TEXT)')
        with self.db:
            interrupted = self.db.execute("UPDATE manual_queue SET status='uncertain' WHERE status='sending'").rowcount
            if interrupted:
                self.db.execute("INSERT OR REPLACE INTO state VALUES ('manual_next_send',?)", (time.time()+15,))

    def log(self, message):
        with self.db: self.db.execute('INSERT INTO manual_log(time,message) VALUES (?,?)', (time.time(), message))

    def add(self, items):
        added = 0
        with self.db:
            for item in items:
                if self.store.seen_owner(item['owner_id'], item['recipient']): continue
                if self.db.execute("SELECT 1 FROM owners WHERE lower(ltrim(recipient, '@'))=?", (item['recipient'],)).fetchone(): continue
                if self.db.execute('SELECT 1 FROM manual_queue WHERE owner_id=? OR recipient=?', (item['owner_id'], item['recipient'])).fetchone(): continue
                self.db.execute('INSERT INTO manual_queue(recipient,owner_id,text,status) VALUES (?,?,?,?)', (item['recipient'], item['owner_id'], render(item), 'pending'))
                added += 1
        self.log(f'Добавлено: {added}; дубликаты: {len(items)-added}')
        return added

    def next(self):
        return self.db.execute("SELECT id,recipient,owner_id,text FROM manual_queue WHERE status='pending' ORDER BY id LIMIT 1").fetchone()

    def claim(self, row, slots):
        ident, recipient, owner_id, text = row
        with self.db:
            cursor = self.db.execute("SELECT value FROM state WHERE key='manual_cursor'").fetchone()
            cursor = cursor[0] if cursor else 0
            slot = slots[cursor % len(slots)]
            self.db.execute("UPDATE manual_queue SET status='sending',account=? WHERE id=? AND status='pending'", (slot, ident))
            self.db.execute("INSERT OR REPLACE INTO state VALUES ('manual_cursor',?)", (cursor+1,))
            self.db.execute('INSERT OR IGNORE INTO owners VALUES (?,?)', (f'id:{owner_id}', recipient))
            self.db.execute('INSERT OR IGNORE INTO owners VALUES (?,?)', ('name:'+recipient, recipient))
            self.db.execute("INSERT OR REPLACE INTO state VALUES ('manual_next_send',?)", (time.time()+15,))
        return slot

    def finish(self, ident, status):
        with self.db:
            self.db.execute('UPDATE manual_queue SET status=? WHERE id=?', (status, ident))
            self.db.execute("INSERT OR REPLACE INTO state VALUES ('manual_next_send',?)", (time.time()+15,))

    def delay(self):
        row = self.db.execute("SELECT value FROM state WHERE key='manual_next_send'").fetchone()
        return max(0, row[0]-time.time()) if row else 0

    def summary(self):
        return ', '.join(f'{status}: {count}' for status,count in self.db.execute('SELECT status,count(*) FROM manual_queue GROUP BY status')) or 'пусто'
