"""Headless account backend: no tracker, catalogue, Qt or desktop imports."""
import asyncio
from datetime import datetime
import json
from pathlib import Path
import queue

from telethon import TelegramClient
from telethon.tl.types import User
from telethon.errors import FloodWaitError, PeerFloodError

DATA = Path(__file__).resolve().parent / 'data'
DATA.mkdir(exist_ok=True)


def read_settings(path, default):
    try: return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError): return default


def write_settings(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


class Backend:
    def __init__(self, threaded=False):
        self.clients, self.connecting = {}, set()
        self.task, self.paused = None, False
        self.logs = queue.Queue()
        limits = read_settings(DATA / 'account_limits.json', [])
        self.restricted_accounts = {i for i in limits if type(i) is int and 0 <= i < 10} if isinstance(limits, list) else set()
        self.peer_streaks = {}

    def log(self, text):
        line = f'{datetime.now():%Y-%m-%d %H:%M:%S} {text}'
        self.logs.put(('log', ('info', line)))
        with (DATA / 'activity.log').open('a', encoding='utf-8') as handle:
            handle.write(line+'\n')

    def finished(self, task):
        if not task.cancelled() and task.exception():
            self.log('Очередь остановлена: ' + type(task.exception()).__name__)

    def peer_failure(self, slot):
        count = self.peer_streaks[slot] = self.peer_streaks.get(slot, 0)+1
        if count >= 3:
            self.restricted_accounts.add(slot)
            write_settings(DATA / 'account_limits.json', sorted(self.restricted_accounts))
        self.log(f'Аккаунт {slot+1}: PeerFlood {count}/3' + ('; отключён' if count >= 3 else ''))

    def peer_success(self, slot):
        self.peer_streaks.pop(slot, None)

    async def send_with_cooldown(self, client, recipient, text):
        if not hasattr(self, '_send_lock'): self._send_lock = asyncio.Lock()
        async with self._send_lock:
            clock = getattr(self, 'send_clock', asyncio.get_running_loop().time)
            sleeper = getattr(self, 'send_sleep', asyncio.sleep)
            wait = max(0, getattr(self, '_next_send_at', 0)-clock())
            if wait: await sleeper(wait)
            try: return await client.send_message(recipient, text, parse_mode=None)
            finally: self._next_send_at = clock()+15
