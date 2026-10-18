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
from tkinter import font as tkfont
from PIL import Image, ImageTk, ImageOps, ImageDraw

from telethon import TelegramClient
from telethon.errors import FloodWaitError, SessionPasswordNeededError, ApiIdInvalidError, PeerFloodError, UserPrivacyRestrictedError, UserIsBlockedError
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


class AccountScanError(Exception):
    def __init__(self, slot, cause):
        self.slot, self.cause = slot, cause
        super().__init__(f'Account {slot+1}: {type(cause).__name__}')


class WallpaperLabel(tk.Canvas):
    """Draw text over the matching wallpaper crop instead of an opaque label."""
    def __init__(self, app, parent, text='', textvariable=None, style='TLabel', padding=3):
        self.app, self.text, self.variable, self.label_style = app, text, textvariable, style
        self.padding = padding
        self.label_font = tkfont.Font(font=ttk.Style(parent).lookup(style, 'font'))
        value = textvariable.get() if textvariable is not None else text
        super().__init__(parent, width=self.label_font.measure(value) + 2*padding,
                         height=self.label_font.metrics('linespace') + 2*padding,
                         background='#e9ecef', borderwidth=0, highlightthickness=0)
        self.photo = None
        self.bind('<Configure>', lambda event: self.redraw())
        self.trace = textvariable.trace_add('write', self.text_changed) if textvariable is not None else None
        self.bind('<Destroy>', self.cleanup)
        app.wallpaper_labels.append(self)

    def cleanup(self, event):
        if event.widget is self and self.trace:
            self.variable.trace_remove('write', self.trace)
            self.trace = None

    def text_changed(self, *args):
        value = self.variable.get() if self.variable is not None else self.text
        super().configure(width=self.label_font.measure(value) + 2*self.padding)
        self.redraw()

    def configure(self, cnf=None, **options):
        style = options.pop('style', None)
        if style is not None:
            self.label_style = style
            self.label_font.configure(**tkfont.Font(font=ttk.Style(self).lookup(style, 'font')).actual())
        result = super().configure(cnf, **options)
        if style is not None:
            self.text_changed()
        return result

    def cget(self, key):
        return self.label_style if key == 'style' else super().cget(key)

    def redraw(self):
        self.delete('all')
        image = getattr(self.app, '_wallpaper_image', None)
        if image is not None:
            x = self.winfo_rootx() - self.app.root.winfo_rootx()
            y = self.winfo_rooty() - self.app.root.winfo_rooty()
            self.photo = ImageTk.PhotoImage(image.crop((x, y, x+self.winfo_width(), y+self.winfo_height())))
            self.create_image(0, 0, image=self.photo, anchor='nw')
        value = self.variable.get() if self.variable is not None else self.text
        color = '#d94c4c' if self.label_style == 'Error.TLabel' else '#555b62' if self.label_style == 'Muted.TLabel' else '#202428'
        self.create_text(self.padding, self.winfo_height()/2, text=value, anchor='w', fill=color, font=self.label_font, tags='caption')


class WallpaperCheck(WallpaperLabel):
    def __init__(self, app, parent, text, variable, command=None):
        self.checked, self.command = variable, command
        super().__init__(app, parent, text=text)
        super().configure(width=self.label_font.measure(text)+38, takefocus=True)
        self.check_trace = variable.trace_add('write', lambda *args: self.redraw())
        self.bind('<Button-1>', lambda event: self.invoke())
        self.bind('<space>', lambda event: self.invoke())
        self.bind('<FocusIn>', lambda event: self.redraw())
        self.bind('<FocusOut>', lambda event: self.redraw())

    def cleanup(self, event):
        super().cleanup(event)
        if event.widget is self and getattr(self, 'check_trace', None):
            self.checked.trace_remove('write', self.check_trace)
            self.check_trace = None

    def invoke(self):
        self.focus_set()
        self.checked.set(not self.checked.get())
        if self.command:
            return self.command()

    def redraw(self):
        super().redraw()
        self.move('caption', 28, 0)
        y = self.winfo_height()/2
        self.create_rectangle(3, y-9, 21, y+9, fill='#f4cf4e' if self.checked.get() else '#e9ecef', outline='#b59a42')
        if self.checked.get():
            self.create_line(6, y, 10, y+4, 18, y-5, fill='#25282b', width=2)
        if self.focus_get() is self:
            self.create_rectangle(1, 1, self.winfo_width()-2, self.winfo_height()-2, outline='#6b7178', dash=(2, 2))


class App:
    def __init__(self, root):
        self.root = root
        root.title('Telegram Gifts — 10 аккаунтов')
        root.geometry('1080x760')
        root.minsize(1000, 640)
        style = ttk.Style(root)
        style.theme_use('clam')
        ui_family = 'Helvetica' if root.tk.call('tk', 'windowingsystem') == 'aqua' else 'DejaVu Sans'
        self.log_font = ('Menlo', 12) if root.tk.call('tk', 'windowingsystem') == 'aqua' else ('DejaVu Sans Mono', 11)
        style.configure('.', font=(ui_family, 11), background='#e9ecef', foreground='#25282b')
        style.configure('TFrame', background='#e9ecef')
        style.configure('TLabel', padding=3)
        style.configure('Title.TLabel', font=(ui_family, 22, 'bold'), foreground='#25282b')
        style.configure('Muted.TLabel', foreground='#61666c')
        style.configure('Error.TLabel', foreground='#d94c4c', font=(ui_family, 11, 'bold'))
        style.configure('TButton', padding=(13, 8), background='#edf4fa', borderwidth=0)
        style.map('TButton', background=[('active', '#deedf9')], foreground=[('disabled', '#64748b')])
        style.configure('Accent.TButton', background='#2481cc', foreground='#ffffff')
        style.map('Accent.TButton', background=[('active', '#1d6fb4')])
        style.configure('TEntry', fieldbackground='#f7faff', foreground='#25282b', padding=7, bordercolor='#dbe5ee', lightcolor='#dbe5ee', darkcolor='#dbe5ee')
        style.configure('TCheckbutton', background='#e9ecef', foreground='#25282b', padding=4)
        style.map('TCheckbutton', background=[('active', '#edf4fa')])
        style.configure('TLabelframe', background='#e9ecef', bordercolor='#dbe5ee')
        style.configure('TLabelframe.Label', foreground='#61666c')
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
        root.configure(background='#edf3f8')
        self.background_panels = []
        self.wallpaper_labels = []
        self.wallpaper_canvases = []
        self._wallpaper_image = None
        self.banner = tk.Label(root, background='#edf3f8', borderwidth=0)
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
        self.wallpaper_label(brand, text='Telegram Offers', style='Title.TLabel').pack(side='left')
        self.gift_summary = tk.StringVar(value=self.selection_summary())
        self.wallpaper_label(brand, textvariable=self.gift_summary, style='Muted.TLabel').pack(side='left', padx=20)
        ttk.Button(brand, text='Подарки', command=self.open_gifts, style='Accent.TButton').pack(side='right', padx=5)
        ttk.Button(brand, text='Окно логов', command=self.open_logs).pack(side='right', padx=5)
        header = self.background_panel(root)
        header.pack(fill='x')
        self.wallpaper_label(header, text='API ID').pack(side='left')
        ttk.Entry(header, textvariable=self.api_id, width=12).pack(side='left')
        self.wallpaper_label(header, text='API Hash').pack(side='left', padx=10)
        ttk.Entry(header, textvariable=self.api_hash, show='*', width=36).pack(side='left')
        ttk.Button(header, text='Выбрать обложку', command=self.choose_banner).pack(side='right')
        self.rows = []
        self.status_labels = [None] * 10
        self.account_cards = {}
        self.account_dialog = None
        accounts = self.background_panel(root)
        accounts.pack(fill='x', padx=12, pady=8)
        actions = self.background_panel(accounts)
        actions.pack(fill='x', pady=(0, 10))
        self.account_count = tk.StringVar(value='0 из 10 аккаунтов')
        self.wallpaper_label(actions, textvariable=self.account_count, style='Muted.TLabel').pack(side='left')
        ttk.Button(actions, text='+ Добавить аккаунт', style='Accent.TButton', command=self.open_account).pack(side='right')
        self.account_canvas = tk.Canvas(accounts, height=70, background='#e9ecef', highlightthickness=0)
        self.wallpaper_canvases.append(self.account_canvas)
        scrollbar = ttk.Scrollbar(accounts, orient='vertical', command=self.account_canvas.yview)
        scrollbar.pack(side='right', fill='y')
        self.account_canvas.pack(fill='x')
        self.account_canvas.configure(yscrollcommand=lambda first, last: (scrollbar.set(first, last), self.schedule_banner(None)))
        self.accounts_body = self.background_panel(self.account_canvas)
        self.accounts_item = self.account_canvas.create_window(0, 0, window=self.accounts_body, anchor='nw')
        self.account_canvas.bind('<Configure>', lambda event: self.account_canvas.itemconfigure(self.accounts_item, width=event.width))
        self.accounts_body.bind('<Configure>', lambda event: self.resize_accounts())
        self.empty_accounts = self.wallpaper_label(self.accounts_body, text='Добавьте первый аккаунт, чтобы начать.', style='Muted.TLabel', padding=18)
        self.empty_accounts.pack(fill='x')
        for i in range(10):
            status = tk.StringVar(value='● PeerFloodError' if i in self.restricted_accounts else 'Сохранённая сессия')
            self.rows.append((tk.StringVar(), tk.StringVar(), tk.StringVar(), status))
        for i in range(10):
            if (DATA / f'account_{i+1}.session').exists():
                self.show_account(i)
        controls = self.background_panel(root)
        controls.pack(fill='x')
        self.live = tk.BooleanVar(value=False)
        WallpaperCheck(self, controls, text='Отправлять владельцам (выключено = предпросмотр)', variable=self.live).pack(side='left')
        ttk.Button(controls, text='Запустить', command=self.start, style='Accent.TButton').pack(side='left', padx=10)
        ttk.Button(controls, text='Пауза / продолжить', command=lambda: self.loop.call_soon_threadsafe(self.pause)).pack(side='left')
        ttk.Button(controls, text='Остановить', style='Danger.TButton', command=lambda: self.loop.call_soon_threadsafe(self.stop)).pack(side='left', padx=6)
        journal = self.background_panel(root)
        journal.pack(fill='both', expand=True, padx=10, pady=10)
        self.wallpaper_label(journal, text='Журнал событий', style='Muted.TLabel').pack(anchor='w')
        toolbar = self.background_panel(journal)
        toolbar.pack(fill='x', pady=(0, 6))
        self.follow = tk.BooleanVar(value=True)
        WallpaperCheck(self, toolbar, text='Следить за новыми событиями', variable=self.follow).pack(side='left')
        self.wallpaper_label(toolbar, text='Зелёный — отправлено • голубой — предложение • красный — ошибка').pack(side='left', padx=12)
        ttk.Button(toolbar, text='Очистить окно', command=self.clear_log).pack(side='right')
        body = ttk.Frame(journal)
        body.pack(fill='both', expand=True)
        self.output = tk.Text(body, wrap='word', state='disabled', background='#f7faff',
                              foreground='#34465a', selectbackground='#dbe5ee',
                              font=self.log_font, relief='flat', padx=12, pady=10,
                              spacing1=4, spacing3=6)
        scroll = ttk.Scrollbar(body, orient='vertical', command=self.output.yview)
        self.output.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.output.pack(side='left', fill='both', expand=True)
        for tag, color in {'info': '#34465a', 'offer': '#2481cc', 'success': '#258b64',
                           'warning': '#a86c12', 'error': '#d94c4c'}.items():
            self.output.tag_configure(tag, foreground=color)
        root.after(100, self.drain)
        root.after_idle(self.draw_banner)
        root.protocol('WM_DELETE_WINDOW', self.close)

    def resize_accounts(self):
        self.account_canvas.configure(scrollregion=self.account_canvas.bbox('all'),
                                      height=min(230, max(70, self.accounts_body.winfo_reqheight())))

    def show_account(self, i):
        if i in self.account_cards:
            return
        self.empty_accounts.pack_forget()
        card = self.background_panel(self.accounts_body)
        self.account_cards[i] = card
        avatar = tk.Canvas(card, width=38, height=38, background='#e9ecef', highlightthickness=0)
        self.wallpaper_canvases.append(avatar)
        avatar.create_oval(1, 1, 37, 37, fill='#f4cf4e', outline='')
        avatar.create_text(19, 19, text=str(i+1), fill='#25282b', font=('Helvetica', 13, 'bold'))
        avatar.pack(side='left', padx=(0, 10))
        details = self.background_panel(card)
        details.pack(side='left', fill='x', expand=True)
        self.wallpaper_label(details, text=f'Аккаунт {i+1}').pack(anchor='w')
        label = self.status_labels[i] = self.wallpaper_label(details, textvariable=self.rows[i][3],
                                                style='Error.TLabel' if i in self.restricted_accounts else 'Muted.TLabel')
        label.pack(anchor='w')
        ttk.Button(card, text='Подключить', command=lambda: self.open_account(i)).pack(side='left', padx=5)
        ttk.Button(card, text='Выйти', style='Danger.TButton', command=lambda: self.logout(i)).pack(side='left')
        for slot in sorted(self.account_cards):
            self.account_cards[slot].pack_forget()
            self.account_cards[slot].pack(fill='x', pady=2)
        self.account_count.set(f'{len(self.account_cards)} из 10 аккаунтов')
        self.root.after_idle(self.resize_accounts)

    def open_account(self, i=None):
        if self.account_dialog and self.account_dialog.winfo_exists():
            self.account_dialog.lift()
            return
        if i is None:
            i = next((slot for slot in range(10) if slot not in self.account_cards), None)
            if i is None:
                messagebox.showinfo('Аккаунты', 'Можно добавить до 10 аккаунтов. Сначала выйдите из одного из них.')
                return
        window = self.account_dialog = tk.Toplevel(self.root)
        self.account_dialog_slot = i
        window.title(f'Добавить аккаунт • слот {i+1}')
        window.geometry('440x430')
        window.resizable(False, False)
        window.configure(background='#e9ecef')
        body = ttk.Frame(window, padding=24)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='Вход в Telegram', style='Title.TLabel').pack(anchor='w', pady=(0, 8))
        ttk.Label(body, text='Сначала укажите API ID и API Hash в главном окне.', style='Muted.TLabel', wraplength=370).pack(anchor='w', pady=(0, 12))
        phone, code, password, status = self.rows[i]
        ttk.Label(body, text='Номер телефона с кодом страны').pack(anchor='w')
        ttk.Entry(body, textvariable=phone).pack(fill='x', pady=(2, 8))
        ttk.Button(body, text='Получить код / подключить сессию', command=lambda: self.connect(i)).pack(fill='x', pady=(0, 10))
        fields = ttk.Frame(body)
        fields.pack(fill='x')
        fields.columnconfigure(0, weight=1)
        fields.columnconfigure(1, weight=1)
        ttk.Label(fields, text='Код Telegram').grid(row=0, column=0, sticky='w')
        ttk.Label(fields, text='Пароль 2FA, если нужен').grid(row=0, column=1, sticky='w')
        ttk.Entry(fields, textvariable=code, width=12).grid(row=1, column=0, sticky='ew', padx=(0, 8), pady=4)
        ttk.Entry(fields, textvariable=password, show='*', width=16).grid(row=1, column=1, sticky='ew', pady=4)
        ttk.Label(body, textvariable=status, style='Muted.TLabel').pack(anchor='w', pady=10)
        ttk.Button(body, text='Войти', style='Accent.TButton', command=lambda: self.login(i)).pack(fill='x')

    def install_control_theme(self, style):
        # Native ttk handles focus, keyboard activation and widget states;
        # these images replace only the stock square faces and cross indicator.
        self.control_images = []
        def photo(image):
            value = ImageTk.PhotoImage(image)
            self.control_images.append(value)
            return value
        for name, colors in {
            'TButton': ('#f4cf4e', '#ffe080', '#dfb838'),
            'Accent.TButton': ('#f8d354', '#ffe58c', '#e3bc3f'),
            'Danger.TButton': ('#eed06b', '#ffe59b', '#d9b64c'),
        }.items():
            images = []
            for color in (*colors, '#edf1f5'):
                image = Image.new('RGBA', (64, 42))
                draw = ImageDraw.Draw(image)
                draw.rounded_rectangle((1, 1, 62, 38), radius=18, fill=color, outline='#d1b24f', width=1)
                images.append(photo(image))
            element = 'Offers.' + name + '.face'
            style.element_create(element, 'image', images[0], ('disabled', images[3]),
                                 ('pressed', images[2]), ('active', images[1]),
                                 border=(19, 19, 19, 19), sticky='nsew')
            style.layout(name, [(element, {'sticky': 'nsew', 'children': [
                ('Button.padding', {'sticky': 'nsew', 'children': [
                    ('Button.focus', {'sticky': 'nsew', 'children': [
                        ('Button.label', {'sticky': 'nsew'})]})]})]})])
            style.configure(name, padding=(13, 8), foreground='#25282b', borderwidth=0)
            style.map(name, foreground=[('disabled', '#64748b')])
        indicators = []
        for selected, active in ((False, False), (True, False), (False, True)):
            image = Image.new('RGBA', (26, 24))
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((1, 2, 20, 21), radius=5,
                                   fill='#f4cf4e' if selected else '#f5e8b8' if active else '#e9ecef',
                                   outline='#b59a42' if selected or active else '#888d92', width=1)
            if selected:
                draw.line((5, 11, 9, 15, 16, 7), fill='#25282b', width=3, joint='curve')
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
        window.configure(background='#e9ecef')
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
        canvas = tk.Canvas(window, background='#e9ecef', highlightthickness=0)
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
                        self.peer_failure(i, 'read')
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
        text = self.detached_output = tk.Text(window, background='#e9ecef', foreground='#34465a', font=self.log_font, wrap='word', state='disabled', padx=14, pady=12, relief='flat', spacing3=6)
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
        self.log(f'Ошибка: аккаунт {i+1} — PeerFloodError 3/3. Аккаунт выключен из поиска и отправки.')
        try:
            write_settings(DATA / 'account_limits.json', sorted(self.restricted_accounts))
        except OSError:
            self.log('Ошибка: не удалось сохранить отметку ограничения аккаунта')

    def peer_failure(self, slot, phase='send'):
        if not hasattr(self, 'peer_streaks'): self.peer_streaks = {}
        key = (slot, phase)
        count = self.peer_streaks[key] = self.peer_streaks.get(key, 0) + 1
        if count >= 3:
            self.mark_peer_flood(slot)
            return True
        self.log(f'Аккаунт {slot+1}: временный отказ Telegram {count}/3 ({"отправка" if phase == "send" else "поиск"}). Неудачная операция пропущена.')
        return False

    def peer_success(self, slot, phase='send'):
        if hasattr(self, 'peer_streaks'): self.peer_streaks.pop((slot, phase), None)

    def background_panel(self, parent):
        frame = tk.Frame(parent, background='#edf3f8', padx=10, pady=10)
        background = tk.Label(frame, background='#edf3f8', borderwidth=0)
        background.place(x=-10, y=-10, relwidth=1, relheight=1, width=20, height=20)
        background.lower()
        self.background_panels.append((frame, background))
        frame.bind('<Configure>', self.schedule_banner)
        return frame

    def wallpaper_label(self, parent, **options):
        return WallpaperLabel(self, parent, **options)

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
        image = ImageOps.fit(self.banner_source, (width, height), Image.Resampling.LANCZOS) if self.banner_source is not None else Image.new('RGB', (width, height), '#e9ecef')
        self._wallpaper_image = image
        self.banner_photo = ImageTk.PhotoImage(image)
        self.banner.configure(image=self.banner_photo)
        self.background_panels = [(frame, background) for frame, background in self.background_panels if frame.winfo_exists()]
        for frame, background in self.background_panels:
            x = frame.winfo_rootx() - self.root.winfo_rootx()
            y = frame.winfo_rooty() - self.root.winfo_rooty()
            section = image.crop((x, y, x + frame.winfo_width(), y + frame.winfo_height()))
            background.photo = ImageTk.PhotoImage(section)
            background.configure(image=background.photo)
        self.wallpaper_labels = [label for label in self.wallpaper_labels if label.winfo_exists()]
        for label in self.wallpaper_labels:
            label.redraw()
        self.wallpaper_canvases = [canvas for canvas in self.wallpaper_canvases if canvas.winfo_exists()]
        for canvas in self.wallpaper_canvases:
            x = canvas.winfo_rootx() - self.root.winfo_rootx()
            y = canvas.winfo_rooty() - self.root.winfo_rooty()
            canvas.wallpaper_photo = ImageTk.PhotoImage(image.crop((x, y, x+canvas.winfo_width(), y+canvas.winfo_height())))
            canvas.delete('wallpaper')
            canvas.create_image(0, 0, image=canvas.wallpaper_photo, anchor='nw', tags='wallpaper')
            canvas.tag_lower('wallpaper')

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
                self.rows[i][0].set('')
                card = self.account_cards.pop(i, None)
                if card:
                    card.destroy()
                self.status_labels[i] = None
                self.account_count.set(f'{len(self.account_cards)} из 10 аккаунтов')
                if not self.account_cards:
                    self.empty_accounts.pack(fill='x')
            elif kind == 'status':
                i, text = payload
                self.show_account(i)
                limited = i in self.restricted_accounts
                self.rows[i][3].set('● PeerFloodError' if limited else text)
                self.status_labels[i].configure(style='Error.TLabel' if limited else 'Muted.TLabel')
                if text == 'Подключён' and self.account_dialog and self.account_dialog.winfo_exists() and self.account_dialog_slot == i:
                    self.account_dialog.destroy()
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
            self.peer_failure(i, 'read')
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

    async def tracker_catalog(self, client, slot):
        while True:
            try:
                catalog = await watcher.get_gift_catalog(client)
            except PeerFloodError:
                if self.peer_failure(slot, 'read'): return None
                self.log(f'Каталог аккаунта {slot+1}: повтор через {watcher.POLL_INTERVAL} сек.')
                await asyncio.sleep(watcher.POLL_INTERVAL)
            else:
                self.peer_success(slot, 'read')
                return catalog

    async def parallel_scan(self, trackers, ids, seen):
        async def worker(slot, client, gift_ids):
            listings = []
            stats = dict(total_seen_in_responses=0, new_slugs=0, filtered_by_name=0, filtered_names_sample=[], errors=0)
            try:
                for gift_id in gift_ids:
                    if slot in getattr(self, 'restricted_accounts', set()): break
                    while self.paused:
                        await asyncio.sleep(0.2)
                    start = len(listings)
                    errors = stats['errors']
                    try:
                        await watcher.check_gift(client, gift_id, seen, listings, stats)
                    except PeerFloodError:
                        stats['errors'] += 1
                        if self.peer_failure(slot, 'read'): break
                    else:
                        if stats['errors'] == errors: self.peer_success(slot, 'read')
                    finally:
                        for listing in listings[start:]: listing['_source_slot'] = slot
                    await asyncio.sleep(watcher.REQUEST_DELAY)
            except Exception as exc:
                raise AccountScanError(slot, exc) from exc
            return listings, stats
        assignments = [(slot, client, ids[index::len(trackers)]) for index, (slot, client) in enumerate(trackers.items())]
        self.log('Разделение поиска: ' + ' • '.join(f'аккаунт {slot+1}: {len(gifts)} коллекций' for slot, _, gifts in assignments))
        tasks = [asyncio.create_task(worker(slot, client, gifts)) for slot, client, gifts in assignments if gifts]
        try:
            results = await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        listings = []
        stats = dict(total_seen_in_responses=0, new_slugs=0, filtered_by_name=0, filtered_names_sample=[], errors=0)
        for found, counters in results:
            listings.extend(found)
            for name, value in counters.items():
                if isinstance(value, dict):
                    target = stats.setdefault(name, {})
                    for key, count in value.items(): target[key] = target.get(key, 0) + count
                elif isinstance(value, list): stats.setdefault(name, []).extend(value)
                else: stats[name] = stats.get(name, 0) + value
        return listings, stats

    async def scan(self, live, selection=None):
        clients = {i: c for i, c in sorted(self.clients.items()) if i not in getattr(self, 'restricted_accounts', set()) and i not in getattr(self, 'connecting', set()) and await c.is_user_authorized()}
        if not clients:
            self.log('Сначала подключите аккаунт')
            return
        trackers = {i: client for i, client in clients.items() if i not in getattr(self, 'tracker_disabled', set())}
        if not trackers:
            self.log('Включите «Поиск» хотя бы у одного подключённого аккаунта.'); return
        primary = next(iter(trackers.values()))
        current_slot = next(iter(trackers))
        catalog_titles = {}
        store = OfferStore(DATA / ('offers.sqlite' if live else 'preview.sqlite'))
        seen = store.known_slugs()
        warming_up = True
        self.log('Режим: ' + ('ОТПРАВКА' if live else 'ПРЕДПРОСМОТР — сообщения не отправляются'))
        try:
            if selection is not None:
                catalog = await self.tracker_catalog(primary, current_slot)
                if catalog is None: return
                catalog_titles = {gift['id']: gift['title'] for gift in catalog}
                ids = selected_catalog(catalog, selection)
                self.logs.put(('catalog', catalog))
                if not selection['all']:
                    self.log(f"Выбор коллекций: отмечено {len(selection['names'])}, найдено в каталоге {len(ids)}")
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
            self.log('Выбраны коллекции: ' + ', '.join(catalog_titles.get(gift_id, str(gift_id)) for gift_id in ids))
            self.log(f'В сохранённой истории: {len(seen)} подарков')
            self.log('Первый проход: запоминаю текущие подарки, без предложений и отправки')
            while True:
                while self.paused:
                    await asyncio.sleep(0.2)
                trackers = {i: client for i, client in clients.items() if i not in getattr(self, 'tracker_disabled', set()) and i not in getattr(self, 'restricted_accounts', set())}
                if not trackers:
                    self.log('Поиск выключен на всех аккаунтах. Сканирование остановлено.'); return
                current_slot = next(iter(trackers)); primary = trackers[current_slot]
                # check_gift marks discoveries locally; failed owner lookups must be retryable.
                pass_seen = set(seen)
                listings, stats = await self.parallel_scan(trackers, ids, pass_seen)
                for report in stats.get('collection_reports', []):
                    title = catalog_titles.get(report['gift_id'], str(report['gift_id']))
                    self.log(f"Коллекция {title}: лотов {report['total_seen_in_responses']}, Stars {report['stars_listings']}, новых {report['new_slugs']}, в истории {report['known_gifts']}, страниц {report['pages']}" + (f"; ошибка {report['error']}" if report.get('error') else '; есть следующие страницы' if report['more'] else ''))
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
                dropped = {}
                outcomes = {}
                def outcome(listing, reason):
                    title = listing.get('title', '?')
                    counts = outcomes.setdefault(title, {})
                    counts[reason] = counts.get(reason, 0) + 1
                    dropped[reason] = dropped.get(reason, 0) + 1
                candidates = []
                for listing in listings:
                    if store.seen_owner(getattr(listing['owner_id'], 'user_id', None)):
                        outcome(listing, 'продавец уже записан')
                    else:
                        candidates.append(listing)
                listings = candidates
                # Telegram entity caches belong to individual sessions. Resolve a seller
                # through the same account that obtained the listing and its access hash.
                owner_groups = {}
                for listing in listings:
                    slot = listing.get('_source_slot', next(iter(trackers)))
                    owner_groups.setdefault(slot, []).append(listing)
                info_by_slot = {}
                for current_slot, group in owner_groups.items():
                    if current_slot in getattr(self, 'restricted_accounts', set()): continue
                    try:
                        info_by_slot[current_slot] = await watcher.resolve_owner_info(clients[current_slot], group)
                        self.peer_success(current_slot, 'read')
                    except PeerFloodError:
                        self.peer_failure(current_slot, 'read')
                        stats['errors'] += 1
                for listing in listings:
                    while self.paused:
                        await asyncio.sleep(0.2)
                    uid = getattr(listing['owner_id'], 'user_id', None)
                    source = listing.get('_source_slot', next(iter(trackers)))
                    details = info_by_slot.get(source, {}).get(uid, {})
                    recipient = details.get('username')
                    if not recipient:
                        outcome(listing, 'нет username или данных владельца')
                        continue
                    if watcher.EXCLUDE_PAID_MESSAGE_SELLERS and details.get('paid') is not False:
                        outcome(listing, 'платные сообщения' if details.get('paid') else 'не удалось проверить платные сообщения')
                        continue
                    if not watcher.looks_cjk_anywhere(listing['owner_name'], details.get('about', '')):
                        level = details.get('level')
                        if level is not None and level > watcher.MAX_SELLER_LEVEL:
                            outcome(listing, 'звёздный уровень выше лимита')
                            continue
                    try:
                        text = command(listing.get('title'), listing.get('num'), listing['price'])
                    except ValueError:
                        outcome(listing, 'неподходящая цена или название')
                        self.log(f"Пропуск {listing['slug']}: требуется цена только в Stars, название и номер подарка")
                        continue
                    original = int(re.search(r'(?:^| / )(\d+) XTR', listing['price']).group(1))
                    if watcher.MIN_PRICE_STARS and original < watcher.MIN_PRICE_STARS:
                        outcome(listing, 'ниже минимальной цены')
                        continue
                    if watcher.MAX_PRICE_STARS and original > watcher.MAX_PRICE_STARS:
                        outcome(listing, 'выше максимальной цены')
                        continue
                    senders = [i for i in clients if i not in getattr(self, 'restricted_accounts', set())]
                    if not senders:
                        self.log('Все аккаунты отключены от отправки. Сканирование остановлено.'); return
                    slot = store.reserve(listing['slug'], recipient, senders, text, owner_id=uid)
                    seen.add(listing['slug'])
                    if slot is None:
                        outcome(listing, 'подарок или продавец уже записан')
                        continue
                    self.log(f'Аккаунт {slot+1} → @{recipient}: {text}')
                    if not live:
                        outcome(listing, 'предпросмотр')
                        store.status(listing['slug'], 'preview')
                        continue
                    try:
                        await clients[slot].send_message(recipient, text, parse_mode=None)
                    except PeerFloodError:
                        store.status(listing['slug'], 'failed')
                        outcome(listing, 'временный отказ Telegram')
                        self.peer_failure(slot)
                    except (UserPrivacyRestrictedError, UserIsBlockedError):
                        self.peer_success(slot)
                        store.status(listing['slug'], 'failed')
                        outcome(listing, 'получатель запретил сообщения')
                        self.log(f'Пропуск • аккаунт {slot+1} → @{recipient}: сообщения ограничены получателем')
                    except FloodWaitError as exc:
                        store.status(listing['slug'], 'blocked')
                        self.log(f'Telegram ограничил аккаунт {slot+1} на {exc.seconds} сек. Отправка остановлена; другие аккаунты не используются для обхода.')
                        return
                    except Exception as exc:
                        self.peer_success(slot)
                        store.status(listing['slug'], 'failed')
                        outcome(listing, 'ошибка отправки')
                        self.log(f'Не отправлено • аккаунт {slot+1} → @{recipient} • {listing["slug"]} • {type(exc).__name__}; автоматического повтора нет')
                    else:
                        self.peer_success(slot)
                        outcome(listing, 'отправлено')
                        store.status(listing['slug'], 'sent')
                        self.log(f'Отправлено • аккаунт {slot+1} → @{recipient} • {listing["slug"]}')
                    await asyncio.sleep(max(1, watcher.SEND_DELAY_SECONDS))
                for title, counts in sorted(outcomes.items()):
                    self.log(f'Результат {title}: ' + '; '.join(f'{reason}: {count}' for reason, count in counts.items()))
                if dropped:
                    self.log('Итог отбора: ' + '; '.join(f'{reason}: {count}' for reason, count in dropped.items()))
                self.log(f"Проход завершён: проверено {len(ids)} коллекций, новых кандидатов {len(listings)}, ошибок запросов {stats['errors']}")
                await asyncio.sleep(watcher.POLL_INTERVAL)
        except AccountScanError as exc:
            if isinstance(exc.cause, PeerFloodError):
                self.peer_failure(exc.slot, 'read')
            elif isinstance(exc.cause, FloodWaitError):
                self.log(f'Telegram ограничил запросы аккаунта {exc.slot+1} на {exc.cause.seconds} сек. Сканирование остановлено.')
            else:
                self.log(f'Ошибка: поиск аккаунта {exc.slot+1} — {type(exc.cause).__name__}. Сканирование остановлено.')
        except PeerFloodError:
            self.peer_failure(current_slot, 'read')
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
    from ui_qt import main
    main()
