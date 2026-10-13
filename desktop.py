"""Desktop tracker and round-robin offer sender. No group or bot required."""
import asyncio
from collections import deque
import json
from datetime import datetime
import os
from pathlib import Path
import queue
import re
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from PIL import Image, ImageTk, ImageOps, ImageDraw

from telethon import TelegramClient
from telethon.errors import FloodWaitError, SessionPasswordNeededError, ApiIdInvalidError, PeerFloodError
import watcher_core as watcher
from offers import OfferStore, command

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
DATA.mkdir(exist_ok=True)


def read_settings(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def write_settings(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def selected_catalog(catalog, selection):
    if selection['all']:
        return [gift['id'] for gift in catalog]
    names = {watcher._normalize_gift_name(name) for name in selection['names']}
    return [gift['id'] for gift in catalog if watcher._normalize_gift_name(gift['title']) in names]


class App:
    def __init__(self, root):
        self.root = root
        root.title('Telegram Gifts — 10 аккаунтов')
        root.geometry('1180x880')
        root.minsize(1060, 720)
        style = ttk.Style(root)
        style.theme_use('clam')
        ui_family = 'Helvetica' if root.tk.call('tk', 'windowingsystem') == 'aqua' else 'DejaVu Sans'
        self.log_font = ('Menlo', 12) if root.tk.call('tk', 'windowingsystem') == 'aqua' else ('DejaVu Sans Mono', 11)
        style.configure('.', font=(ui_family, 11), background='#152033', foreground='#e2e8f0')
        style.configure('TFrame', background='#152033')
        style.configure('TLabel', padding=3)
        style.configure('Title.TLabel', font=(ui_family, 22, 'bold'), foreground='#5eead4')
        style.configure('Muted.TLabel', foreground='#94a3b8')
        style.configure('Error.TLabel', foreground='#fb7185', font=(ui_family, 11, 'bold'))
        style.configure('TButton', padding=(13, 8), background='#26374e', borderwidth=0)
        style.map('TButton', background=[('active', '#39516e')], foreground=[('disabled', '#64748b')])
        style.configure('Accent.TButton', background='#0f766e', foreground='#ffffff')
        style.map('Accent.TButton', background=[('active', '#115e59')])
        style.configure('TEntry', fieldbackground='#0b1424', foreground='#f1f5f9', padding=5)
        style.configure('TCheckbutton', background='#152033', foreground='#e2e8f0', padding=4)
        style.map('TCheckbutton', background=[('active', '#26374e')])
        style.configure('TLabelframe', background='#152033', bordercolor='#334155')
        style.configure('TLabelframe.Label', foreground='#94a3b8')
        self.install_control_theme(style)
        self.catalog = []
        raw_selection = read_settings(DATA / 'gifts.json', {'all': True, 'names': []})
        self.gift_selection = raw_selection if isinstance(raw_selection, dict) and isinstance(raw_selection.get('all'), bool) and isinstance(raw_selection.get('names'), list) and all(isinstance(name, str) for name in raw_selection['names']) else {'all': True, 'names': []}
        raw_limits = read_settings(DATA / 'account_limits.json', [])
        self.restricted_accounts = {i for i in raw_limits if type(i) is int and 0 <= i < 10} if isinstance(raw_limits, list) else set()
        self.log_history = deque(maxlen=5000)
        self.log_window = None
        self.detached_output = None
        self.banner_source = None
        self.banner_photo = None
        self.banner_resize = None
        root.configure(background='#111111')
        self.background_panels = []
        self.banner = tk.Label(root, background='#111111', borderwidth=0)
        self.banner.place(x=0, y=0, relwidth=1, relheight=1)
        self.banner.lower()
        root.bind('<Configure>', lambda event: self.schedule_banner(event) if event.widget is root else None)
        banner_path = DATA / 'banner.png'
        if not banner_path.exists():
            banner_path = ROOT / 'assets' / 'banner.png'
        if banner_path.exists():
            try:
                with Image.open(banner_path) as image:
                    self.banner_source = ImageOps.exif_transpose(image).convert('RGB')
            except (OSError, ValueError):
                messagebox.showwarning('Баннер', 'Не удалось прочитать обложку. Выберите изображение заново.')
        self.logs = queue.Queue()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.clients = {}
        self.connecting = set()
        self.hashes = {}
        self.task = None
        self.paused = False
        self.api_id = tk.StringVar(value=os.getenv('TG_API_ID', ''))
        self.api_hash = tk.StringVar(value=os.getenv('TG_API_HASH', ''))
        brand = self.background_panel(root)
        brand.pack(fill='x')
        ttk.Label(brand, text='Telegram Offers', style='Title.TLabel').pack(side='left')
        self.gift_summary = tk.StringVar(value=self.selection_summary())
        ttk.Label(brand, textvariable=self.gift_summary, style='Muted.TLabel').pack(side='left', padx=20)
        ttk.Button(brand, text='Подарки', command=self.open_gifts, style='Accent.TButton').pack(side='right', padx=5)
        ttk.Button(brand, text='Окно логов', command=self.open_logs).pack(side='right', padx=5)
        header = self.background_panel(root)
        header.pack(fill='x')
        ttk.Label(header, text='API ID').pack(side='left')
        ttk.Entry(header, textvariable=self.api_id, width=12).pack(side='left')
        ttk.Label(header, text='API Hash').pack(side='left', padx=10)
        ttk.Entry(header, textvariable=self.api_hash, show='*', width=36).pack(side='left')
        ttk.Button(header, text='Выбрать обложку', command=self.choose_banner).pack(side='right')
        self.rows = []
        panel = self.background_panel(root)
        panel.pack(fill='x')
        self.status_labels = []
        for column, text in enumerate(('Аккаунт', 'Телефон', 'Подключение', 'Код', 'Пароль 2FA', '', 'Статус', '')):
            ttk.Label(panel, text=text, style='Muted.TLabel').grid(row=0, column=column, sticky='w', padx=4, pady=(0, 6))
        for i in range(10):
            phone, code, password = tk.StringVar(), tk.StringVar(), tk.StringVar()
            status = tk.StringVar(value='● PeerFloodError' if i in self.restricted_accounts else 'Не подключён')
            self.rows.append((phone, code, password, status))
            ttk.Label(panel, text=f'Аккаунт {i+1}').grid(row=i+1, column=0, padx=5, pady=3)
            ttk.Entry(panel, textvariable=phone, width=18).grid(row=i+1, column=1, pady=3)
            ttk.Button(panel, text='Получить код / сессия', command=lambda n=i: self.connect(n)).grid(row=i+1, column=2, pady=3)
            ttk.Entry(panel, textvariable=code, width=10).grid(row=i+1, column=3, pady=3)
            ttk.Entry(panel, textvariable=password, show='*', width=15).grid(row=i+1, column=4, pady=3)
            ttk.Button(panel, text='Войти', command=lambda n=i: self.login(n)).grid(row=i+1, column=5, pady=3)
            status_label = ttk.Label(panel, textvariable=status, width=23, style='Error.TLabel' if i in self.restricted_accounts else 'TLabel')
            status_label.grid(row=i+1, column=6, padx=6, pady=3)
            self.status_labels.append(status_label)
            ttk.Button(panel, text='Выйти', style='Danger.TButton', command=lambda n=i: self.logout(n)).grid(row=i+1, column=7, padx=4, pady=3)
        ttk.Label(panel, text='Поля: телефон • код Telegram • пароль 2FA (если нужен)').grid(row=11, column=0, columnspan=8, pady=8)
        controls = self.background_panel(root)
        controls.pack(fill='x')
        self.live = tk.BooleanVar(value=False)
        ttk.Checkbutton(controls, text='Отправлять владельцам (выключено = предпросмотр)', variable=self.live).pack(side='left')
        ttk.Button(controls, text='Запустить', command=self.start, style='Accent.TButton').pack(side='left', padx=10)
        ttk.Button(controls, text='Пауза / продолжить', command=lambda: self.loop.call_soon_threadsafe(self.pause)).pack(side='left')
        ttk.Button(controls, text='Остановить', style='Danger.TButton', command=lambda: self.loop.call_soon_threadsafe(self.stop)).pack(side='left', padx=6)
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
                              font=self.log_font, relief='flat', padx=12, pady=10,
                              spacing1=4, spacing3=6)
        scroll = ttk.Scrollbar(body, orient='vertical', command=self.output.yview)
        self.output.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.output.pack(side='left', fill='both', expand=True)
        for tag, color in {'info': '#d1d5db', 'offer': '#7dd3fc', 'success': '#86efac',
                           'warning': '#fcd34d', 'error': '#fca5a5'}.items():
            self.output.tag_configure(tag, foreground=color)
        root.after(100, self.drain)
        root.after_idle(self.draw_banner)
        root.protocol('WM_DELETE_WINDOW', self.close)

    def install_control_theme(self, style):
        # Native ttk handles focus, keyboard activation and widget states;
        # these images replace only the stock square faces and cross indicator.
        self.control_images = []
        def photo(image):
            value = ImageTk.PhotoImage(image)
            self.control_images.append(value)
            return value
        for name, colors in {
            'TButton': ('#253b55', '#345775', '#192b42'),
            'Accent.TButton': ('#0d9488', '#14b8a6', '#0f766e'),
            'Danger.TButton': ('#632b42', '#94364e', '#492033'),
        }.items():
            images = []
            for color in (*colors, '#263041'):
                image = Image.new('RGBA', (64, 42))
                draw = ImageDraw.Draw(image)
                draw.rounded_rectangle((1, 3, 62, 41), radius=11, fill='#080f1c')
                draw.rounded_rectangle((1, 1, 62, 38), radius=11, fill=color, outline='#56718b', width=1)
                draw.line((14, 3, 49, 3), fill='#ffffff30', width=1)
                images.append(photo(image))
            element = 'Offers.' + name + '.face'
            style.element_create(element, 'image', images[0], ('disabled', images[3]),
                                 ('pressed', images[2]), ('active', images[1]),
                                 border=(13, 13, 13, 13), sticky='nsew')
            style.layout(name, [(element, {'sticky': 'nsew', 'children': [
                ('Button.padding', {'sticky': 'nsew', 'children': [
                    ('Button.focus', {'sticky': 'nsew', 'children': [
                        ('Button.label', {'sticky': 'nsew'})]})]})]})])
            style.configure(name, padding=(13, 8), foreground='#f8fafc', borderwidth=0)
            style.map(name, foreground=[('disabled', '#64748b')])
        indicators = []
        for selected, active in ((False, False), (True, False), (False, True)):
            image = Image.new('RGBA', (26, 24))
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((1, 2, 20, 21), radius=5,
                                   fill='#14b8a6' if selected else '#20384a' if active else '#0b1424',
                                   outline='#5eead4' if selected or active else '#64748b', width=1)
            if selected:
                draw.line((5, 11, 9, 15, 16, 7), fill='#ffffff', width=3, joint='curve')
            indicators.append(photo(image))
        style.element_create('Offers.Check.indicator', 'image', indicators[0],
                             ('selected', indicators[1]), ('active', indicators[2]), sticky='w')
        style.layout('TCheckbutton', [('Checkbutton.padding', {'sticky': 'nsew', 'children': [
            ('Offers.Check.indicator', {'side': 'left', 'sticky': 'w'}),
            ('Checkbutton.focus', {'side': 'left', 'sticky': 'w', 'children': [
                ('Checkbutton.label', {'sticky': 'w'})]})]})])

    def selection_summary(self):
        return 'Все коллекции • только Stars' if self.gift_selection['all'] else f"Выбрано: {len(self.gift_selection['names'])} • только Stars"

    def open_gifts(self):
        existing = getattr(self, 'gifts_window', None)
        if existing and existing.winfo_exists():
            existing.lift()
            return
        window = self.gifts_window = tk.Toplevel(self.root)
        window.title('Подарки • выбор коллекций')
        window.geometry('490x620')
        window.configure(background='#152033')
        controls = ttk.Frame(window, padding=12)
        controls.pack(fill='x')
        self.gifts_all = tk.BooleanVar(value=self.gift_selection['all'])
        self.gift_vars = {name: tk.BooleanVar(value=True) for name in self.gift_selection['names']}
        ttk.Checkbutton(controls, text='Все коллекции Telegram', variable=self.gifts_all, command=self.toggle_all_gifts).pack(anchor='w')
        ttk.Button(controls, text='Обновить каталог Telegram', command=self.refresh_catalog).pack(fill='x', pady=8)
        ttk.Label(controls, text='Изменения действуют со следующего запуска.', style='Muted.TLabel').pack(anchor='w')
        ttk.Label(controls, text='Поиск коллекции', style='Muted.TLabel').pack(anchor='w', pady=(8, 0))
        self.gift_search = tk.StringVar()
        search = ttk.Entry(controls, textvariable=self.gift_search)
        search.pack(fill='x', pady=(0, 6))
        self.gift_search.trace_add('write', lambda *args: self.render_gifts())
        canvas = tk.Canvas(window, background='#152033', highlightthickness=0)
        scrollbar = ttk.Scrollbar(window, orient='vertical', command=canvas.yview)
        scrollbar.pack(side='right', fill='y')
        canvas.pack(fill='both', expand=True, padx=12)
        canvas.configure(yscrollcommand=scrollbar.set)
        self.gift_list = ttk.Frame(canvas)
        item = canvas.create_window(0, 0, window=self.gift_list, anchor='nw')
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(item, width=event.width))
        self.gift_list.bind('<Configure>', lambda event: canvas.configure(scrollregion=canvas.bbox('all')))
        ttk.Button(window, text='Сохранить выбор', style='Accent.TButton', command=self.save_gifts).pack(fill='x', padx=12, pady=12)
        self.render_gifts()
        self.refresh_catalog()

    def render_gifts(self):
        window = getattr(self, 'gifts_window', None)
        if not window or not window.winfo_exists():
            return
        names = sorted(set(watcher.GIFT_NAME_WHITELIST) | {gift['title'] for gift in self.catalog} | set(self.gift_vars), key=str.lower)
        for child in self.gift_list.winfo_children():
            child.destroy()
        for name in names:
            if name not in self.gift_vars:
                self.gift_vars[name] = tk.BooleanVar(value=self.gifts_all.get())
            if self.gift_search.get().strip().lower() not in name.lower():
                continue
            ttk.Checkbutton(self.gift_list, text=name, variable=self.gift_vars[name], command=lambda: self.gifts_all.set(False)).pack(anchor='w', fill='x', pady=2)

    def toggle_all_gifts(self):
        for variable in self.gift_vars.values():
            variable.set(self.gifts_all.get())

    def save_gifts(self):
        selection = {'all': self.gifts_all.get(), 'names': sorted(name for name, variable in self.gift_vars.items() if variable.get())}
        try:
            write_settings(DATA / 'gifts.json', selection)
        except OSError:
            messagebox.showerror('Подарки', 'Не удалось сохранить выбор коллекций.')
            return
        self.gift_selection = selection
        self.gift_summary.set(self.selection_summary())
        self.log('Настройки подарков сохранены. Для применения остановите и запустите сканирование.')
        self.gifts_window.destroy()

    def refresh_catalog(self):
        async def run():
            if self.task and not self.task.done():
                self.log('Каталог: остановите сканирование перед обновлением списка')
                return
            for i, client in sorted(self.clients.items()):
                if i in self.restricted_accounts or i in self.connecting:
                    continue
                if await client.is_user_authorized():
                    try:
                        catalog = await watcher.get_gift_catalog(client)
                    except PeerFloodError:
                        self.mark_peer_flood(i)
                        return
                    self.logs.put(('catalog', catalog))
                    self.log(f'Каталог обновлён: {len(catalog)} коллекций')
                    return
            self.log('Каталог: подключите аккаунт. Пока показан сохранённый список коллекций.')
        self.submit(run())

    def open_logs(self):
        if self.log_window and self.log_window.winfo_exists():
            self.log_window.lift()
            return
        window = self.log_window = tk.Toplevel(self.root)
        window.title('Telegram Offers • Журнал событий')
        window.geometry('1050x600')
        controls = ttk.Frame(window, padding=10)
        controls.pack(fill='x')
        ttk.Checkbutton(controls, text='Автопрокрутка', variable=self.follow).pack(side='left')
        ttk.Button(controls, text='Очистить журнал', command=self.clear_log).pack(side='right')
        text = self.detached_output = tk.Text(window, background='#0b1424', foreground='#d1d5db', font=self.log_font, wrap='word', state='disabled', padx=14, pady=12, relief='flat', spacing3=6)
        scrollbar = ttk.Scrollbar(window, command=text.yview)
        scrollbar.pack(side='right', fill='y')
        text.configure(yscrollcommand=scrollbar.set)
        text.pack(fill='both', expand=True)
        for tag in ('info', 'offer', 'success', 'warning', 'error'):
            text.tag_configure(tag, foreground=self.output.tag_cget(tag, 'foreground'))
        for level, line in self.log_history:
            self.append_log(text, level, line)
        def close():
            self.detached_output = None
            self.log_window = None
            window.destroy()
        window.protocol('WM_DELETE_WINDOW', close)

    def append_log(self, widget, level, line):
        widget.configure(state='normal')
        widget.insert('end', line + '\n', level)
        if int(widget.index('end-1c').split('.')[0]) > 5000:
            widget.delete('1.0', '501.0')
        if self.follow.get():
            widget.see('end')
        widget.configure(state='disabled')

    def mark_peer_flood(self, i):
        if not hasattr(self, 'restricted_accounts'):
            self.restricted_accounts = set()
        self.restricted_accounts.add(i)
        self.logs.put(('status', (i, '● PeerFloodError')))
        self.log(f'Ошибка: аккаунт {i+1} — PeerFloodError. Работа остановлена; обход через другие аккаунты не выполняется.')
        try:
            write_settings(DATA / 'account_limits.json', sorted(self.restricted_accounts))
        except OSError:
            self.log('Ошибка: не удалось сохранить отметку ограничения аккаунта')

    def background_panel(self, parent):
        frame = tk.Frame(parent, background='#111111', padx=10, pady=10)
        background = tk.Label(frame, background='#111111', borderwidth=0)
        background.place(x=-10, y=-10, relwidth=1, relheight=1, width=20, height=20)
        background.lower()
        self.background_panels.append((frame, background))
        frame.bind('<Configure>', self.schedule_banner)
        return frame

    def schedule_banner(self, event):
        if self.banner_resize is not None:
            self.root.after_cancel(self.banner_resize)
        self.banner_resize = self.root.after(80, self.draw_banner)

    def choose_banner(self):
        path = filedialog.askopenfilename(title='Выберите обложку Telegram Offers',
                                         filetypes=[('Изображения', '*.png *.jpg *.jpeg *.webp'), ('Все файлы', '*')])
        if not path:
            return
        try:
            with Image.open(path) as image:
                source = ImageOps.exif_transpose(image).convert('RGB')
            temporary = DATA / 'banner.tmp.png'
            source.save(temporary, format='PNG')
            temporary.replace(DATA / 'banner.png')
        except (OSError, ValueError, Image.DecompressionBombError):
            messagebox.showerror('Обложка', 'Не удалось открыть или сохранить изображение. Выберите PNG или JPEG.')
            return
        self.banner_source = source
        self.draw_banner()

    def draw_banner(self):
        self.banner_resize = None
        width = max(1, self.root.winfo_width())
        height = max(1, self.root.winfo_height())
        if self.banner_source is None:
            return
        image = ImageOps.fit(self.banner_source, (width, height), Image.Resampling.LANCZOS)
        image = Image.blend(image, Image.new('RGB', image.size, '#000000'), 0.25)
        self.banner_photo = ImageTk.PhotoImage(image)
        self.banner.configure(image=self.banner_photo)
        for frame, background in self.background_panels:
            x = frame.winfo_rootx() - self.root.winfo_rootx()
            y = frame.winfo_rooty() - self.root.winfo_rooty()
            section = image.crop((x, y, x + frame.winfo_width(), y + frame.winfo_height()))
            background.photo = ImageTk.PhotoImage(section)
            background.configure(image=background.photo)

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
        self.log_history.clear()
        for widget in (self.output, self.detached_output):
            if widget:
                widget.configure(state='normal')
                widget.delete('1.0', 'end')
                widget.configure(state='disabled')

    def drain(self):
        while not self.logs.empty():
            kind, payload = self.logs.get()
            if kind == 'catalog':
                self.catalog = payload
                self.render_gifts()
            elif kind == 'signed_out':
                i = payload
                self.rows[i][1].set('')
                self.rows[i][2].set('')
                self.rows[i][3].set('Не подключён')
                self.status_labels[i].configure(style='TLabel')
            elif kind == 'status':
                i, text = payload
                limited = i in self.restricted_accounts
                self.rows[i][3].set('● PeerFloodError' if limited else text)
                self.status_labels[i].configure(style='Error.TLabel' if limited else 'TLabel')
                self.log(f'Аккаунт {i+1}: {text}')
            else:
                level, line = payload
                self.log_history.append((level, line))
                self.append_log(self.output, level, line)
                if self.detached_output:
                    self.append_log(self.detached_output, level, line)
        self.root.after(100, self.drain)

    def submit(self, coro):
        async def guard():
            try:
                await coro
            except ApiIdInvalidError:
                self.log('Ошибка: Telegram отклонил пару API ID / API Hash. Скопируйте оба значения из одного приложения на my.telegram.org/apps и повторите подключение.')
            except Exception as exc:
                self.log(f'Ошибка: {type(exc).__name__} (проверьте настройки/доступ)')
        asyncio.run_coroutine_threadsafe(guard(), self.loop)

    def connect(self, i):
        try:
            api_id = int(self.api_id.get())
            api_hash = self.api_hash.get().strip()
            if api_id <= 0 or not re.fullmatch(r'[0-9a-fA-F]{32}', api_hash):
                raise ValueError()
        except ValueError:
            messagebox.showerror('Настройки', 'API ID — положительное число. API Hash — 32 символа 0–9 / a–f. Скопируйте оба из одного приложения на my.telegram.org/apps.')
            return
        phone = self.rows[i][0].get().strip()
        async def run():
            if self.task and not self.task.done():
                self.log('Остановите сканирование перед подключением аккаунтов')
                return
            await self.connect_account(i, api_id, api_hash, phone)
        self.submit(run())

    async def connect_account(self, i, api_id, api_hash, phone):
        if i in self.connecting:
            self.log(f'Аккаунт {i+1}: подключение уже выполняется')
            return
        self.connecting.add(i)
        try:
            client = self.clients.get(i)
            if client is not None and (client.api_id != api_id or client.api_hash != api_hash):
                await client.disconnect()
                self.clients.pop(i, None)
                self.hashes.pop(i, None)
                client = None
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
        except ApiIdInvalidError:
            self.hashes.pop(i, None)
            self.logs.put(('status', (i, 'Проверьте API ID / Hash')))
            raise
        except PeerFloodError:
            self.mark_peer_flood(i)
            raise
        finally:
            self.connecting.discard(i)

    def login(self, i):
        code, password = self.rows[i][1].get().strip(), self.rows[i][2].get()
        async def run():
            if i in self.connecting:
                self.log(f'Аккаунт {i+1}: дождитесь завершения текущей операции')
                return
            self.connecting.add(i)
            try:
                client = self.clients.get(i)
                if client is None:
                    self.log(f'Аккаунт {i+1}: сначала нажмите «Получить код / сессия»')
                    return
                if await client.is_user_authorized():
                    return
                if i not in self.hashes:
                    self.log(f'Аккаунт {i+1}: сначала запросите код Telegram')
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
            finally:
                self.connecting.discard(i)
        self.submit(run())

    def logout(self, i):
        if messagebox.askyesno('Выход из аккаунта', f'Завершить сессию аккаунта {i+1}? Сканирование будет остановлено. Для повторного входа потребуется код Telegram.'):
            self.submit(self.logout_account(i))

    async def logout_account(self, i):
        if i in self.connecting:
            self.log(f'Аккаунт {i+1}: дождитесь завершения текущей операции')
            return
        self.connecting.add(i)
        try:
            client = self.clients.get(i)
            if client is None:
                self.log(f'Аккаунт {i+1}: уже отключён')
                return
            if self.task and not self.task.done():
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
            if not await client.log_out():
                self.log(f'Ошибка: не удалось завершить сессию аккаунта {i+1}. Повторите позже.')
                return
            self.clients.pop(i, None)
            self.hashes.pop(i, None)
            if i in getattr(self, 'restricted_accounts', set()):
                self.restricted_accounts.discard(i)
                write_settings(DATA / 'account_limits.json', sorted(self.restricted_accounts))
            self.logs.put(('signed_out', i))
            self.log(f'Аккаунт {i+1}: выход выполнен. История подарков сохранена.')
        finally:
            self.connecting.discard(i)

    def start(self):
        live = self.live.get()
        selection = {'all': self.gift_selection['all'], 'names': list(self.gift_selection['names'])}
        if not selection['all'] and not selection['names']:
            messagebox.showwarning('Подарки', 'Выберите хотя бы одну коллекцию через кнопку «Подарки».')
            return
        if live and not messagebox.askyesno('Отправка', 'Отправлять команды владельцам подарков от подключённых аккаунтов? Убедитесь, что получатели согласны получать сообщения.'):
            return
        async def run():
            if self.task and not self.task.done():
                self.log('Уже запущено; для изменения режима остановите и запустите снова')
                return
            self.paused = False
            self.task = asyncio.create_task(self.scan(live, selection))
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

    async def scan(self, live, selection=None):
        clients = {i: c for i, c in sorted(self.clients.items()) if i not in getattr(self, 'restricted_accounts', set()) and i not in getattr(self, 'connecting', set()) and await c.is_user_authorized()}
        if not clients:
            self.log('Сначала подключите аккаунт')
            return
        primary = next(iter(clients.values()))
        current_slot = next(iter(clients))
        store = OfferStore(DATA / ('offers.sqlite' if live else 'preview.sqlite'))
        seen = store.known_slugs()
        warming_up = True
        self.log('Режим: ' + ('ОТПРАВКА' if live else 'ПРЕДПРОСМОТР — сообщения не отправляются'))
        try:
            if selection is not None:
                catalog = await watcher.get_gift_catalog(primary)
                ids = selected_catalog(catalog, selection)
                self.logs.put(('catalog', catalog))
                if not selection['all']:
                    found = {watcher._normalize_gift_name(gift['title']) for gift in catalog}
                    missing = [name for name in selection['names'] if watcher._normalize_gift_name(name) not in found]
                    if missing:
                        self.log('Внимание: коллекции не найдены в каталоге Telegram: ' + ', '.join(missing))
            elif watcher.GIFT_NAME_WHITELIST:
                ids = await watcher.get_gift_ids_by_names(primary, watcher.GIFT_NAME_WHITELIST)
            elif watcher.MANUAL_GIFT_IDS:
                ids = watcher.MANUAL_GIFT_IDS
            elif watcher.TRACK_ALL_COLLECTIONS:
                ids = await watcher.get_all_gift_ids(primary)
            else:
                ids = await watcher.get_newest_gift_ids(primary, watcher.NUM_NEWEST_COLLECTIONS)
            if not ids:
                self.log('Ошибка: не найдено коллекций для сканирования')
                return
            self.log(f'Коллекций: {len(ids)}; аккаунтов: {len(clients)}')
            self.log(f'В сохранённой истории: {len(seen)} подарков')
            self.log('Первый проход: запоминаю текущие подарки, без предложений и отправки')
            while True:
                while self.paused:
                    await asyncio.sleep(0.2)
                listings = []
                # check_gift marks discoveries locally; failed owner lookups must be retryable.
                pass_seen = set(seen)
                stats = dict(total_seen_in_responses=0, new_slugs=0, filtered_by_name=0, filtered_names_sample=[], errors=0)
                for index, gift_id in enumerate(ids):
                    while self.paused:
                        await asyncio.sleep(0.2)
                    current_slot = list(clients)[index % len(clients)]
                    client = clients[current_slot]
                    await watcher.check_gift(client, gift_id, pass_seen, listings, stats)
                    await asyncio.sleep(watcher.REQUEST_DELAY)
                self.log(f"Рынок: получено {stats['total_seen_in_responses']} лотов • доступны за Stars {stats.get('stars_listings', 0)} • уже известны {stats.get('known_gifts', 0)} • исключены по валюте {stats.get('filtered_currency', 0)} • без ссылки {stats.get('missing_slug', 0)} • новых {len(listings)} • ошибок {stats['errors']}")
                if stats.get('price_types'):
                    self.log('Типы цен Telegram: ' + ', '.join(f'{name}: {count}' for name, count in sorted(stats['price_types'].items())))
                if stats['total_seen_in_responses'] == 0 and not stats['errors']:
                    self.log('Внимание: Telegram вернул пустые списки лотов по выбранным коллекциям. Это не означает, что весь рынок пуст.')
                if warming_up:
                    baseline = [listing['slug'] for listing in listings]
                    store.remember_baseline(baseline)
                    seen.update(baseline)
                    if stats['errors']:
                        self.log(f"Прогрев: запомнено {len(baseline)} подарков; ошибок {stats['errors']}. Отправка выключена до полного прохода без ошибок.")
                    else:
                        warming_up = False
                        self.log(f"Первый проход завершён: добавлено в историю {len(baseline)} новых подарков; доступны за Stars {stats.get('stars_listings', 0)}, уже известны {stats.get('known_gifts', 0)}. Следующие проходы обрабатывают только новые находки.")
                    await asyncio.sleep(watcher.POLL_INTERVAL)
                    continue
                listings = [listing for listing in listings if not store.seen_owner(getattr(listing['owner_id'], 'user_id', None))]
                current_slot = next(iter(clients))
                info = await watcher.resolve_owner_info(primary, listings) if listings else {}
                for listing in listings:
                    while self.paused:
                        await asyncio.sleep(0.2)
                    uid = getattr(listing['owner_id'], 'user_id', None)
                    details = info.get(uid, {})
                    recipient = details.get('username')
                    if not recipient or (watcher.EXCLUDE_PAID_MESSAGE_SELLERS and details.get('paid') is not False):
                        continue
                    if not watcher.looks_cjk_anywhere(listing['owner_name'], details.get('about', '')):
                        level = details.get('level')
                        if level is not None and level > watcher.MAX_SELLER_LEVEL:
                            continue
                    try:
                        text = command(listing.get('title'), listing.get('num'), listing['price'])
                    except ValueError:
                        self.log(f"Пропуск {listing['slug']}: требуется цена только в Stars, название и номер подарка")
                        continue
                    original = int(re.search(r'(?:^| / )(\d+) XTR', listing['price']).group(1))
                    if watcher.MIN_PRICE_STARS and original < watcher.MIN_PRICE_STARS:
                        continue
                    if watcher.MAX_PRICE_STARS and original > watcher.MAX_PRICE_STARS:
                        continue
                    slot = store.reserve(listing['slug'], recipient, list(clients), text, owner_id=uid)
                    seen.add(listing['slug'])
                    if slot is None:
                        continue
                    self.log(f'Аккаунт {slot+1} → @{recipient}: {text}')
                    if not live:
                        store.status(listing['slug'], 'preview')
                        continue
                    try:
                        await clients[slot].send_message(recipient, text, parse_mode=None)
                    except PeerFloodError:
                        store.status(listing['slug'], 'blocked')
                        self.mark_peer_flood(slot)
                        return
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
        except PeerFloodError:
            self.mark_peer_flood(current_slot)
        except FloodWaitError as exc:
            self.log(f'Telegram ограничил запросы на {exc.seconds} сек. Сканирование остановлено.')
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
