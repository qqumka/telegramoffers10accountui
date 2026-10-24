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
import socket
import ssl
import time
import urllib.error
import urllib.request
import certifi

import bot_core as core
from bot_core import Backend
from manual_queue import ManualQueue, parse_line, render, language, parse_results, parse_price

DEFAULT_ADMIN_ID = 8957889799
MENU = {'keyboard': [['▶️ Запустить', '⏯ Пауза', '⏹ Стоп'], ['📊 Статус', '👤 Аккаунты', '🧾 Логи']], 'resize_keyboard': True}
HELP = ('Пришлите список: одна строка — один получатель и NFT с итоговой ценой.\n'
        '@username Scared Cat #16951 425 gram\n'
        'Также можно переслать целиком «Результаты» из парсера и следующим сообщением указать общую цену: 425 gram. /price 425 gram — изменить цену всех строк черновика.\n'
        'Язык по отображаемому имени: cn / ar / ru. Можно указать язык в конце строки.\n'
        '/preview — черновик; /lang 1 cn — изменить язык; /edit 1 <строка> — заменить строку; /enqueue — подтвердить очередь; /cancel — удалить черновик.\n'
        '/run — отправить очередь; /pause — пауза; /stop — остановить, сохранив очередь; /status; /accounts; /connect; /logs.\n'
        'Общий интервал 15 секунд. История общая для всех аккаунтов; повторов нет.')



class BotError(Exception):
    def __init__(self, message, code=None, retryable=False):
        self.code, self.retryable = code, retryable
        super().__init__(message)


class BotAPI:
    def __init__(self, token):
        self.token = token
        self.context = ssl.create_default_context()
        self.context.load_verify_locations(cafile=certifi.where())

    @staticmethod
    def connection_error(exc):
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        if isinstance(reason, ssl.SSLCertVerificationError):
            return BotError('TLS: не удалось проверить сертификат api.telegram.org. Проверьте дату Mac, сертификаты Python и настройки HTTPS-прокси; проверка TLS включена.')
        if isinstance(reason, ssl.SSLError):
            return BotError('TLS: не удалось установить защищённое соединение с api.telegram.org. Проверьте VPN или HTTPS-прокси.')
        if isinstance(reason, socket.gaierror):
            return BotError('DNS: не удалось определить адрес api.telegram.org. Проверьте DNS и VPN.', retryable=True)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return BotError('Таймаут подключения к api.telegram.org. Проверьте доступность Telegram через вашу сеть или VPN.', retryable=True)
        if isinstance(reason, ConnectionRefusedError):
            return BotError('Соединение с api.telegram.org отклонено. Проверьте сеть, прокси и VPN.', retryable=True)
        return BotError('Сеть: api.telegram.org недоступен (' + type(reason).__name__ + '). Проверьте сеть, VPN и прокси.', retryable=True)

    async def call(self, method, **parameters):
        def request():
            body = json.dumps(parameters).encode()
            req = urllib.request.Request(f'https://api.telegram.org/bot{self.token}/{method}', data=body, headers={'Content-Type': 'application/json'})
            try:
                with urllib.request.urlopen(req, timeout=35, context=self.context) as response:
                    result = json.load(response)
            except urllib.error.HTTPError as exc:
                code = exc.code; exc.close()
                hints = {401: 'токен неверен или отозван', 403: 'доступ запрещён', 409: 'уже работает другой процесс бота или установлен webhook'}
                raise BotError(f'Telegram HTTP {code}: {hints.get(code, "запрос отклонён")}', code, retryable=code >= 500) from None
            except OSError as exc:
                raise self.connection_error(exc) from None
            except ValueError:
                raise BotError('Ответ api.telegram.org не является JSON. Возможна страница блокировки сети или прокси.', retryable=True) from None
            if not isinstance(result, dict):
                raise BotError('Некорректный формат ответа api.telegram.org')
            if not result.get('ok'):
                raise BotError(f'Telegram API: код {result.get("error_code", "unknown")}', result.get('error_code'))
            return result['result']
        # Retrying delivery after a timeout could duplicate messages; only reads retry.
        attempts = 3 if method in ('getMe', 'getUpdates') else 1
        for attempt in range(attempts):
            try: return await asyncio.to_thread(request)
            except BotError as exc:
                if not exc.retryable or attempt + 1 == attempts: raise
                await asyncio.sleep(1 + attempt*2)


class Controller:
    def __init__(self, api, service, admin_id, api_id, api_hash):
        self.api, self.service, self.admin_id = api, service, admin_id
        self.api_id, self.api_hash = api_id, api_hash
        self.queue = ManualQueue(core.DATA / 'offers.sqlite')
        self.draft = core.read_settings(core.DATA / 'manual_draft.json', [])
        self.pending_import = core.read_settings(core.DATA / 'manual_import.json', [])
        self.history, self.statuses = deque(maxlen=300), {}
        self.started = time.time()

    async def send(self, text, markup=MENU):
        result = None
        for start in range(0, len(text), 3500):
            result = await self.api.call('sendMessage', chat_id=self.admin_id, text=text[start:start+3500], reply_markup=markup)
        return result

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
                if kind == 'log':
                    self.history.append(payload[1])
                    self.queue.log(payload[1])
                elif kind == 'status': self.statuses[payload[0]] = payload[1]
                elif kind == 'signed_out': self.statuses.pop(payload, None)
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

    def running(self):
        return self.service.task is not None and not self.service.task.done()

    async def stop(self):
        task = self.service.task
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def preview(self):
        if self.pending_import:
            await self.send(f'Извлечено получателей: {len(self.pending_import)}. Пришлите итоговую цену для каждого сообщения, например 425 gram, или /price 425 gram. /cancel — отменить.')
            return
        if not self.draft:
            await self.send('Черновик пуст. Пришлите список получателей.'); return
        for start in range(0, len(self.draft), 10):
            await self.send('\n'.join(f'{i+1}. @{item["recipient"]} → {render(item)}' for i,item in enumerate(self.draft[start:start+10], start)))
        await self.send('Исправить язык: /lang 1 cn (также ar, ru). Заменить строку: /edit 1 @username Scared Cat #16951 425 gram ru. /enqueue — подтвердить и добавить в очередь; /cancel — удалить черновик.')

    async def prepare(self, lines, parsed=None):
        if parsed is None: parsed = [parse_line(line) for line in lines if line.strip()]
        if not parsed or len(parsed) > 100: raise ValueError('Пришлите от 1 до 100 строк.')
        slots = [i for i in sorted(self.service.clients) if i not in self.service.restricted_accounts]
        if not slots: raise ValueError('Сначала подключите аккаунты: /connect')
        client = self.service.clients[slots[0]]
        for item in parsed:
            entity = await client.get_entity(item['recipient'])
            if not isinstance(entity, core.User) or not getattr(entity, 'first_name', None) or getattr(entity, 'bot', False) or getattr(entity, 'is_self', False):
                raise ValueError('Получатель должен быть личным аккаунтом пользователя.')
            item['owner_id'] = entity.id
            item['lang'] = item['lang'] or language(' '.join(filter(None, [entity.first_name, getattr(entity, 'last_name', None)])))
        return parsed

    async def send_queue(self):
        self.service.paused = False
        try:
            while self.queue.next():
                if self.service.paused:
                    await asyncio.sleep(0.3); continue
                slots = [i for i in sorted(self.service.clients) if i not in self.service.restricted_accounts and i not in self.service.connecting]
                if not slots:
                    self.queue.log('Очередь остановлена: нет доступных аккаунтов'); return
                await asyncio.sleep(self.queue.delay())
                if self.service.paused: continue
                row = self.queue.next()
                ident, recipient, owner_id, text = row
                # Resolve on the selected account before committing the send attempt.
                cursor = self.queue.db.execute("SELECT value FROM state WHERE key='manual_cursor'").fetchone()
                slot = slots[(cursor[0] if cursor else 0) % len(slots)]
                client = self.service.clients[slot]
                try:
                    entity = await client.get_entity(recipient)
                    if entity.id != owner_id: raise ValueError('Получатель изменился')
                except asyncio.CancelledError: raise
                except Exception as exc:
                    self.queue.finish(ident, 'failed')
                    self.queue.log(f'Не отправлено @{recipient}: {type(exc).__name__}'); continue
                if self.service.paused: continue
                self.queue.claim(row, slots)
                try:
                    await self.service.send_with_cooldown(client, entity, text)
                except asyncio.CancelledError:
                    self.queue.finish(ident, 'uncertain'); raise
                except core.PeerFloodError:
                    self.service.peer_failure(slot)
                    self.queue.finish(ident, 'failed')
                    self.queue.log(f'PeerFlood: аккаунт {slot+1}, @{recipient}; без повтора')
                except core.FloodWaitError as exc:
                    self.queue.finish(ident, 'failed')
                    self.queue.log(f'FloodWait {exc.seconds} сек.; очередь остановлена, @{recipient} без повтора'); return
                except Exception as exc:
                    self.queue.finish(ident, 'uncertain')
                    self.queue.log(f'Исход отправки @{recipient} не подтверждён: {type(exc).__name__}; без повтора')
                else:
                    self.service.peer_success(slot)
                    self.queue.finish(ident, 'sent')
                    self.queue.log(f'Отправлено: аккаунт {slot+1} → @{recipient}: {text}')
        finally:
            self.queue.log('Обработка очереди завершена; ' + self.queue.summary())

    async def callback(self, data):
        # Legacy scanner buttons deliberately have no effect.
        await self.send('Используйте команды нового меню. /help')

    async def handle(self, update):
        if not self.authorized(update): return
        if update.get('callback_query'):
            await self.api.call('answerCallbackQuery', callback_query_id=update['callback_query']['id'])
            await self.callback(''); return
        message = update['message']
        if message.get('date', 0) < self.started - 5: return
        text = message.get('text', '').strip()
        mapping = {'▶️ Запустить': '/run', '⏯ Пауза': '/pause', '⏹ Стоп': '/stop', '📊 Статус': '/status', '👤 Аккаунты': '/accounts', '🧾 Логи': '/logs'}
        parts = text.split(maxsplit=1)
        command = mapping.get(text, parts[0].split('@')[0] if parts else '')
        try:
            if command in ('/start', '/help'): await self.send(HELP)
            elif command == '/connect':
                if self.running(): raise ValueError('Сначала /stop')
                await self.send(f'Подключено сессий: {await self.connect_sessions()}')
            elif command == '/accounts':
                await self.send('\n'.join(f'{i+1}: ' + ('ограничен' if i in self.service.restricted_accounts else 'подключён') for i in sorted(self.service.clients)) or 'Нет аккаунтов. /connect')
            elif command == '/status':
                await self.send(('Пауза' if self.service.paused and self.running() else 'Работает' if self.running() else 'Остановлен') + '\nОчередь: ' + self.queue.summary() + '\nОбщий интервал: 15 секунд.')
            elif command == '/logs':
                rows = self.queue.db.execute('SELECT time,message FROM manual_log ORDER BY id DESC LIMIT 15').fetchall()
                await self.send('\n'.join(time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stamp)) + ' ' + msg for stamp,msg in reversed(rows)) or 'Лог пуст.')
            elif command == '/run':
                if self.running(): raise ValueError('Уже запущено.')
                if not self.queue.next(): raise ValueError('Очередь пуста.')
                self.service.task = asyncio.create_task(self.send_queue())
                self.service.task.add_done_callback(self.service.finished)
                await self.send('Отправка очереди запущена.')
            elif command == '/pause':
                if not self.running(): raise ValueError('Очередь не запущена.')
                self.service.paused = not self.service.paused
                await self.send('Пауза.' if self.service.paused else 'Продолжаю.')
            elif command == '/stop':
                await self.stop(); await self.send('Остановлено. Неотправленная очередь сохранена.')
            elif command == '/preview': await self.preview()
            elif command == '/cancel':
                self.draft = []; self.pending_import = []; self.save_draft(); await self.send('Черновик удалён.')
            elif command == '/price':
                await self.apply_price(parts[1] if len(parts) > 1 else '')
            elif command == '/lang':
                args = parts[1].split()
                index = int(args[0])-1
                if len(args) != 2 or args[1] not in ('cn', 'ar', 'ru') or not 0 <= index < len(self.draft): raise ValueError('Используйте /lang 1 cn|ar|ru')
                self.draft[index]['lang'] = args[1]; self.save_draft(); await self.preview()
            elif command == '/edit':
                number, line = parts[1].split(maxsplit=1)
                index = int(number)-1
                if not 0 <= index < len(self.draft): raise ValueError('Неверный номер строки')
                item = (await self.prepare([line]))[0]
                self.draft[index] = item; self.save_draft(); await self.preview()
            elif command == '/enqueue':
                if self.pending_import: raise ValueError('Сначала укажите цену: 425 gram')
                if not self.draft: raise ValueError('Черновик пуст.')
                count = self.queue.add(self.draft)
                self.draft = []; self.save_draft()
                await self.send(f'Добавлено: {count}. Остальные уже есть в очереди или истории. /run — отправить.')
            elif command.startswith('/'): await self.send(HELP)
            else:
                if self.pending_import:
                    await self.apply_price(text)
                    return
                if self.draft: raise ValueError('Есть черновик: /preview, /enqueue или /cancel перед новым списком.')
                results = parse_results(text)
                if results is not None:
                    self.pending_import = results
                else:
                    self.draft = await self.prepare(text.splitlines())
                self.save_draft(); await self.preview()
        except (ValueError, IndexError) as exc:
            await self.send(str(exc) or 'Проверьте формат команды.')
        except BotError:
            raise
        except Exception as exc:
            self.queue.log(f'Ошибка команды: {type(exc).__name__}')
            await self.send('Не удалось выполнить команду: ' + type(exc).__name__ + '. Черновик и очередь сохранены.')

    async def apply_price(self, text):
        price = parse_price(text)
        if self.pending_import:
            items = [dict(item, price=price) for item in self.pending_import]
            self.draft = await self.prepare([], parsed=items)
            self.pending_import = []
        elif self.draft:
            self.draft = [dict(item, price=price) for item in self.draft]
        else:
            raise ValueError('Сначала пришлите список получателей.')
        self.save_draft()
        await self.preview()

    def save_draft(self):
        core.write_settings(core.DATA / 'manual_draft.json', self.draft)
        core.write_settings(core.DATA / 'manual_import.json', self.pending_import)

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
        controller.queue.db.close()


def main():
    parser = argparse.ArgumentParser(description='Telegram Offers — управление без окна через Telegram-бота (macOS/Linux).')
    parser.add_argument('--login', type=int, choices=range(1, 11), metavar='1..10', help='локально авторизовать слот и выйти; токен бота не нужен')
    parser.add_argument('--no-input', action='store_true', help='не спрашивать значения, использовать переменные окружения')
    parser.add_argument('--check-network', action='store_true', help='проверить HTTPS Telegram без токена и API-ключей')
    args = parser.parse_args()
    try:
        if args.check_network:
            async def check_network():
                try: await BotAPI('0:network-diagnostic').call('getMe')
                except BotError as exc:
                    if exc.code not in (401, 404): raise
                print('HTTPS api.telegram.org доступен; проверка сертификатов включена. Токен не использовался.')
            asyncio.run(check_network()); return 0
        core.DATA.chmod(0o700)
        with process_lock(): asyncio.run(launch(args))
    except KeyboardInterrupt: print('Контроллер остановлен.')
    except (BotError, ValueError, RuntimeError, OSError) as exc:
        print(f'Не удалось запустить: {exc}'); return 1
    except Exception as exc:
        print(f'Не удалось запустить: {type(exc).__name__}'); return 1
    return 0


if __name__ == '__main__': raise SystemExit(main())
