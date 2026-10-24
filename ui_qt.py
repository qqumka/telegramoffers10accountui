"""Qt desktop studio for manual NFT offers and a shared persistent queue."""
import asyncio
from collections import deque
import html
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF, QPropertyAnimation
from PySide6.QtGui import QColor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QFrame, QLabel,
    QPushButton, QVBoxLayout, QHBoxLayout, QGridLayout, QLineEdit, QCheckBox,
    QScrollArea, QTextEdit, QDialog, QFileDialog, QMessageBox, QStackedWidget,
    QGraphicsDropShadowEffect, QGraphicsOpacityEffect, QButtonGroup, QTableWidget,
    QTableWidgetItem, QHeaderView, QComboBox, QAbstractItemView)
import bot_core as core

DATA = core.DATA
from runtime import Backend
from manual_queue import render


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
        self.eyebrow = QLabel('TELEGRAM OFFERS  /  РУЧНАЯ ОЧЕРЕДЬ NFT'); self.eyebrow.setObjectName('heroSmall')
        self.title = QLabel('Ваш список.\nТочные предложения.'); self.title.setObjectName('heroTitle')
        self.note = QLabel('Одна цена для списка. Проверка каждой строки. Отправка под вашим контролем.'); self.note.setObjectName('heroNote')
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


STATUS = {'pending': 'Ожидает', 'sending': 'Отправляется', 'sent': 'Отправлено', 'failed': 'Не отправлено', 'uncertain': 'Результат не подтверждён'}
STYLE += '''
QTableWidget { background: #ffffff; border: 1px solid #e5e6e9; border-radius: 10px; gridline-color: #eceef1; selection-background-color: #faf1d6; selection-color: #292c32; }
QHeaderView::section { background: #f5f6f8; padding: 10px; border: none; color: #777d89; font-weight: 600; }
QTableWidget::item { padding: 7px; }
QPushButton[role="primary"]:disabled, QPushButton[role="danger"]:disabled { color: #b2b5bd; background: #f2f3f4; border-color: #e9eaed; }
QComboBox { border: 1px solid #e1e4e9; border-radius: 8px; padding: 7px; background: white; }
QTextEdit#input { background: #f7f8fa; border: 1px solid #e1e4e9; border-radius: 12px; padding: 12px; }
'''


class Window(QMainWindow):
    def __init__(self, backend=None):
        super().__init__()
        self.setWindowTitle('Telegram Offers · Списки и очередь')
        self.resize(1280, 920); self.setMinimumSize(1060, 740)
        self.backend = backend or Backend()
        self.state = {}; self.draft_signature = None; self.dirty = False
        self.auth_dialogs = {}; self.closing = self.closed = False; self.futures = []
        shell = QWidget(); layout = QHBoxLayout(shell); layout.setContentsMargins(0,0,0,0); layout.setSpacing(0)
        self.setCentralWidget(shell)
        sidebar = QFrame(); sidebar.setObjectName('sidebar'); sidebar.setFixedWidth(210)
        side = QVBoxLayout(sidebar); side.setContentsMargins(22,28,22,24); side.setSpacing(8)
        side.addWidget(label('Telegram\nOffers','brand')); side.addWidget(label('СПИСКИ · ПРОВЕРКА · ОТПРАВКА','sideNote')); side.addSpacing(24)
        self.stack = QStackedWidget(); self.nav_group = QButtonGroup(self); self.nav_group.setExclusive(True)
        for i,(name,symbol) in enumerate((('Подготовка','overview'),('Очередь','play'),('Аккаунты','accounts'),('История','gifts'),('Логи','logs'),('Настройки','settings'))):
            item = button(name,lambda index=i: self.navigate(index),'nav')
            item.setIcon(icon(symbol,'#a5a8b2')); item.setCheckable(True); side.addWidget(item); self.nav_group.addButton(item,i)
        side.addStretch(); self.side_status = label('●  Загрузка…','sideNote'); side.addWidget(self.side_status)
        side.addWidget(label('До 10 аккаунтов\nОбщий интервал: 15 секунд','sideNote')); layout.addWidget(sidebar)
        self.wallpaper = Wallpaper(); box = QVBoxLayout(self.wallpaper); box.setContentsMargins(24,22,24,16); box.addWidget(self.stack)
        self.notice = label('Вставьте результаты парсера и задайте итоговую цену.','muted'); box.addWidget(self.notice)
        layout.addWidget(self.wallpaper,1)
        self.create_prepare(); self.create_queue(); self.create_accounts(); self.create_history(); self.create_logs(); self.create_settings()
        self.navigate(0); self.load_cover()
        self.timer = QTimer(self); self.timer.timeout.connect(self.drain); self.timer.start(100)

    def page(self,title,subtitle):
        page = QWidget(); box = QVBoxLayout(page); box.setContentsMargins(0,0,0,0); box.setSpacing(14)
        box.addWidget(label(title,'pageTitle')); box.addWidget(label(subtitle,'subtitle')); self.stack.addWidget(page)
        return box

    def navigate(self,index):
        self.stack.setCurrentIndex(index); self.nav_group.button(index).setChecked(True)

    def table(self, headers, editable=False):
        table = QTableWidget(0,len(headers)); table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False); table.verticalHeader().setDefaultSectionSize(43)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setWordWrap(False); table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        if not editable: table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        return table

    def create_prepare(self):
        box = self.page('Подготовка сообщений','Вставьте строки @username - ссылка NFT и задайте общую цену. Проверьте каждое сообщение перед подтверждением.')
        self.hero = Hero(); self.hero.setMinimumHeight(150); box.addWidget(self.hero)
        panel, inner = card()
        self.input_text = QTextEdit(); self.input_text.setObjectName('input'); self.input_text.setAcceptRichText(False)
        self.input_text.setPlaceholderText('@Anufreya - https://t.me/nft/ScaredCat-1775\n@paipaiqi - https://t.me/nft/ScaredCat-188\n\nТакже поддерживаются результаты парсера и ручные строки с ценой.')
        self.input_text.setMinimumHeight(100); self.input_text.setMaximumHeight(160); inner.addWidget(self.input_text)
        row = QHBoxLayout(); self.price = QLineEdit(); self.price.setPlaceholderText('Общая итоговая цена: 425 gram'); row.addWidget(self.price,1)
        self.import_button = button('Разобрать список',self.import_list,'primary'); row.addWidget(self.import_button)
        row.addWidget(button('Применить цену',self.apply_price)); inner.addLayout(row)
        self.import_hint = label('Цена применяется к каждой строке. Пометка (+1) не добавляет NFT без номера.','muted'); inner.addWidget(self.import_hint); box.addWidget(panel)
        self.draft_title = label('Черновик · 0 получателей'); box.addWidget(self.draft_title)
        self.draft_table = self.table(['Получатель','NFT','Номер','Цена, gram','Язык','Сообщение'],True)
        for col,width in enumerate((145,145,80,100,85)):
            self.draft_table.horizontalHeader().setSectionResizeMode(col,QHeaderView.ResizeMode.Interactive)
            self.draft_table.setColumnWidth(col,width)
        self.draft_table.setMinimumHeight(150); self.draft_table.itemChanged.connect(self.draft_changed)
        box.addWidget(self.draft_table,1)
        controls = QHBoxLayout(); self.cancel_button = button('Удалить черновик',self.cancel_draft,'danger'); controls.addWidget(self.cancel_button)
        controls.addStretch(); self.save_button = button('Сохранить правки',self.save_draft); controls.addWidget(self.save_button)
        self.enqueue_button = button('Подтвердить → в очередь',self.enqueue,'primary'); controls.addWidget(self.enqueue_button); box.addLayout(controls)

    def create_queue(self):
        box = self.page('Очередь отправки','Одна очередь для всех аккаунтов. После остановки или перезапуска оставшиеся строки сохраняются.')
        row = QHBoxLayout(); self.queue_summary = label('Очередь пуста','badge'); row.addWidget(self.queue_summary); row.addStretch()
        self.start_button = button('Начать отправку',lambda: self.action('command','/run'),'primary','play')
        self.pause_button = button('Пауза',lambda: self.action('command','/pause'),symbol='pause')
        self.stop_button = button('Стоп',lambda: self.action('command','/stop'),symbol='stop')
        for item in (self.start_button,self.pause_button,self.stop_button): row.addWidget(item)
        box.addLayout(row); box.addWidget(label('Общий интервал ≥ 15 секунд · аккаунты по кругу · повторные отправки исключены','muted'))
        self.queue_table = self.table(['№','Получатель','Сообщение','Статус','Аккаунт'])
        for col in (0,1,3,4): self.queue_table.horizontalHeader().setSectionResizeMode(col,QHeaderView.ResizeMode.ResizeToContents)
        box.addWidget(self.queue_table,1)
        box.addWidget(label('Показаны последние 500 записей. Неподтверждённые результаты и ошибки не отправляются повторно автоматически.','muted'))

    def create_accounts(self):
        box = self.page('Аккаунты','Подключите сохранённые сессии или добавьте новый аккаунт. До 10 слотов для отправки по кругу.')
        row = QHBoxLayout(); row.addWidget(button('Добавить аккаунт',self.add_account,'primary','plus'))
        row.addWidget(button('Подключить сохранённые',lambda: self.action('connect_all',self.api_id.text(),self.api_hash.text()))); row.addStretch(); box.addLayout(row)
        self.account_table = self.table(['Слот','Состояние','Подключение','Действие']); box.addWidget(self.account_table,1)
        box.addWidget(label('API ID и API Hash вводятся в настройках. Коды и пароль 2FA — только в окне входа.','muted'))

    def create_history(self):
        box = self.page('История получателей','Общая история desktop и Telegram-бота. Проверка по Telegram ID и username, включая старую базу.')
        self.history_table = self.table(['Получатель','Telegram ID / ключ истории']); box.addWidget(self.history_table,1)
        box.addWidget(label('История сохраняется в data. Уже обработанные получатели не добавляются повторно, даже с другим NFT. Показаны последние 500 получателей.','muted'))

    def create_logs(self):
        box = self.page('Логи','Постоянный журнал загрузки списков, отправок, остановок и ошибок.')
        self.log_text = QTextEdit(); self.log_text.setReadOnly(True); box.addWidget(self.log_text,1)
        box.addWidget(label('Последние 250 событий. Полный журнал сохраняется в data/offers.sqlite.','muted'))

    def create_settings(self):
        box = self.page('Настройки','Подключение Telegram и оформление программы.')
        panel, inner = card(); inner.addWidget(label('Telegram API')); inner.addWidget(label('API ID и API Hash одного приложения с my.telegram.org/apps.','muted'))
        row = QHBoxLayout(); self.api_id = QLineEdit(os.getenv('TG_API_ID','')); self.api_id.setPlaceholderText('API ID')
        self.api_hash = QLineEdit(os.getenv('TG_API_HASH','')); self.api_hash.setPlaceholderText('API Hash'); self.api_hash.setEchoMode(QLineEdit.EchoMode.Password)
        row.addWidget(self.api_id,1); row.addWidget(self.api_hash,2); inner.addLayout(row)
        inner.addWidget(label('Ключи хранятся только в памяти окна. Сессии сохраняются в data.','muted')); box.addWidget(panel)
        panel,inner = card(); inner.addWidget(label('Ваша обложка')); inner.addWidget(button('Выбрать изображение',self.choose_cover)); box.addWidget(panel)
        box.addWidget(label('Для переключения между программой и Telegram-ботом завершите текущий процесс. Оба используют одну очередь и историю.','muted')); box.addStretch()

    def action(self, action, *args):
        future = self.backend.submit(action,*args); self.futures.append(future)
        self.notice.setText('Выполняется…'); return future

    def price_text(self):
        value = self.price.text().strip()
        return value if not value or value.lower().endswith('gram') else value+' gram'

    def import_list(self):
        if not self.input_text.toPlainText().strip(): self.warn('Вставьте список: @username - https://t.me/nft/ScaredCat-1775'); return
        self.action('import',self.input_text.toPlainText(),self.price_text())

    def apply_price(self):
        if self.dirty: self.warn('Сначала сохраните правки таблицы.'); return
        self.action('price',self.price_text())

    def draft_rows(self):
        rows = []
        for row in range(self.draft_table.rowCount()):
            values = [self.draft_table.item(row,col).text().strip() for col in range(4)]
            rows.append(dict(zip(('recipient','title','number','price','lang'),values+[self.draft_table.cellWidget(row,4).currentText()])))
        return rows

    def save_draft(self): self.action('save',self.draft_rows())
    def enqueue(self): self.action('save',self.draft_rows(),True)

    def cancel_draft(self):
        if QMessageBox.question(self,'Удалить черновик','Удалить текущий черновик? Очередь и история сохранятся.') == QMessageBox.StandardButton.Yes:
            self.action('command','/cancel')

    def draft_changed(self,*args):
        self.dirty = True
        for row,item in enumerate(self.draft_rows()):
            self.draft_table.blockSignals(True)
            self.draft_table.item(row,5).setText(render(item))
            self.draft_table.blockSignals(False)
        self.draft_title.setText(f'Черновик · {self.draft_table.rowCount()} получателей · есть несохранённые правки')

    def render_draft(self,items):
        self.draft_table.blockSignals(True); self.draft_table.setRowCount(len(items))
        for row,item in enumerate(items):
            for col,key in enumerate(('recipient','title','number','price')):
                self.draft_table.setItem(row,col,QTableWidgetItem(item[key]))
            combo = QComboBox(); combo.addItems(['ru','cn','ar']); combo.setCurrentText(item['lang']); self.draft_table.setCellWidget(row,4,combo)
            preview = QTableWidgetItem(render(item)); preview.setFlags(preview.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.draft_table.setItem(row,5,preview)
            combo.currentTextChanged.connect(self.draft_changed)
        self.draft_table.blockSignals(False); self.dirty = False
        self.draft_title.setText(f'Черновик · {len(items)} получателей')

    def fill_table(self,table,rows):
        table.setRowCount(len(rows))
        for r,row in enumerate(rows):
            for c,value in enumerate(row):
                item = QTableWidgetItem(str(value or '—')); item.setToolTip(str(value or '—')); table.setItem(r,c,item)

    def show_state(self,data):
        self.state = data
        signature = repr(data['draft'])
        if signature != self.draft_signature:
            self.draft_signature = signature; self.render_draft(data['draft'])
        pending = data['pending']
        self.import_hint.setText(f'Извлечено {pending} получателей. Укажите общую итоговую цену и нажмите «Применить цену».' if pending else 'Цена применяется к каждой строке. Язык можно изменить в таблице: cn / ar / ru.')
        self.enqueue_button.setEnabled(bool(data['draft']) and not pending)
        self.save_button.setEnabled(bool(data['draft'])); self.import_button.setEnabled(not data['draft'] and not pending)
        self.cancel_button.setEnabled(bool(data['draft']) or bool(pending))
        running = data['running']; self.start_button.setEnabled(not running and data['counts'].get('pending',0) > 0)
        self.pause_button.setEnabled(running); self.stop_button.setEnabled(running)
        self.pause_button.setText('Продолжить' if data['paused'] else 'Пауза')
        self.side_status.setText('●  Пауза' if running and data['paused'] else '●  Отправка' if running else '●  Готов к работе')
        self.queue_summary.setText(' · '.join(f'{STATUS.get(status,status)}: {count}' for status,count in data['counts'].items()) or 'Очередь пуста')
        self.fill_table(self.queue_table,[(i,'@'+recipient,text,STATUS.get(status,status),str(slot+1) if slot is not None else '—') for i,recipient,text,status,slot in data['queue']])
        self.fill_table(self.history_table,[('@'+recipient,key) for key,recipient in data['history']])
        self.log_text.setPlainText('\n'.join(time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(stamp))+'  '+message for stamp,message in data['logs']))
        self.log_text.verticalScrollBar().setValue(self.log_text.verticalScrollBar().maximum())
        self.account_table.setRowCount(len(data['accounts']))
        for row,item in enumerate(data['accounts']):
            slot = item['slot']
            self.account_table.setItem(row,0,QTableWidgetItem(str(slot+1)))
            self.account_table.setItem(row,1,QTableWidgetItem(item['status']))
            self.account_table.setCellWidget(row,2,button('Подключён' if item['connected'] else 'Подключить / войти',lambda i=slot: self.open_auth(i)))
            self.account_table.setCellWidget(row,3,button('Выйти',lambda i=slot: self.logout(i),'danger'))
            if slot in self.auth_dialogs: self.auth_dialogs[slot].status_label.setText(item['status'])

    def add_account(self):
        occupied = {item['slot'] for item in self.state.get('accounts',[])} | set(self.auth_dialogs)
        slot = next((i for i in range(10) if i not in occupied),None)
        if slot is None: self.warn('Все 10 слотов заняты.'); return
        self.open_auth(slot)

    def open_auth(self,slot):
        if slot in self.auth_dialogs: self.auth_dialogs[slot].raise_(); return
        dialog = QDialog(self); dialog.setWindowTitle(f'Аккаунт {slot+1} · Telegram'); dialog.resize(440,420)
        box = QVBoxLayout(dialog); box.setContentsMargins(24,24,24,24); box.setSpacing(14)
        box.addWidget(label('Вход в Telegram','pageTitle'))
        phone = QLineEdit(); phone.setPlaceholderText('Телефон с кодом страны')
        code = QLineEdit(); code.setPlaceholderText('Код Telegram'); code.setEchoMode(QLineEdit.EchoMode.Password)
        password = QLineEdit(); password.setPlaceholderText('Пароль 2FA'); password.setEchoMode(QLineEdit.EchoMode.Password)
        box.addWidget(phone); box.addWidget(button('Получить код / подключить сессию',lambda: self.action('connect',slot,self.api_id.text(),self.api_hash.text(),phone.text())))
        box.addWidget(code); box.addWidget(password)
        status = label('Введите API ID и Hash в настройках.','muted'); box.addWidget(status); dialog.status_label = status
        box.addWidget(button('Войти',lambda: self.action('login',slot,code.text(),password.text()),'primary'))
        dialog.finished.connect(lambda result: self.auth_dialogs.pop(slot,None)); self.auth_dialogs[slot] = dialog; dialog.show()

    def logout(self,slot):
        if QMessageBox.question(self,'Выход из аккаунта','Завершить сессию Telegram? Очередь будет остановлена и сохранена.') == QMessageBox.StandardButton.Yes:
            self.action('logout',slot)

    def load_cover(self):
        cover = QPixmap(str(DATA / 'banner.png'))
        self.wallpaper.cover = cover; self.hero.cover = cover; self.wallpaper.update(); self.hero.update()

    def choose_cover(self):
        path,_ = QFileDialog.getOpenFileName(self,'Выбрать обложку','','Изображения (*.png *.jpg *.jpeg *.webp)')
        if not path: return
        image = QPixmap(path)
        if image.isNull() or not image.save(str(DATA / 'banner.png'),'PNG'): self.warn('Не удалось сохранить изображение.'); return
        self.load_cover()

    def drain(self):
        self.futures = [future for future in self.futures if not future.done()]
        while True:
            try: kind,payload = self.backend.events.get_nowait()
            except queue.Empty: break
            if kind == 'snapshot': self.show_state(payload)
            elif kind == 'notice':
                if payload == 'Правки сохранены.':
                    self.dirty = False
                    self.draft_title.setText(f'Черновик · {self.draft_table.rowCount()} получателей')
                if len(payload) < 250: self.notice.setText(payload)
                elif 'Исправить язык' not in payload: self.notice.setText('Черновик обновлён. Проверьте таблицу перед подтверждением.')
            elif kind == 'error': self.warn(payload)

    def warn(self,text): QMessageBox.information(self,'Telegram Offers',text)

    def closeEvent(self,event):
        if self.closed: event.accept(); return
        event.ignore()
        if self.closing: return
        self.closing = True; self.setEnabled(False); self.shutdown_future = self.backend.shutdown()
        self.close_timer = QTimer(self); self.close_timer.timeout.connect(self.finish_close); self.close_timer.start(50)

    def finish_close(self):
        if not self.shutdown_future.done(): return
        self.timer.stop(); self.close_timer.stop(); self.backend.loop.call_soon_threadsafe(self.backend.loop.stop)
        self.backend.thread.join(timeout=1); self.closed = True; self.close()


def main():
    from bot_runner import process_lock
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName('Telegram Offers'); app.setStyle('Fusion'); app.setStyleSheet(STYLE)
    core.DATA.chmod(0o700)
    try:
        with process_lock():
            window = Window(); window.show(); return app.exec()
    except RuntimeError as exc:
        QMessageBox.information(None,'Telegram Offers',str(exc)+'\nОстановите Telegram-бота или другое окно программы.')
        return 1


if __name__ == '__main__': sys.exit(main())
