"""Desktop tracker and round-robin offer sender. No group or bot required."""
import asyncio
from datetime import datetime
import os
from pathlib import Path
import queue
import re
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from telethon import TelegramClient
from telethon.errors import FloodWaitError, SessionPasswordNeededError
import watcher_core as watcher
from offers import OfferStore, command

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
DATA.mkdir(exist_ok=True)


class App:
    def __init__(self, root):
        self.root = root
        root.title('Telegram Gifts — 10 аккаунтов')
        root.geometry('1050x700')
        self.logs = queue.Queue()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.clients = {}
        self.hashes = {}
        self.task = None
        self.paused = False
        self.api_id = tk.StringVar(value=os.getenv('TG_API_ID', ''))
        self.api_hash = tk.StringVar(value=os.getenv('TG_API_HASH', ''))
        header = ttk.Frame(root, padding=10)
        header.pack(fill='x')
        ttk.Label(header, text='API ID').pack(side='left')
        ttk.Entry(header, textvariable=self.api_id, width=12).pack(side='left')
        ttk.Label(header, text='API Hash').pack(side='left', padx=10)
        ttk.Entry(header, textvariable=self.api_hash, show='*', width=36).pack(side='left')
        self.rows = []
        panel = ttk.Frame(root, padding=10)
        panel.pack(fill='x')
        for i in range(10):
            phone, code, password = tk.StringVar(), tk.StringVar(), tk.StringVar()
            status = tk.StringVar(value='Не подключён')
            self.rows.append((phone, code, password, status))
            ttk.Label(panel, text=f'Аккаунт {i+1}').grid(row=i, column=0, padx=5)
            ttk.Entry(panel, textvariable=phone, width=18).grid(row=i, column=1)
            ttk.Button(panel, text='Получить код / сессия', command=lambda n=i: self.connect(n)).grid(row=i, column=2)
            ttk.Entry(panel, textvariable=code, width=10).grid(row=i, column=3)
            ttk.Entry(panel, textvariable=password, show='*', width=15).grid(row=i, column=4)
            ttk.Button(panel, text='Войти', command=lambda n=i: self.login(n)).grid(row=i, column=5)
            ttk.Label(panel, textvariable=status, width=25).grid(row=i, column=6)
        ttk.Label(panel, text='Поля: телефон • код Telegram • пароль 2FA (если нужен)').grid(row=10, column=0, columnspan=7, pady=8)
        controls = ttk.Frame(root, padding=10)
        controls.pack(fill='x')
        self.live = tk.BooleanVar(value=False)
        ttk.Checkbutton(controls, text='Отправлять владельцам (выключено = предпросмотр)', variable=self.live).pack(side='left')
        ttk.Button(controls, text='Запустить', command=self.start).pack(side='left', padx=10)
        ttk.Button(controls, text='Пауза / продолжить', command=lambda: self.loop.call_soon_threadsafe(self.pause)).pack(side='left')
        ttk.Button(controls, text='Остановить', command=lambda: self.loop.call_soon_threadsafe(self.stop)).pack(side='left')
        journal = ttk.LabelFrame(root, text='Журнал • время / статус / аккаунт / получатель', padding=8)
        journal.pack(fill='both', expand=True, padx=10, pady=10)
        toolbar = ttk.Frame(journal)
        toolbar.pack(fill='x', pady=(0, 6))
        self.follow = tk.BooleanVar(value=True)
        ttk.Checkbutton(toolbar, text='Следить за новыми событиями', variable=self.follow).pack(side='left')
        ttk.Label(toolbar, text='Зелёный — отправлено • голубой — предложение • красный — ошибка').pack(side='left', padx=12)
        ttk.Button(toolbar, text='Очистить окно', command=self.clear_log).pack(side='right')
        body = ttk.Frame(journal)
        body.pack(fill='both', expand=True)
        self.output = tk.Text(body, wrap='word', state='disabled', background='#111827',
                              foreground='#d1d5db', selectbackground='#334155',
                              font=('Menlo', 12), relief='flat', padx=12, pady=10,
                              spacing1=4, spacing3=6)
        scroll = ttk.Scrollbar(body, orient='vertical', command=self.output.yview)
        self.output.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.output.pack(side='left', fill='both', expand=True)
        for tag, color in {'info': '#d1d5db', 'offer': '#7dd3fc', 'success': '#86efac',
                           'warning': '#fcd34d', 'error': '#fca5a5'}.items():
            self.output.tag_configure(tag, foreground=color)
        root.after(100, self.drain)
        root.protocol('WM_DELETE_WINDOW', self.close)

    def log(self, text):
        level = 'info'
        badge = 'ИНФО'
        if text.startswith('Отправлено'):
            level, badge = 'success', 'ОТПРАВЛЕНО'
        elif text.startswith(('Ошибка', 'Не отправлено', 'Сканирование остановлено:')):
            level, badge = 'error', 'ОШИБКА'
        elif text.startswith(('Пропуск', 'Telegram ограничил', 'Пауза')):
            level, badge = 'warning', 'ВНИМАНИЕ'
        elif text.startswith('Аккаунт') and '→' in text:
            level, badge = 'offer', 'ПРЕДЛОЖЕНИЕ'
        line = f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}  [{badge}]  {text}'
        self.logs.put(('log', (level, line)))
        with (DATA / 'activity.log').open('a', encoding='utf-8') as f:
            f.write(line + '\n')

    def clear_log(self):
        self.output.configure(state='normal')
        self.output.delete('1.0', 'end')
        self.output.configure(state='disabled')

    def drain(self):
        while not self.logs.empty():
            kind, payload = self.logs.get()
            if kind == 'status':
                i, text = payload
                self.rows[i][3].set(text)
                self.log(f'Аккаунт {i+1}: {text}')
            else:
                level, line = payload
                self.output.configure(state='normal')
                self.output.insert('end', line + '\n', level)
                if int(self.output.index('end-1c').split('.')[0]) > 5000:
                    self.output.delete('1.0', '501.0')
                if self.follow.get():
                    self.output.see('end')
                self.output.configure(state='disabled')
        self.root.after(100, self.drain)

    def submit(self, coro):
        async def guard():
            try:
                await coro
            except Exception as exc:
                self.log(f'Ошибка: {type(exc).__name__} (проверьте настройки/доступ)')
        asyncio.run_coroutine_threadsafe(guard(), self.loop)

    def connect(self, i):
        try:
            api_id = int(self.api_id.get())
            api_hash = self.api_hash.get().strip()
            if not api_hash:
                raise ValueError()
        except ValueError:
            messagebox.showerror('Настройки', 'Введите API ID и API Hash с my.telegram.org')
            return
        phone = self.rows[i][0].get().strip()
        async def run():
            if self.task and not self.task.done():
                self.log('Остановите сканирование перед подключением аккаунтов')
                return
            client = self.clients.get(i)
            if client is None:
                client = TelegramClient(str(DATA / f'account_{i+1}'), api_id, api_hash)
                self.clients[i] = client
            await client.connect()
            if await client.is_user_authorized():
                self.logs.put(('status', (i, 'Подключён')))
            else:
                response = await client.send_code_request(phone)
                self.hashes[i] = (phone, response.phone_code_hash)
                self.logs.put(('status', (i, 'Введите код')))
        self.submit(run())

    def login(self, i):
        code, password = self.rows[i][1].get().strip(), self.rows[i][2].get()
        async def run():
            client = self.clients[i]
            if await client.is_user_authorized():
                return
            phone, code_hash = self.hashes[i]
            try:
                await client.sign_in(phone, code, phone_code_hash=code_hash)
            except SessionPasswordNeededError:
                if not password:
                    self.logs.put(('status', (i, 'Нужен пароль 2FA')))
                    return
                await client.sign_in(password=password)
            self.logs.put(('status', (i, 'Подключён')))
        self.submit(run())

    def start(self):
        live = self.live.get()
        if live and not messagebox.askyesno('Отправка', 'Отправлять команды владельцам подарков от подключённых аккаунтов? Убедитесь, что получатели согласны получать сообщения.'):
            return
        async def run():
            if self.task and not self.task.done():
                self.log('Уже запущено; для изменения режима остановите и запустите снова')
                return
            self.paused = False
            self.task = asyncio.create_task(self.scan(live))
            self.task.add_done_callback(self.finished)
        self.submit(run())

    def finished(self, task):
        if task.cancelled():
            self.log('Остановлено')
        elif task.exception():
            self.log(f'Сканирование остановлено: {type(task.exception()).__name__}')

    def pause(self):
        self.paused = not self.paused
        self.log('Пауза' if self.paused else 'Продолжение')

    def stop(self):
        if self.task:
            self.task.cancel()

    async def scan(self, live):
        clients = {i: c for i, c in sorted(self.clients.items()) if await c.is_user_authorized()}
        if not clients:
            self.log('Сначала подключите аккаунт')
            return
        primary = next(iter(clients.values()))
        store = OfferStore(DATA / ('offers.sqlite' if live else 'preview.sqlite'))
        seen = {row[0] for row in store.db.execute('SELECT slug FROM offers')}
        self.log('Режим: ' + ('ОТПРАВКА' if live else 'ПРЕДПРОСМОТР — сообщения не отправляются'))
        try:
            if watcher.GIFT_NAME_WHITELIST:
                ids = await watcher.get_gift_ids_by_names(primary, watcher.GIFT_NAME_WHITELIST)
            elif watcher.MANUAL_GIFT_IDS:
                ids = watcher.MANUAL_GIFT_IDS
            elif watcher.TRACK_ALL_COLLECTIONS:
                ids = await watcher.get_all_gift_ids(primary)
            else:
                ids = await watcher.get_newest_gift_ids(primary, watcher.NUM_NEWEST_COLLECTIONS)
            self.log(f'Коллекций: {len(ids)}; аккаунтов: {len(clients)}')
            while True:
                while self.paused:
                    await asyncio.sleep(0.2)
                listings = []
                stats = dict(total_seen_in_responses=0, new_slugs=0, filtered_by_name=0, filtered_names_sample=[], errors=0)
                for index, gift_id in enumerate(ids):
                    while self.paused:
                        await asyncio.sleep(0.2)
                    client = list(clients.values())[index % len(clients)]
                    await watcher.check_gift(client, gift_id, seen, listings, stats)
                    await asyncio.sleep(watcher.REQUEST_DELAY)
                info = await watcher.resolve_owner_info(primary, listings) if listings else {}
                for listing in listings:
                    while self.paused:
                        await asyncio.sleep(0.2)
                    uid = getattr(listing['owner_id'], 'user_id', None)
                    details = info.get(uid, {})
                    recipient = details.get('username')
                    if not recipient or (watcher.EXCLUDE_PAID_MESSAGE_SELLERS and details.get('paid')):
                        continue
                    if not watcher.looks_cjk_anywhere(listing['owner_name'], details.get('about', '')):
                        level = details.get('level')
                        if level is not None and level > watcher.MAX_SELLER_LEVEL:
                            continue
                    try:
                        text = command(listing['slug'], listing['price'])
                    except ValueError:
                        self.log(f"Пропуск {listing['slug']}: нет подходящей цены Stars")
                        continue
                    original = int(re.search(r'(?:^| / )(\d+) XTR', listing['price']).group(1))
                    if watcher.MIN_PRICE_STARS and original < watcher.MIN_PRICE_STARS:
                        continue
                    if watcher.MAX_PRICE_STARS and original > watcher.MAX_PRICE_STARS:
                        continue
                    slot = store.reserve(listing['slug'], recipient, list(clients), text)
                    if slot is None:
                        continue
                    self.log(f'Аккаунт {slot+1} → @{recipient}: {text}')
                    if not live:
                        store.status(listing['slug'], 'preview')
                        continue
                    try:
                        await clients[slot].send_message(recipient, text, parse_mode=None)
                    except FloodWaitError as exc:
                        store.status(listing['slug'], 'blocked')
                        self.log(f'Telegram ограничил аккаунт {slot+1} на {exc.seconds} сек. Отправка остановлена; другие аккаунты не используются для обхода.')
                        return
                    except Exception as exc:
                        store.status(listing['slug'], 'failed')
                        self.log(f'Не отправлено • аккаунт {slot+1} → @{recipient} • {listing["slug"]} • {type(exc).__name__}; автоматического повтора нет')
                    else:
                        store.status(listing['slug'], 'sent')
                        self.log(f'Отправлено • аккаунт {slot+1} → @{recipient} • {listing["slug"]}')
                    await asyncio.sleep(max(1, watcher.SEND_DELAY_SECONDS))
                self.log(f"Проход завершён: найдено {len(listings)}, ошибок запросов {stats['errors']}")
                await asyncio.sleep(watcher.POLL_INTERVAL)
        finally:
            store.db.close()

    def close(self):
        async def shutdown():
            self.stop()
            if self.task:
                await asyncio.gather(self.task, return_exceptions=True)
            await asyncio.gather(*(c.disconnect() for c in self.clients.values()), return_exceptions=True)
        future = asyncio.run_coroutine_threadsafe(shutdown(), self.loop)
        def poll():
            if future.done():
                self.loop.call_soon_threadsafe(self.loop.stop)
                self.root.destroy()
            else:
                self.root.after(100, poll)
        poll()


if __name__ == '__main__':
    window = tk.Tk()
    App(window)
    window.mainloop()
