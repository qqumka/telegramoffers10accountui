"""Qt desktop interface; scanner and account operations reuse the tested backend."""
import asyncio
from collections import deque
import html
import os
from pathlib import Path
import queue
import re
import sys
import threading

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF, QPropertyAnimation
from PySide6.QtGui import QColor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QFrame, QLabel,
    QPushButton, QVBoxLayout, QHBoxLayout, QGridLayout, QLineEdit, QCheckBox,
    QScrollArea, QTextEdit, QDialog, QFileDialog, QMessageBox, QStackedWidget,
    QGraphicsDropShadowEffect, QGraphicsOpacityEffect, QButtonGroup)
from PIL import Image, ImageOps
import desktop as core

DATA = core.DATA
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
    def __init__(self):
        self.logs = queue.Queue()
        self.clients, self.hashes, self.connecting = {}, {}, set()
        self.task, self.paused = None, False
        self.testing, self.test_task = False, None
        self.rows = [(Value(), Value(), Value(), Value('Сохранённая сессия')) for _ in range(10)]
        limits = core.read_settings(DATA / 'account_limits.json', [])
        self.restricted_accounts = {i for i in limits if type(i) is int and 0 <= i < 10} if isinstance(limits, list) else set()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

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
                    await client.send_message(TEST_RECIPIENT, TEST_MESSAGE, parse_mode=None)
                except core.PeerFloodError:
                    self.mark_peer_flood(i); return
                except core.FloodWaitError as exc:
                    self.log(f'Telegram ограничил аккаунт {i+1} на {exc.seconds} сек. Пробная отправка остановлена.'); return
                except Exception as exc:
                    self.log(f'Не отправлено • пробное • аккаунт {i+1}: {type(exc).__name__}')
                else:
                    sent += 1
                    self.log(f'Отправлено • пробное • аккаунт {i+1} → @{TEST_RECIPIENT}')
                    await asyncio.sleep(1)
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


def icon(name, color='#727780'):
    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.GlobalColor.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor(color), 1.7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    if name == 'plus':
        p.drawLine(12, 5, 12, 19); p.drawLine(5, 12, 19, 12)
    elif name == 'accounts':
        p.drawEllipse(QRectF(8, 3, 8, 8)); p.drawArc(QRectF(4, 12, 16, 16), 0, 180*16)
    elif name == 'gifts':
        p.drawRoundedRect(QRectF(4, 10, 16, 11), 2, 2); p.drawRect(QRectF(3, 7, 18, 4))
        p.drawLine(12, 7, 12, 21); p.drawEllipse(QRectF(5, 2, 7, 5)); p.drawEllipse(QRectF(12, 2, 7, 5))
    elif name == 'logs':
        p.drawRoundedRect(QRectF(4, 3, 16, 18), 3, 3)
        for y in (8, 12, 16): p.drawLine(8, y, 16, y)
    elif name == 'settings':
        for x, y in ((6, 8), (12, 16), (18, 10)):
            p.drawLine(x, 4, x, 20); p.setBrush(QColor('#ffffff')); p.drawEllipse(QPointF(x, y), 2.5, 2.5); p.setBrush(Qt.BrushStyle.NoBrush)
    elif name == 'overview':
        for x in (4, 14):
            for y in (4, 14): p.drawRoundedRect(QRectF(x, y, 6, 6), 1.5, 1.5)
    elif name == 'play':
        p.setBrush(QColor(color)); path = QPainterPath(); path.moveTo(8, 5); path.lineTo(19, 12); path.lineTo(8, 19); path.closeSubpath(); p.drawPath(path)
    elif name == 'pause':
        p.drawLine(8, 5, 8, 19); p.drawLine(16, 5, 16, 19)
    elif name == 'stop':
        p.drawRoundedRect(QRectF(6, 6, 12, 12), 2, 2)
    else:
        path = QPainterPath(); path.moveTo(3, 10); path.lineTo(21, 3); path.lineTo(15, 21); path.lineTo(11, 14); path.lineTo(3, 10); p.drawPath(path)
        p.drawLine(11, 14, 17, 7)
    p.end()
    return QIcon(pixmap)


class Check(QCheckBox):
    """Vector tick, independent of the platform's cross-shaped indicator."""
    def __init__(self, text):
        super().__init__(text); self.setMinimumHeight(30); self.setCursor(Qt.CursorShape.PointingHandCursor)
    def hitButton(self, position):
        return self.rect().contains(position)
    def paintEvent(self, event):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled(): p.setOpacity(0.45)
        y = (self.height()-20)/2
        p.setPen(QPen(QColor('#e6bb3b' if self.isChecked() else '#cfd2d7'), 1.3))
        p.setBrush(QColor('#f4ce59' if self.isChecked() else '#ffffff'))
        p.drawRoundedRect(QRectF(1, y, 20, 20), 6, 6)
        if self.isChecked():
            p.setPen(QPen(QColor('#292c32'), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(6, y+10), QPointF(10, y+14)); p.drawLine(QPointF(10, y+14), QPointF(17, y+6))
        p.setPen(QColor('#292c32')); p.setFont(self.font())
        p.drawText(QRectF(33, 0, self.width()-35, self.height()), Qt.AlignmentFlag.AlignVCenter, self.text())
        if self.hasFocus():
            p.setPen(QPen(QColor('#a8acb4'), 1, Qt.PenStyle.DotLine)); p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(0, 0, self.width()-1, self.height()-1), 6, 6)


class Wallpaper(QWidget):
    def __init__(self):
        super().__init__(); self.cover = QPixmap()
    def paintEvent(self, event):
        p = QPainter(self); p.fillRect(self.rect(), QColor('#f5f5f3'))
        if not self.cover.isNull():
            p.setOpacity(0.16)
            scaled = self.cover.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap((self.width()-scaled.width())//2, (self.height()-scaled.height())//2, scaled)


class Hero(QWidget):
    def __init__(self):
        super().__init__(); self.cover = QPixmap(); self.setMinimumHeight(200)
        box = QVBoxLayout(self); box.setContentsMargins(28, 24, 28, 24)
        self.eyebrow = QLabel('TELEGRAM OFFERS  /  ВАША КОЛЛЕКЦИЯ ВОЗМОЖНОСТЕЙ'); self.eyebrow.setObjectName('heroSmall')
        self.title = QLabel('Новые находки.\nПод вашим контролем.'); self.title.setObjectName('heroTitle')
        self.note = QLabel('Только Stars. Точные предложения. Всё в одном месте.'); self.note.setObjectName('heroNote')
        box.addWidget(self.eyebrow); box.addStretch(); box.addWidget(self.title); box.addWidget(self.note)
    def paintEvent(self, event):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath(); path.addRoundedRect(QRectF(self.rect()), 24, 24); p.setClipPath(path)
        if not self.cover.isNull():
            scaled = self.cover.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap((self.width()-scaled.width())//2, (self.height()-scaled.height())//2, scaled)
            gradient = QLinearGradient(0, 0, self.width(), 0)
            gradient.setColorAt(0, QColor(15, 17, 22, 205)); gradient.setColorAt(1, QColor(15, 17, 22, 35))
        else:
            gradient = QLinearGradient(0, 0, self.width(), self.height())
            gradient.setColorAt(0, QColor('#292b31')); gradient.setColorAt(1, QColor('#505052'))
        p.fillRect(self.rect(), gradient)
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(244, 206, 89, 35))
        p.drawEllipse(QPointF(self.width()-80, 35), 140, 140)
        p.setBrush(QColor(244, 206, 89, 20)); p.drawEllipse(QPointF(self.width()-30, 160), 95, 95)


STYLE = '''
QWidget { font-family: "SF Pro Text", "Inter", "Segoe UI", "DejaVu Sans"; font-size: 13px; color: #292c32; }
QMainWindow, QDialog { background: #f5f5f3; }
QFrame#sidebar { background: #191b20; border-radius: 0; }
QLabel#brand { color: #ffffff; font-size: 21px; font-weight: 700; }
QLabel#sideNote { color: #8c909c; font-size: 11px; }
QLabel#pageTitle { font-size: 28px; font-weight: 700; letter-spacing: -0.7px; }
QLabel#subtitle, QLabel#muted { color: #7c8089; }
QLabel#heroTitle { color: #ffffff; font-size: 31px; font-weight: 700; letter-spacing: -0.6px; background: transparent; }
QLabel#heroSmall { color: #f4ce59; font-size: 10px; font-weight: 600; letter-spacing: 1.3px; background: transparent; }
QLabel#heroNote { color: #dbdce0; font-size: 12px; background: transparent; }
QFrame#card { background: rgba(255,255,255,225); border: 1px solid rgba(225,226,229,160); border-radius: 20px; }
QLabel#metric { font-size: 27px; font-weight: 700; }
QLabel#badge { color: #8b6b16; background: #faf1d6; border-radius: 9px; padding: 5px 10px; font-size: 11px; }
QPushButton { background: #ffffff; border: 1px solid #e2e3e6; border-radius: 13px; padding: 11px 16px; font-weight: 600; }
QPushButton:hover { background: #f1f2f3; border-color: #ccd0d6; }
QPushButton:pressed { background: #e5e7eb; }
QPushButton:disabled { color: #b2b5bd; background: #f2f3f4; border-color: #e9eaed; }
QPushButton[role="primary"] { background: #f4ce59; border-color: #f4ce59; color: #292c32; }
QPushButton[role="primary"]:hover { background: #ffdd75; border-color: #ffdd75; }
QPushButton[role="primary"]:pressed { background: #e6bb3b; }
QPushButton[role="danger"] { color: #c95a5a; background: #fff4f4; border-color: #f4dede; }
QPushButton[role="danger"]:hover { background: #fde8e8; }
QPushButton[role="nav"] { text-align: left; color: #999daa; background: transparent; border: none; border-radius: 12px; padding: 13px; }
QPushButton[role="nav"]:hover { background: #252830; color: #ffffff; }
QPushButton[role="nav"]:checked { color: #f4ce59; background: #302d25; }
QLineEdit { background: #f7f8fa; border: 1px solid #e1e4e9; border-radius: 12px; padding: 12px; selection-background-color: #f4ce59; }
QLineEdit:focus { border: 1px solid #d6b044; background: #ffffff; }
QTextEdit { background: transparent; border: none; font-family: "Menlo", "DejaVu Sans Mono"; font-size: 12px; padding: 4px; }
QScrollArea, QStackedWidget { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 7px; margin: 4px 0; }
QScrollBar::handle:vertical { background: #d0d3d8; border-radius: 3px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QToolTip { background: #252830; color: #ffffff; padding: 8px; border-radius: 6px; }
'''


def label(text, name=None):
    result = QLabel(text)
    if name: result.setObjectName(name)
    result.setWordWrap(True)
    return result


def button(text, callback, role=None, symbol=None):
    result = QPushButton(text); result.setCursor(Qt.CursorShape.PointingHandCursor)
    if role: result.setProperty('role', role)
    if symbol: result.setIcon(icon(symbol, '#292c32'))
    result.clicked.connect(lambda checked=False: callback())
    return result


def card():
    result = QFrame(); result.setObjectName('card')
    box = QVBoxLayout(result); box.setContentsMargins(20, 18, 20, 18); box.setSpacing(12)
    effect = QGraphicsDropShadowEffect(result); effect.setBlurRadius(24); effect.setOffset(0, 5); effect.setColor(QColor(30, 35, 45, 12)); result.setGraphicsEffect(effect)
    return result, box


class Window(QMainWindow):
    def __init__(self):
        super().__init__(); self.setWindowTitle('Telegram Offers'); self.resize(1180, 850); self.setMinimumSize(1000, 730)
        self.backend = Backend(); self.history = deque(maxlen=2500); self.catalog = []
        self.accounts, self.statuses, self.connected = set(), {}, set()
        self.auth_dialogs = {}; self.detached = None; self.closing = False; self.closed = False
        selection = core.read_settings(DATA / 'gifts.json', {'all': True, 'names': []})
        self.selection = selection if isinstance(selection, dict) and isinstance(selection.get('all'), bool) and isinstance(selection.get('names'), list) and all(isinstance(name, str) for name in selection['names']) else {'all': True, 'names': []}
        shell = QWidget(); layout = QHBoxLayout(shell); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(0); self.setCentralWidget(shell)
        sidebar = QFrame(); sidebar.setObjectName('sidebar'); sidebar.setFixedWidth(215)
        side = QVBoxLayout(sidebar); side.setContentsMargins(22, 28, 22, 24); side.setSpacing(8)
        side.addWidget(label('Telegram\nOffers', 'brand')); side.addWidget(label('ВАША РАБОЧАЯ СТУДИЯ', 'sideNote')); side.addSpacing(28)
        self.nav_group = QButtonGroup(self); self.nav_group.setExclusive(True)
        self.stack = QStackedWidget()
        for i, (name, symbol) in enumerate((('Обзор', 'overview'), ('Аккаунты', 'accounts'), ('Подарки', 'gifts'), ('Журнал', 'logs'), ('Настройки', 'settings'))):
            item = button(name, lambda index=i: self.navigate(index), 'nav')
            item.setIcon(icon(symbol, '#a5a8b2')); item.setCheckable(True); self.nav_group.addButton(item, i); side.addWidget(item)
        self.nav_group.button(0).setChecked(True)
        side.addStretch(); self.side_status = label('●  Предпросмотр', 'sideNote'); side.addWidget(self.side_status)
        side.addWidget(button('Окно логов', self.open_logs, 'nav'))
        side.addWidget(label('До 10 аккаунтов\nТолько подарки за Stars', 'sideNote'))
        layout.addWidget(sidebar)
        self.wallpaper = Wallpaper(); content = QVBoxLayout(self.wallpaper); content.setContentsMargins(28, 24, 28, 24); content.addWidget(self.stack); layout.addWidget(self.wallpaper, 1)
        self.create_overview(); self.create_accounts(); self.create_gifts(); self.create_logs(); self.create_settings()
        for i in range(10):
            if (DATA / f'account_{i+1}.session').exists():
                self.accounts.add(i); self.statuses[i] = 'Сохранённая сессия'
        self.render_accounts(); self.render_gifts(); self.update_metrics(); self.load_cover()
        self.timer = QTimer(self); self.timer.timeout.connect(self.drain); self.timer.start(100)

    def page(self, title, subtitle):
        widget = QWidget(); box = QVBoxLayout(widget); box.setContentsMargins(0, 0, 0, 0); box.setSpacing(20)
        header = QHBoxLayout(); texts = QVBoxLayout(); texts.setSpacing(6); texts.addWidget(label(title, 'pageTitle')); texts.addWidget(label(subtitle, 'subtitle')); header.addLayout(texts, 1); box.addLayout(header)
        self.stack.addWidget(widget)
        return widget, box, header

    def navigate(self, index):
        self.nav_group.button(index).setChecked(True)
        if self.stack.currentIndex() == index: return
        if getattr(self, 'transition', None) is not None:
            try: self.fade.stop(); self.transition.hide(); self.transition.deleteLater()
            except RuntimeError: pass
        snapshot = self.stack.grab()
        self.stack.setCurrentIndex(index)
        overlay = self.transition = QLabel(self.stack); overlay.setPixmap(snapshot); overlay.setGeometry(self.stack.rect()); overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents); overlay.show(); overlay.raise_()
        effect = QGraphicsOpacityEffect(overlay); overlay.setGraphicsEffect(effect)
        self.fade = QPropertyAnimation(effect, b'opacity', overlay); self.fade.setDuration(180); self.fade.setStartValue(1); self.fade.setEndValue(0); self.fade.finished.connect(overlay.deleteLater); self.fade.start()

    def create_overview(self):
        _, box, head = self.page('Обзор', 'Спокойный рабочий процесс. Все важные события — перед вами.')
        self.mode_badge = label('ПРЕДПРОСМОТР', 'badge'); self.mode_badge.setMaximumHeight(30); head.addWidget(self.mode_badge)
        self.hero = Hero(); box.addWidget(self.hero)
        metrics = QHBoxLayout(); metrics.setSpacing(14); self.metric_values = []
        for caption in ('Подключено аккаунтов', 'Коллекции в поиске', 'Отправлено за запуск'):
            item, inner = card(); inner.addWidget(label(caption, 'muted')); value = label('0', 'metric'); inner.addWidget(value); self.metric_values.append(value); metrics.addWidget(item)
        box.addLayout(metrics); self.sent = 0
        controls, inner = card(); row = QHBoxLayout(); self.live = Check('Отправка сообщений'); self.live.toggled.connect(self.update_metrics); row.addWidget(self.live); row.addStretch()
        self.start_button = button('Запустить', self.start, 'primary', 'play'); self.pause_button = button('Пауза', lambda: self.backend.loop.call_soon_threadsafe(self.backend.pause), symbol='pause'); self.stop_button = button('Стоп', lambda: self.backend.loop.call_soon_threadsafe(self.backend.stop), symbol='stop')
        self.test_button = button('Пробное', self.send_test)
        for item in (self.test_button, self.start_button, self.pause_button, self.stop_button): row.addWidget(item)
        inner.addLayout(row); inner.addWidget(label('Первый круг запоминает рынок. Со второго — только новые находки.', 'muted')); box.addWidget(controls)
        events, inner = card(); top = QHBoxLayout(); top.addWidget(label('Последние события')); top.addStretch(); top.addWidget(button('Все логи', lambda: self.navigate(3))); inner.addLayout(top)
        self.preview_log = QTextEdit(); self.preview_log.setReadOnly(True); self.preview_log.setPlaceholderText('События появятся здесь после запуска сканирования.'); self.preview_log.document().setMaximumBlockCount(250); inner.addWidget(self.preview_log); box.addWidget(events, 1)

    def create_accounts(self):
        _, box, head = self.page('Аккаунты', 'Добавляйте по одному. Сессии сохраняются на этом компьютере.')
        head.addWidget(button('Добавить аккаунт', self.add_account, 'primary', 'plus'))
        self.account_area = QScrollArea(); self.account_area.setWidgetResizable(True); self.account_body = QWidget(); self.account_box = QVBoxLayout(self.account_body); self.account_box.setContentsMargins(2, 2, 8, 10); self.account_box.setSpacing(12); self.account_area.setWidget(self.account_body); box.addWidget(self.account_area)

    def render_accounts(self):
        self.clear_layout(self.account_box)
        if not self.accounts:
            empty, inner = card(); inner.setContentsMargins(30, 45, 30, 45); inner.addWidget(label('Начните с первого аккаунта', 'pageTitle')); inner.addWidget(label('Войдите по номеру телефона — код и пароль 2FA вводятся в отдельном окне.', 'muted')); inner.addWidget(button('Добавить аккаунт', self.add_account, 'primary', 'plus')); self.account_box.addWidget(empty)
        for i in sorted(self.accounts):
            item, inner = card(); row = QHBoxLayout(); avatar = label(f'{i+1:02d}'); avatar.setFixedSize(44, 44); avatar.setAlignment(Qt.AlignmentFlag.AlignCenter); avatar.setStyleSheet('background: #faf1d6; border-radius: 16px; color: #8b6b16; font-size: 17px; font-weight: 700;'); row.addWidget(avatar)
            details = QVBoxLayout(); phone = self.backend.rows[i][0].get(); details.addWidget(label(phone or f'Аккаунт {i+1}'))
            limited = i in self.backend.restricted_accounts; status = label('● PeerFloodError' if limited else self.statuses.get(i, 'Не подключён'), 'muted')
            if limited: status.setStyleSheet('color: #cc5555; font-weight: 600;')
            details.addWidget(status); row.addLayout(details, 1); row.addWidget(button('Подключить', lambda slot=i: self.open_auth(slot))); row.addWidget(button('Выйти', lambda slot=i: self.logout(slot), 'danger')); inner.addLayout(row); self.account_box.addWidget(item)
        self.account_box.addStretch()

    def create_gifts(self):
        _, box, head = self.page('Подарки', 'Ваш фокус. Оставьте только нужные коллекции или включите весь каталог.')
        head.addWidget(button('Обновить каталог', self.refresh_catalog))
        controls, inner = card(); self.all_gifts = Check('Все коллекции Telegram'); self.all_gifts.setChecked(self.selection['all']); self.all_gifts.clicked.connect(self.toggle_all); inner.addWidget(self.all_gifts)
        self.gift_search = QLineEdit(); self.gift_search.setPlaceholderText('Найти коллекцию — например, Mirage Lamp'); self.gift_search.textChanged.connect(self.filter_gifts); inner.addWidget(self.gift_search); box.addWidget(controls)
        self.gift_area = QScrollArea(); self.gift_area.setWidgetResizable(True); self.gift_body = QWidget(); self.gift_box = QVBoxLayout(self.gift_body); self.gift_box.setContentsMargins(4, 4, 8, 8); self.gift_box.setSpacing(8); self.gift_area.setWidget(self.gift_body); box.addWidget(self.gift_area, 1)
        footer = QHBoxLayout(); footer.addWidget(label('Выбор применяется со следующего запуска сканирования.', 'muted')); footer.addStretch(); footer.addWidget(button('Сохранить выбор', self.save_gifts, 'primary')); box.addLayout(footer); self.gift_checks = {}

    def render_gifts(self):
        values = {name: check.isChecked() for name, check in self.gift_checks.items()}
        self.clear_layout(self.gift_box); self.gift_checks = {}; self.gift_cards = {}
        names = sorted(set(core.watcher.GIFT_NAME_WHITELIST) | {gift['title'] for gift in self.catalog} | set(self.selection['names']), key=str.lower)
        for name in names:
            item, inner = card(); inner.setContentsMargins(16, 10, 16, 10); check = Check(name); check.setMinimumHeight(30); check.setChecked(values.get(name, self.all_gifts.isChecked() or name in self.selection['names'])); check.clicked.connect(lambda checked: self.all_gifts.setChecked(False)); inner.addWidget(check); self.gift_checks[name] = check; self.gift_cards[name] = item; self.gift_box.addWidget(item)
        self.gift_box.addStretch(); self.filter_gifts()

    def filter_gifts(self):
        text = self.gift_search.text().strip().lower()
        for name, item in getattr(self, 'gift_cards', {}).items(): item.setVisible(text in name.lower())

    def toggle_all(self):
        for check in self.gift_checks.values(): check.setChecked(self.all_gifts.isChecked())

    def save_gifts(self):
        selection = {'all': self.all_gifts.isChecked(), 'names': sorted(name for name, check in self.gift_checks.items() if check.isChecked())}
        try: core.write_settings(DATA / 'gifts.json', selection)
        except OSError: self.warn('Не удалось сохранить выбор подарков.'); return
        self.selection = selection; self.update_metrics(); self.backend.log('Выбор подарков сохранён. Применится после нового запуска.')

    def create_logs(self):
        _, box, head = self.page('Журнал', 'От находки до отправки. История каждого действия.')
        head.addWidget(button('Отдельное окно', self.open_logs))
        panel, inner = card(); controls = QHBoxLayout(); self.follow = Check('Автопрокрутка'); self.follow.setChecked(True); controls.addWidget(self.follow); controls.addStretch(); controls.addWidget(button('Очистить', self.clear_logs)); inner.addLayout(controls)
        self.log_text = QTextEdit(); self.log_text.setReadOnly(True); self.log_text.document().setMaximumBlockCount(2500); inner.addWidget(self.log_text); box.addWidget(panel, 1)

    def create_settings(self):
        _, box, _ = self.page('Настройки', 'Подключение Telegram и ваше оформление рабочего пространства.')
        panel, inner = card(); inner.addWidget(label('Telegram API')); inner.addWidget(label('API ID и API Hash из одного приложения на my.telegram.org/apps.', 'muted'))
        fields = QHBoxLayout(); self.api_id = QLineEdit(os.getenv('TG_API_ID', '')); self.api_id.setPlaceholderText('API ID'); self.api_hash = QLineEdit(os.getenv('TG_API_HASH', '')); self.api_hash.setPlaceholderText('API Hash'); self.api_hash.setEchoMode(QLineEdit.EchoMode.Password); fields.addWidget(self.api_id, 1); fields.addWidget(self.api_hash, 2); inner.addLayout(fields)
        inner.addWidget(label('Ключи не публикуются и не записываются в исходный код.', 'muted')); box.addWidget(panel)
        cover, inner = card(); inner.addWidget(label('Ваша обложка')); inner.addWidget(label('Картинка появится на главном экране и станет мягким фоном страниц.', 'muted')); inner.addWidget(button('Выбрать изображение', self.choose_cover)); box.addWidget(cover); box.addStretch()

    def add_account(self):
        i = next((slot for slot in range(10) if slot not in self.accounts and slot not in self.auth_dialogs), None)
        if i is None: self.warn('Можно добавить до 10 аккаунтов. Сначала выйдите из одного из них.'); return
        self.open_auth(i)

    def open_auth(self, i):
        if i in self.auth_dialogs:
            self.auth_dialogs[i].raise_(); self.auth_dialogs[i].activateWindow(); return
        dialog = QDialog(self); dialog.setWindowTitle('Добавить аккаунт'); dialog.resize(430, 430); box = QVBoxLayout(dialog); box.setContentsMargins(28, 26, 28, 26); box.setSpacing(14)
        box.addWidget(label('Вход в Telegram', 'pageTitle')); box.addWidget(label('Используйте свой номер телефона. Сохранённая сессия подключится без повторного кода.', 'muted'))
        phone = QLineEdit(self.backend.rows[i][0].get()); phone.setPlaceholderText('+7 900 000 00 00'); code = QLineEdit(); code.setPlaceholderText('Код Telegram'); password = QLineEdit(); password.setEchoMode(QLineEdit.EchoMode.Password); password.setPlaceholderText('Пароль 2FA, если установлен')
        box.addWidget(phone); box.addWidget(button('Получить код / подключить сессию', lambda: self.connect_account(i, phone.text()))); box.addWidget(code); box.addWidget(password)
        status = label(self.statuses.get(i, 'Введите номер телефона'), 'muted'); box.addWidget(status)
        def login():
            self.backend.rows[i][1].set(code.text()); self.backend.rows[i][2].set(password.text()); self.backend.login(i)
        box.addWidget(button('Войти', login, 'primary')); dialog.status_label = status
        dialog.finished.connect(lambda result: self.auth_dialogs.pop(i, None)); self.auth_dialogs[i] = dialog; dialog.show()

    def connect_account(self, i, phone):
        try:
            api_id = int(self.api_id.text().strip()); api_hash = self.api_hash.text().strip()
            if api_id <= 0 or not re.fullmatch(r'[0-9a-fA-F]{32}', api_hash): raise ValueError()
        except ValueError:
            self.warn('Откройте «Настройки» и введите API ID и API Hash с my.telegram.org/apps.'); return
        self.backend.rows[i][0].set(phone.strip())
        async def run():
            if self.backend.testing or (self.backend.task and not self.backend.task.done()):
                self.backend.log('Остановите сканирование перед подключением аккаунтов.'); return
            await self.backend.connect_account(i, api_id, api_hash, phone.strip())
        self.backend.submit(run())

    def logout(self, i):
        if self.backend.testing: self.warn('Дождитесь завершения пробной отправки или нажмите «Стоп».'); return
        if QMessageBox.question(self, 'Выход из аккаунта', 'Завершить эту сессию Telegram? Сканирование остановится. Для повторного входа потребуется код.') != QMessageBox.StandardButton.Yes: return
        if i not in self.backend.clients:
            self.warn('Сначала подключите сохранённую сессию, чтобы завершить её в Telegram.'); return
        self.backend.submit(self.backend.logout_account(i))

    def start(self):
        selection = {'all': self.selection['all'], 'names': list(self.selection['names'])}
        if not selection['all'] and not selection['names']: self.warn('Выберите хотя бы одну коллекцию в разделе «Подарки».'); return
        if self.live.isChecked() and QMessageBox.question(self, 'Отправка сообщений', 'Отправлять предложения владельцам от подключённых аккаунтов? Убедитесь, что получатели согласны получать сообщения.') != QMessageBox.StandardButton.Yes: return
        self.backend.begin(self.live.isChecked(), selection)

    def send_test(self):
        if self.backend.testing or (self.backend.task and not self.backend.task.done()):
            self.warn('Сначала остановите сканирование и текущую отправку.'); return
        if QMessageBox.question(self, 'Пробная отправка', f'Каждый подключённый аккаунт отправит одно сообщение в @{TEST_RECIPIENT}:\n\n{TEST_MESSAGE}') != QMessageBox.StandardButton.Yes: return
        self.backend.submit(self.backend.send_test())

    def refresh_catalog(self):
        async def run():
            if self.backend.task and not self.backend.task.done(): self.backend.log('Перед обновлением каталога остановите сканирование.'); return
            for i, client in sorted(self.backend.clients.items()):
                if i in self.backend.restricted_accounts or i in self.backend.connecting: continue
                if await client.is_user_authorized():
                    try: catalog = await core.watcher.get_gift_catalog(client)
                    except core.PeerFloodError: self.backend.mark_peer_flood(i); return
                    self.backend.logs.put(('catalog', catalog)); self.backend.log(f'Каталог обновлён: {len(catalog)} коллекций'); return
            self.backend.log('Подключите аккаунт, чтобы обновить каталог Telegram.')
        self.backend.submit(run())

    def load_cover(self):
        path = DATA / 'banner.png'
        if not path.exists(): path = core.ROOT / 'assets' / 'banner.png'
        pixmap = QPixmap(str(path)) if path.exists() else QPixmap()
        self.wallpaper.cover = pixmap; self.hero.cover = pixmap; self.wallpaper.update(); self.hero.update()

    def choose_cover(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Выберите обложку', '', 'Изображения (*.png *.jpg *.jpeg *.webp)')
        if not path: return
        try:
            with Image.open(path) as image: source = ImageOps.exif_transpose(image).convert('RGB')
            temporary = DATA / 'banner.tmp.png'; source.save(temporary, format='PNG'); temporary.replace(DATA / 'banner.png')
        except (OSError, ValueError, Image.DecompressionBombError): self.warn('Не удалось открыть или сохранить изображение.'); return
        self.load_cover()

    def update_metrics(self):
        if not hasattr(self, 'metric_values'): return
        self.metric_values[0].setText(str(len(self.connected - self.backend.restricted_accounts)))
        self.metric_values[1].setText(str(len(self.catalog)) if self.selection['all'] and self.catalog else 'Все' if self.selection['all'] else str(len(self.selection['names'])))
        self.metric_values[2].setText(str(getattr(self, 'sent', 0)))
        live = hasattr(self, 'live') and self.live.isChecked(); self.mode_badge.setText('ОТПРАВКА' if live else 'ПРЕДПРОСМОТР'); self.side_status.setText('●  Отправка включена' if live else '●  Предпросмотр')
        scanning = self.backend.task is not None and not self.backend.task.done()
        running = scanning or self.backend.testing
        if hasattr(self, 'live'): self.live.setEnabled(not running)
        if hasattr(self, 'pause_button'):
            self.pause_button.setEnabled(scanning); self.stop_button.setEnabled(running); self.pause_button.setText('Продолжить' if self.backend.paused else 'Пауза'); self.start_button.setEnabled(not running); self.test_button.setEnabled(not running)

    def append_event(self, widget, level, text):
        colors = {'info': '#6c727e', 'offer': '#9a781f', 'success': '#25856b', 'warning': '#b87b25', 'error': '#c95757'}
        scroll = widget.verticalScrollBar(); old = scroll.value(); widget.append(f'<span style="color:{colors.get(level, colors["info"])}">{html.escape(text)}</span>')
        scroll.setValue(scroll.maximum() if self.follow.isChecked() else old)

    def drain(self):
        for _ in range(250):
            try: kind, payload = self.backend.logs.get_nowait()
            except queue.Empty: break
            if kind == 'status':
                i, text = payload; self.accounts.add(i); self.statuses[i] = text
                if text == 'Подключён': self.connected.add(i)
                if i in self.auth_dialogs:
                    self.auth_dialogs[i].status_label.setText(text)
                    if text == 'Подключён': self.auth_dialogs[i].accept()
                self.render_accounts()
            elif kind == 'signed_out':
                for value in self.backend.rows[payload]: value.set('')
                self.accounts.discard(payload); self.connected.discard(payload); self.statuses.pop(payload, None); self.render_accounts()
            elif kind == 'catalog': self.catalog = payload; self.render_gifts()
            elif kind == 'log':
                level, text = payload; self.history.append((level, text))
                for widget in (self.preview_log, self.log_text, getattr(self.detached, 'log_text', None)):
                    if widget is not None: self.append_event(widget, level, text)
                if '[ОТПРАВЛЕНО]' in text: self.sent += 1
        self.update_metrics()

    def open_logs(self):
        if self.detached: self.detached.raise_(); self.detached.activateWindow(); return
        dialog = self.detached = QDialog(self); dialog.setWindowTitle('Telegram Offers • Журнал'); dialog.resize(950, 610)
        box = QVBoxLayout(dialog); box.setContentsMargins(24, 22, 24, 22); row = QHBoxLayout(); row.addWidget(label('Журнал событий', 'pageTitle')); row.addStretch(); row.addWidget(button('Очистить', self.clear_logs)); box.addLayout(row)
        dialog.log_text = QTextEdit(); dialog.log_text.setReadOnly(True); dialog.log_text.document().setMaximumBlockCount(2500); box.addWidget(dialog.log_text)
        for level, text in self.history: self.append_event(dialog.log_text, level, text)
        dialog.finished.connect(lambda result: setattr(self, 'detached', None)); dialog.show()

    def clear_logs(self):
        self.history.clear(); self.preview_log.clear(); self.log_text.clear()
        if self.detached: self.detached.log_text.clear()

    def warn(self, text): QMessageBox.information(self, 'Telegram Offers', text)

    @staticmethod
    def clear_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()

    def closeEvent(self, event):
        if self.closed: event.accept(); return
        event.ignore()
        if self.closing: return
        self.closing = True; self.setEnabled(False); self.shutdown_future = self.backend.shutdown()
        self.close_timer = QTimer(self); self.close_timer.timeout.connect(self.finish_close); self.close_timer.start(50)

    def finish_close(self):
        if not self.shutdown_future.done(): return
        self.timer.stop(); self.close_timer.stop(); self.backend.loop.call_soon_threadsafe(self.backend.loop.stop); self.backend.thread.join(timeout=1); self.closed = True; self.close()


def main():
    app = QApplication.instance() or QApplication(sys.argv); app.setApplicationName('Telegram Offers'); app.setStyle('Fusion'); app.setStyleSheet(STYLE)
    window = Window(); window.show(); return app.exec()


if __name__ == '__main__': sys.exit(main())
