"""Shared account/scanner runtime for Qt and headless Telegram control."""
import asyncio
import queue
import threading
import desktop as core

TEST_RECIPIENT = 'rus81quuodh'
TEST_MESSAGE = 'dcsnjdc Mirage Lamp #51160 1098 stars ru'


class Value:
    def __init__(self, value=''):
        self.value = value
    def get(self):
        return self.value
    def set(self, value):
        self.value = value


class Backend(core.App):
    def __init__(self, threaded=True):
        self.logs = queue.Queue()
        self.clients, self.hashes, self.connecting = {}, {}, set()
        self.task, self.paused = None, False
        self.testing, self.test_task = False, None
        self.rows = [(Value(), Value(), Value(), Value('Сохранённая сессия')) for _ in range(10)]
        limits = core.read_settings(core.DATA / 'account_limits.json', [])
        self.restricted_accounts = {i for i in limits if type(i) is int and 0 <= i < 10} if isinstance(limits, list) else set()
        disabled = core.read_settings(core.DATA / 'tracker_disabled.json', [])
        self.tracker_disabled = {i for i in disabled if type(i) is int and 0 <= i < 10} if isinstance(disabled, list) else set()
        self.loop = asyncio.new_event_loop() if threaded else asyncio.get_running_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True) if threaded else None
        if self.thread: self.thread.start()

    def begin(self, live, selection):
        async def run():
            if self.testing:
                self.log('Дождитесь завершения пробной отправки.'); return
            if self.task and not self.task.done():
                self.log('Уже запущено. Остановите сканирование перед изменением режима.')
                return
            self.paused = False
            self.task = asyncio.create_task(self.scan(live, selection))
            self.task.add_done_callback(self.finished)
        self.submit(run())

    async def send_test(self):
        if self.testing or (self.task and not self.task.done()):
            self.log('Пробное: остановите сканирование и дождитесь завершения текущей отправки.'); return
        self.testing = True; self.test_task = asyncio.current_task()
        sent = 0
        try:
            self.log(f'Пробное: получатель @{TEST_RECIPIENT}; текст: {TEST_MESSAGE}')
            for i, client in sorted(self.clients.items()):
                if i in self.restricted_accounts or i in self.connecting:
                    self.log(f'Пропуск • пробное • аккаунт {i+1}: ограничение или выполняется вход'); continue
                try:
                    if not await client.is_user_authorized():
                        self.log(f'Пропуск • пробное • аккаунт {i+1}: требуется вход'); continue
                    await self.send_with_cooldown(client, TEST_RECIPIENT, TEST_MESSAGE)
                except core.PeerFloodError:
                    self.peer_failure(i)
                except (core.UserPrivacyRestrictedError, core.UserIsBlockedError):
                    self.peer_success(i)
                    self.log(f'Пропуск • пробное • аккаунт {i+1}: сообщения ограничены получателем')
                except core.FloodWaitError as exc:
                    self.log(f'Telegram ограничил аккаунт {i+1} на {exc.seconds} сек. Пробная отправка остановлена.'); return
                except Exception as exc:
                    self.peer_success(i)
                    self.log(f'Не отправлено • пробное • аккаунт {i+1}: {type(exc).__name__}')
                else:
                    self.peer_success(i)
                    sent += 1
                    self.log(f'Отправлено • пробное • аккаунт {i+1} → @{TEST_RECIPIENT}')
            self.log(f'Пробное завершено: отправлено {sent} сообщений. История продавцов не изменена.')
        except asyncio.CancelledError:
            self.log('Пробная отправка остановлена'); raise
        finally:
            self.testing = False; self.test_task = None

    def stop(self):
        super().stop()
        if self.test_task and not self.test_task.done(): self.test_task.cancel()

    def shutdown(self):
        async def run():
            pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.gather(*(client.disconnect() for client in self.clients.values()), return_exceptions=True)
        return asyncio.run_coroutine_threadsafe(run(), self.loop)


