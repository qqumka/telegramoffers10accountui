"""Desktop adapter for the same durable controller used by the Telegram bot."""
import asyncio
import copy
import queue
import threading
import time

import bot_core as core
from bot_runner import Controller, DEFAULT_ADMIN_ID
from manual_queue import parse_line, parse_results, parse_price, render
from telethon.errors import SessionPasswordNeededError


class DesktopAPI:
    def __init__(self, events): self.events = events
    async def call(self, method, **kwargs):
        if method == 'sendMessage': self.events.put(('notice', kwargs['text']))
        return {}


class Backend:
    def __init__(self):
        self.events = queue.Queue()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.ready = asyncio.run_coroutine_threadsafe(self.initialize(), self.loop)

    async def initialize(self):
        self.service = core.Backend()
        self.controller = Controller(DesktopAPI(self.events), self.service, DEFAULT_ADMIN_ID, 0, '')
        self.hashes, self.phones, self.auth_clients = {}, {}, {}
        self.account_status = {}
        self.previous = None
        self.operations = set()
        self.edit_lock = asyncio.Lock()
        self.emit_snapshot()
        self.pulse = asyncio.create_task(self.poll())

    async def poll(self):
        while True:
            while not self.service.logs.empty():
                kind, payload = self.service.logs.get_nowait()
                if kind == 'log': self.controller.queue.log(payload[1])
            self.emit_snapshot()
            await asyncio.sleep(0.4)

    def emit_snapshot(self):
        c = self.controller
        accounts = []
        for i in range(10):
            if i in self.service.clients or i in self.account_status or (core.DATA / f'account_{i+1}.session').exists():
                accounts.append(dict(slot=i, connected=i in self.service.clients,
                    status='Ограничен Telegram' if i in self.service.restricted_accounts else self.account_status.get(i, 'Сохранённая сессия')))
        data = dict(draft=copy.deepcopy(c.draft), pending=len(c.pending_import), running=c.running(), paused=self.service.paused,
            summary=c.queue.summary(), counts=dict(c.queue.db.execute('SELECT status,count(*) FROM manual_queue GROUP BY status')), accounts=accounts,
            queue=list(c.queue.db.execute('SELECT id,recipient,text,status,account FROM manual_queue ORDER BY id DESC LIMIT 500')),
            history=list(c.queue.db.execute("SELECT coalesce(max(CASE WHEN owner_key LIKE 'id:%' THEN owner_key END), max(owner_key)),recipient FROM owners GROUP BY recipient ORDER BY max(rowid) DESC LIMIT 500")),
            logs=list(reversed(list(c.queue.db.execute('SELECT time,message FROM manual_log ORDER BY id DESC LIMIT 250')))))
        if data != self.previous:
            self.previous = data
            self.events.put(('snapshot', data))

    def submit(self, action, *args):
        async def run():
            await asyncio.wrap_future(self.ready)
            task = asyncio.current_task()
            self.operations.add(task)
            try:
                if action in ('import', 'price', 'save') or (action == 'command' and args[0] == '/cancel'):
                    async with self.edit_lock: await getattr(self, 'do_'+action)(*args)
                else: await getattr(self, 'do_'+action)(*args)
            except Exception as exc:
                self.events.put(('error', str(exc) if isinstance(exc, ValueError) else type(exc).__name__))
            finally:
                self.operations.discard(task)
                self.emit_snapshot()
        return asyncio.run_coroutine_threadsafe(run(), self.loop)

    async def command(self, text):
        await self.controller.handle({'message': {'text': text, 'date': time.time(),
            'from': {'id': DEFAULT_ADMIN_ID}, 'chat': {'id': DEFAULT_ADMIN_ID, 'type': 'private'}}})

    async def do_command(self, text): await self.command(text)

    async def do_import(self, text, price):
        if self.controller.draft or self.controller.pending_import:
            raise ValueError('Сначала подтвердите или удалите текущий черновик.')
        amount = parse_price(price) if price.strip() else None
        results = parse_results(text)
        if results is not None:
            if price.strip():
                self.controller.draft = await self.controller.prepare([], parsed=[dict(item, price=amount) for item in results])
            else: self.controller.pending_import = results
        else:
            prepared = await self.controller.prepare(text.splitlines())
            self.controller.draft = prepared
            if price.strip():
                self.controller.draft = [dict(item, price=amount) for item in self.controller.draft]
        self.controller.save_draft()
        self.events.put(('notice', 'Список загружен. Проверьте каждую строку перед добавлением в очередь.'))

    async def do_price(self, text): await self.controller.apply_price(text)

    async def do_save(self, rows, enqueue=False):
        if len(rows) != len(self.controller.draft): raise ValueError('Черновик изменился. Обновите таблицу.')
        prepared = []
        for row, old in zip(rows, self.controller.draft):
            item = parse_line('@'+row['recipient'].lstrip('@')+' '+render(row))
            if any(item[key] != old[key] for key in ('recipient', 'title', 'number')):
                item = (await self.controller.prepare([], parsed=[item]))[0]
            else: item['owner_id'] = old['owner_id']
            prepared.append(item)
        self.controller.draft = prepared
        self.controller.save_draft()
        if enqueue: await self.command('/enqueue')
        else: self.events.put(('notice', 'Правки сохранены.'))

    def credentials(self, api_id, api_hash):
        import re
        try: api_id = int(api_id)
        except (ValueError, TypeError): raise ValueError('Введите API ID и API Hash в настройках.') from None
        if api_id <= 0 or not re.fullmatch('[0-9a-fA-F]{32}', api_hash): raise ValueError('Проверьте API ID и API Hash в настройках.')
        self.controller.api_id, self.controller.api_hash = api_id, api_hash
        return api_id

    async def do_connect_all(self, api_id, api_hash):
        if self.controller.running(): raise ValueError('Остановите очередь перед подключением аккаунтов.')
        self.credentials(api_id, api_hash)
        count = await self.controller.connect_sessions()
        for i in self.service.clients: self.account_status[i] = 'Подключён'
        self.events.put(('notice', f'Подключено аккаунтов: {count}'))

    async def do_connect(self, slot, api_id, api_hash, phone):
        if self.controller.running(): raise ValueError('Сначала остановите очередь.')
        api_id = self.credentials(api_id, api_hash)
        if slot in self.service.clients:
            self.events.put(('notice', 'Аккаунт уже подключён.')); return
        client = self.auth_clients.get(slot)
        if client is None:
            client = core.TelegramClient(str(core.DATA / f'account_{slot+1}'), api_id, api_hash)
            self.auth_clients[slot] = client
        await client.connect()
        if await client.is_user_authorized():
            self.service.clients[slot] = client
            self.auth_clients.pop(slot, None)
            self.account_status[slot] = 'Подключён'; return
        if not phone.strip(): raise ValueError('Введите номер телефона с кодом страны.')
        result = await client.send_code_request(phone.strip())
        self.hashes[slot], self.phones[slot] = result.phone_code_hash, phone.strip()
        self.account_status[slot] = 'Введите код Telegram'

    async def do_login(self, slot, code, password):
        client = self.auth_clients.get(slot)
        if client is None or slot not in self.hashes: raise ValueError('Сначала запросите код.')
        if self.account_status.get(slot) == 'Введите пароль 2FA':
            if not password: raise ValueError('Введите пароль 2FA.')
            await client.sign_in(password=password)
        else:
            await self.sign_in_code(slot, client, code, password)
            if self.account_status.get(slot) == 'Введите пароль 2FA': return
        self.service.clients[slot] = client
        self.auth_clients.pop(slot, None)
        self.hashes.pop(slot, None); self.phones.pop(slot, None)
        self.account_status[slot] = 'Подключён'
        self.events.put(('notice', f'Аккаунт {slot+1} подключён'))

    async def sign_in_code(self, slot, client, code, password):
        try:
            await client.sign_in(phone=self.phones[slot], code=code.strip(), phone_code_hash=self.hashes[slot])
        except SessionPasswordNeededError:
            if not password:
                self.account_status[slot] = 'Введите пароль 2FA'; return
            await client.sign_in(password=password)
            self.account_status[slot] = 'Проверка входа'

    async def do_logout(self, slot):
        await self.controller.stop()
        client = self.service.clients.get(slot)
        if client is None: raise ValueError('Сначала подключите сохранённую сессию.')
        await client.log_out()
        await client.disconnect()
        self.service.clients.pop(slot, None)
        self.account_status.pop(slot, None)
        self.service.restricted_accounts.discard(slot)
        self.service.peer_streaks.pop(slot, None)
        core.write_settings(core.DATA / 'account_limits.json', sorted(self.service.restricted_accounts))
        self.events.put(('notice', f'Аккаунт {slot+1}: сессия завершена.'))

    def shutdown(self):
        async def run():
            await asyncio.wrap_future(self.ready)
            for task in list(self.operations): task.cancel()
            await asyncio.gather(*list(self.operations), return_exceptions=True)
            self.pulse.cancel()
            await asyncio.gather(self.pulse, return_exceptions=True)
            await self.controller.stop()
            clients = list(self.service.clients.values())+list(self.auth_clients.values())
            await asyncio.gather(*(client.disconnect() for client in clients), return_exceptions=True)
            self.controller.queue.db.close()
        return asyncio.run_coroutine_threadsafe(run(), self.loop)
