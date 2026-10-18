"""Headless Telegram controller. No Qt, desktop window or display required."""
import argparse
import asyncio
from collections import deque
from contextlib import contextmanager
import getpass
import json
import os
import queue
import re
import secrets
import time
import urllib.error
import urllib.request

import desktop as core
from runtime import Backend

DEFAULT_ADMIN_ID = 8957889799
MENU = {'keyboard': [['▶️ Запустить', '⏯ Пауза', '⏹ Стоп'], ['📊 Статус', '👤 Аккаунты', '🎁 Подарки'], ['🧾 Логи', '🧪 Пробное', '⚙️ Режим']], 'resize_keyboard': True}
HELP = ('Telegram Offers\n\n/run — запустить поиск\n/pause — пауза / продолжить\n/stop — остановить\n/status — состояние\n/accounts — аккаунты и участие в поиске\n/connect — подключить сохранённые сессии\n/gifts — выбрать коллекции\n/refresh — обновить каталог\n/mode — предпросмотр / отправка\n/test — пробные сообщения\n/logs — последние события\n\nОбщий интервал отправки: 15 секунд. По умолчанию — предпросмотр.')


class BotError(Exception):
    def __init__(self, message, code=None):
        self.code = code
        super().__init__(message)


class BotAPI:
    def __init__(self, token):
        self.token = token

    async def call(self, method, **parameters):
        def request():
            body = json.dumps(parameters).encode()
            req = urllib.request.Request(f'https://api.telegram.org/bot{self.token}/{method}', data=body, headers={'Content-Type': 'application/json'})
            try:
                with urllib.request.urlopen(req, timeout=35) as response:
                    result = json.load(response)
            except urllib.error.HTTPError as exc:
                code = exc.code; exc.close()
                raise BotError(f'Telegram HTTP {code}', code) from None
            except (OSError, ValueError):
                raise BotError('Telegram: ошибка сети или ответа') from None
            if not result.get('ok'):
                raise BotError(f'Telegram API: код {result.get("error_code", "unknown")}', result.get('error_code'))
            return result['result']
        return await asyncio.to_thread(request)


class Controller:
    def __init__(self, api, service, admin_id, api_id, api_hash):
        self.api, self.service, self.admin_id = api, service, admin_id
        self.api_id, self.api_hash = api_id, api_hash
        self.live = False
        self.history, self.statuses = deque(maxlen=300), {}
        self.catalog = core.read_settings(core.DATA / 'bot_catalog.json', [])
        if not isinstance(self.catalog, list): self.catalog = []
        self.selection = core.read_settings(core.DATA / 'gifts.json', {'all': True, 'names': []})
        if not isinstance(self.selection, dict) or not isinstance(self.selection.get('all'), bool) or not isinstance(self.selection.get('names'), list):
            self.selection = {'all': True, 'names': []}
        self.confirmations = {}
        self.started = time.time()

    async def send(self, text, markup=MENU):
        return await self.api.call('sendMessage', chat_id=self.admin_id, text=text[-3500:], reply_markup=markup)

    def authorized(self, update):
        query = update.get('callback_query')
        message = query.get('message', {}) if query else update.get('message', {})
        sender = query.get('from', {}) if query else message.get('from', {})
        chat = message.get('chat', {})
        return sender.get('id') == self.admin_id and chat.get('id') == self.admin_id and chat.get('type') == 'private'

    async def consume_events(self):
        while True:
            for _ in range(500):
                try: kind, payload = self.service.logs.get_nowait()
                except queue.Empty: break
                if kind == 'log': self.history.append(payload[1])
                elif kind == 'status': self.statuses[payload[0]] = payload[1]
                elif kind == 'signed_out': self.statuses.pop(payload, None)
                elif kind == 'catalog':
                    self.catalog = payload
                    try: core.write_settings(core.DATA / 'bot_catalog.json', payload)
                    except OSError: pass
            await asyncio.sleep(0.3)

    async def connect_sessions(self):
        count = 0
        for i in range(10):
            if not (core.DATA / f'account_{i+1}.session').exists(): continue
            client = self.service.clients.get(i)
            try:
                if client is None:
                    client = core.TelegramClient(str(core.DATA / f'account_{i+1}'), self.api_id, self.api_hash)
                await client.connect()
                if not await client.is_user_authorized():
                    self.service.clients.pop(i, None); self.statuses.pop(i, None)
                    await client.disconnect(); continue
            except Exception as exc:
                self.service.clients.pop(i, None); self.statuses.pop(i, None)
                self.service.log(f'Ошибка подключения аккаунта {i+1}: {type(exc).__name__}')
                if client: await client.disconnect()
                continue
            self.service.clients[i] = client
            self.statuses[i] = 'Подключён'; count += 1
        return count

    async def refresh(self):
        if self.running(): return 'Перед обновлением каталога нажмите «Стоп».'
        for i, client in sorted(self.service.clients.items()):
            if i in self.service.restricted_accounts: continue
            try:
                self.catalog = await core.watcher.get_gift_catalog(client)
                self.service.peer_success(i, 'read')
                core.write_settings(core.DATA / 'bot_catalog.json', self.catalog)
                return f'Каталог обновлён: {len(self.catalog)} коллекций.'
            except core.PeerFloodError:
                self.service.peer_failure(i, 'read'); return 'Telegram временно отказал в запросе каталога. Подробности в /logs.'
            except Exception as exc: return f'Каталог недоступен: {type(exc).__name__}.'
        return 'Нет подключённых аккаунтов. Используйте /connect или сначала выполните локальный вход --login 1.'

    def running(self):
        return self.service.testing or (self.service.task is not None and not self.service.task.done())

    async def stop(self):
        tasks = [task for task in (self.service.task, self.service.test_task) if task and not task.done()]
        self.service.stop()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def accounts(self):
        lines, buttons = ['Аккаунты'], []
        for i in sorted(self.service.clients):
            limited = i in self.service.restricted_accounts
            enabled = i not in self.service.tracker_disabled and not limited
            lines.append(f'{i+1}. {"🔴 PeerFloodError" if limited else "🟢 подключён"} · поиск {"включён" if enabled else "выключен"}')
            if not limited:
                buttons.append([{'text': f'{"✓" if enabled else "○"} Поиск — аккаунт {i+1}', 'callback_data': f'search:{i}'}])
            buttons.append([{'text': f'Выйти — аккаунт {i+1}', 'callback_data': f'logout:{i}'}])
        if len(lines) == 1: lines.append('Нет подключённых сессий. /connect — подключить сохранённые.')
        await self.send('\n'.join(lines), {'inline_keyboard': buttons} if buttons else MENU)

    async def gifts(self, page=0):
        if not self.catalog: await self.send('Сначала обновите каталог: /refresh'); return
        page = min(max(0, page), (len(self.catalog)-1)//8)
        selected = set(self.selection['names']); rows = [[{'text': f'{"✓" if self.selection["all"] else "○"} Все коллекции', 'callback_data': 'gifts:all'}]]
        for gift in self.catalog[page*8:page*8+8]:
            enabled = self.selection['all'] or gift['title'] in selected
            rows.append([{'text': f'{"✓" if enabled else "○"} {gift["title"]}', 'callback_data': f'gift:{gift["id"]}:{page}'}])
        navigation = []
        if page: navigation.append({'text': '← Назад', 'callback_data': f'page:{page-1}'})
        if (page+1)*8 < len(self.catalog): navigation.append({'text': 'Далее →', 'callback_data': f'page:{page+1}'})
        if navigation: rows.append(navigation)
        await self.send(f'Коллекции · страница {page+1}\nНажмите название, чтобы переключить. Изменения действуют со следующего запуска.', {'inline_keyboard': rows})

    async def confirm(self, action, text):
        nonce = secrets.token_urlsafe(10)
        self.confirmations = {key: value for key, value in self.confirmations.items() if value[1] > time.monotonic()}
        self.confirmations[nonce] = (action, time.monotonic()+60)
        await self.send(text, {'inline_keyboard': [[{'text': 'Подтвердить', 'callback_data': f'confirm:{nonce}'}, {'text': 'Отмена', 'callback_data': 'cancel'}]]})

    async def callback(self, data):
        if data == 'cancel': self.confirmations.clear(); await self.send('Отменено.'); return
        if data.startswith('confirm:'):
            action = self.confirmations.pop(data.split(':', 1)[1], None)
            if not action or action[1] < time.monotonic(): await self.send('Подтверждение истекло. Повторите команду.'); return
            command = action[0]
            if command == 'live':
                if self.running(): await self.send('Сначала остановите текущий процесс.'); return
                self.live = True; await self.send('Включена отправка. /run — запустить. Первый круг остаётся без сообщений.')
            elif command == 'test':
                if self.running(): await self.send('Сначала остановите текущий процесс.'); return
                self.service.submit(self.service.send_test()); await self.send('Пробная отправка запущена. Результаты: /logs.')
            elif command.startswith('logout:'):
                slot = int(command.split(':')[1]); await self.stop(); await self.service.logout_account(slot); await self.accounts()
            return
        if data == 'mode:live': await self.confirm('live', 'Включить реальные предложения владельцам подарков? Используйте отправку только для согласившихся получателей.'); return
        if data == 'mode:preview':
            await self.stop(); self.live = False; await self.send('Предпросмотр включён. /run — запустить без отправки.'); return
        if data.startswith('search:'):
            slot = int(data.split(':')[1])
            if slot not in self.service.clients or slot in self.service.restricted_accounts: return
            disabled = set(self.service.tracker_disabled)
            if slot in disabled: disabled.remove(slot)
            else: disabled.add(slot)
            core.write_settings(core.DATA / 'tracker_disabled.json', sorted(disabled)); self.service.tracker_disabled = disabled
            await self.accounts(); return
        if data.startswith('logout:'):
            slot = int(data.split(':')[1])
            if slot in self.service.clients: await self.confirm(data, f'Выйти из аккаунта {slot+1}? Для повторного входа потребуется код в локальном терминале.')
            return
        if data.startswith('page:'): await self.gifts(int(data.split(':')[1])); return
        if data == 'gifts:all':
            self.selection = {'all': not self.selection['all'], 'names': []}
            core.write_settings(core.DATA / 'gifts.json', self.selection); await self.gifts(); return
        if data.startswith('gift:'):
            _, gift_id, page = data.split(':')
            gift = next((gift for gift in self.catalog if gift['id'] == int(gift_id)), None)
            if not gift: return
            names = {gift['title'] for gift in self.catalog} if self.selection['all'] else set(self.selection['names'])
            if gift['title'] in names: names.remove(gift['title'])
            else: names.add(gift['title'])
            self.selection = {'all': False, 'names': sorted(names)}
            core.write_settings(core.DATA / 'gifts.json', self.selection); await self.gifts(int(page))

    async def handle(self, update):
        if not self.authorized(update): return
        query = update.get('callback_query')
        if query:
            await self.api.call('answerCallbackQuery', callback_query_id=query['id'])
            await self.callback(query.get('data', '')); return
        message = update['message']
        if message.get('date', 0) < self.started - 5: return
        text = message.get('text', '').strip()
        mapping = {'▶️ Запустить': '/run', '⏯ Пауза': '/pause', '⏹ Стоп': '/stop', '📊 Статус': '/status', '👤 Аккаунты': '/accounts', '🎁 Подарки': '/gifts', '🧾 Логи': '/logs', '🧪 Пробное': '/test', '⚙️ Режим': '/mode'}
        command = mapping.get(text, text.split()[0].split('@')[0] if text else '')
        if command in ('/start', '/help'): await self.send(HELP)
        elif command == '/status':
            enabled = [i for i in self.service.clients if i not in self.service.tracker_disabled and i not in self.service.restricted_accounts]
            await self.send(f'Telegram Offers\nПроцесс: {"пауза" if self.service.paused and self.running() else "работает" if self.running() else "остановлен"}\nРежим: {"отправка" if self.live else "предпросмотр"}\nАккаунтов: {len(self.service.clients)} · поиск: {len(enabled)}\nКоллекции: {"все" if self.selection["all"] else len(self.selection["names"])}\nОбщий интервал: 15 сек.\n\n' + next((line for line in reversed(self.history) if 'Рынок:' in line), 'Для запуска: /run'))
        elif command == '/run':
            if self.running(): await self.send('Процесс уже запущен. /pause или /stop.'); return
            if not self.selection['all'] and not self.selection['names']: await self.send('Выберите коллекции: /gifts'); return
            if not self.service.clients: await self.send('Сначала подключите сессии: /connect'); return
            self.service.begin(self.live, {'all': self.selection['all'], 'names': list(self.selection['names'])}); await self.send('Запускаю. Первый круг — без отправки. Состояние: /status.')
        elif command == '/pause':
            if not self.running(): await self.send('Процесс не запущен.'); return
            if self.service.testing: await self.send('Пробную отправку можно остановить командой /stop.'); return
            self.service.pause(); await self.send('Пауза.' if self.service.paused else 'Продолжаю.')
        elif command == '/stop': await self.stop(); await self.send('Остановлено.')
        elif command == '/connect':
            if self.running(): await self.send('Сначала нажмите «Стоп».'); return
            await self.send(f'Подключено сессий: {await self.connect_sessions()}. /accounts')
        elif command == '/accounts': await self.accounts()
        elif command == '/gifts': await self.gifts()
        elif command == '/refresh': await self.send(await self.refresh())
        elif command == '/logs': await self.send('\n'.join(list(self.history)[-15:]) or 'Событий пока нет.')
        elif command == '/mode': await self.send('Выберите режим:', {'inline_keyboard': [[{'text': 'Предпросмотр', 'callback_data': 'mode:preview'}, {'text': 'Отправка', 'callback_data': 'mode:live'}]]})
        elif command == '/test':
            if self.running(): await self.send('Сначала остановите сканирование.'); return
            await self.confirm('test', 'Отправить от каждого подключённого аккаунта пробное сообщение в @rus81quuodh?')
        else: await self.send(HELP)

    async def run(self):
        identity = await self.api.call('getMe')
        print(f'Бот @{identity.get("username", "unknown")} подключён. Администратор: {self.admin_id}. Откройте бота и нажмите /start.', flush=True)
        await self.connect_sessions()
        event_task = asyncio.create_task(self.consume_events())
        offset = 0
        try:
            while True:
                try:
                    updates = await self.api.call('getUpdates', offset=offset, timeout=20, allowed_updates=['message', 'callback_query'])
                    for update in updates:
                        offset = update['update_id'] + 1
                        try: await self.handle(update)
                        except (ValueError, KeyError): continue
                        except BotError: raise
                        except Exception as exc: self.service.log(f'Ошибка команды бота: {type(exc).__name__}')
                except BotError as exc:
                    if exc.code in (401, 403, 409): raise
                    self.service.log(str(exc))
                    print(str(exc), flush=True)
                    await asyncio.sleep(5)
        finally:
            event_task.cancel(); await asyncio.gather(event_task, return_exceptions=True)


@contextmanager
def process_lock():
    import fcntl
    with (core.DATA / 'controller.lock').open('a') as handle:
        try: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError('Бот уже запущен с этой папкой data') from None
        try: yield
        finally: fcntl.flock(handle, fcntl.LOCK_UN)


def setting(name, prompt, secret=False, no_input=False, default=None):
    value = os.getenv(name)
    if value: return value
    if default is not None: return str(default)
    if no_input: raise ValueError(f'Не задана переменная {name}')
    return (getpass.getpass if secret else input)(prompt).strip()


async def launch(args):
    try: api_id = int(setting('TG_API_ID', 'API ID: ', no_input=args.no_input))
    except ValueError: raise ValueError('TG_API_ID должен быть положительным числом') from None
    api_hash = setting('TG_API_HASH', 'API Hash (скрытый ввод): ', secret=True, no_input=args.no_input)
    if api_id <= 0 or not re.fullmatch('[0-9a-fA-F]{32}', api_hash): raise ValueError('Проверьте формат API ID и API Hash')
    if args.login:
        client = core.TelegramClient(str(core.DATA / f'account_{args.login}'), api_id, api_hash)
        try:
            await client.start(phone=lambda: input('Телефон с кодом страны: ').strip(), code_callback=lambda: getpass.getpass('Код Telegram (скрытый ввод): '), password=lambda: getpass.getpass('Пароль 2FA (скрытый ввод): '))
            print(f'Сессия аккаунта {args.login} сохранена.')
        finally: await client.disconnect()
        return
    token = setting('TG_CONTROL_BOT_TOKEN', 'Токен BotFather (скрытый ввод): ', secret=True, no_input=args.no_input)
    try: admin = int(setting('TG_CONTROL_ADMIN_ID', 'Telegram user ID: ', default=DEFAULT_ADMIN_ID))
    except ValueError: raise ValueError('TG_CONTROL_ADMIN_ID должен быть положительным числом') from None
    if admin <= 0 or not re.fullmatch(r'\d+:[A-Za-z0-9_-]{25,}', token): raise ValueError('Проверьте формат токена и Telegram user ID')
    service = Backend(threaded=False)
    controller = Controller(BotAPI(token), service, admin, api_id, api_hash)
    print('Подключение бота...', flush=True)
    try: await controller.run()
    finally:
        await controller.stop()
        await asyncio.gather(*(client.disconnect() for client in service.clients.values()), return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description='Telegram Offers — управление без окна через Telegram-бота (macOS/Linux).')
    parser.add_argument('--login', type=int, choices=range(1, 11), metavar='1..10', help='локально авторизовать слот и выйти; токен бота не нужен')
    parser.add_argument('--no-input', action='store_true', help='не спрашивать значения, использовать переменные окружения')
    args = parser.parse_args()
    try:
        core.DATA.chmod(0o700)
        with process_lock(): asyncio.run(launch(args))
    except KeyboardInterrupt: print('Контроллер остановлен.')
    except (BotError, ValueError, RuntimeError, OSError) as exc:
        print(f'Не удалось запустить: {exc}'); return 1
    except Exception as exc:
        print(f'Не удалось запустить: {type(exc).__name__}'); return 1
    return 0


if __name__ == '__main__': raise SystemExit(main())
