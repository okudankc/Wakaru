import asyncio
import json
import socket
import threading
import tkinter as tk
from tkinter import ttk
import websockets
import sys
import queue
from pathlib import Path

if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent

CONFIG_FILE = BASE_DIR / 'student_config.json'
UDP_PORT = 9999
UDP_DISCOVER = b'WAKARU_DISCOVER'
UDP_RESPONSE = b'WAKARU_SERVER:'
RECOVERY_SECONDS = 60

CANT_BUTTON_OPTIONS = [
    '🆘',
    '🤯\nちょっと待って！',
    '😵‍💫\n迷子です',
    '🙋\n置いてかないで',
    '🐢\n速すぎ！',
]


def load_config():
    default = {
        'student_id': '', 'class_name': '', 'attendance_no': '', 'name': '', 'teacher': '',
        'seat_x': -1.0, 'seat_y': -1.0,
        'cant_button_idx': 3,
        'always_on_top': False,
    }
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                default.update(json.load(f))
        except Exception:
            pass
    return default


def save_config(cfg):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def discover_server(timeout=5.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        local_ip = s.getsockname()[0]
    except Exception:
        local_ip = '127.0.0.1'
    finally:
        s.close()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(timeout)
    try:
        sock.sendto(UDP_DISCOVER, ('255.255.255.255', UDP_PORT))
        prefix = '.'.join(local_ip.split('.')[:3])
        for i in range(1, 255):
            host = f'{prefix}.{i}'
            if host != local_ip:
                try:
                    sock.sendto(UDP_DISCOVER, (host, UDP_PORT))
                except Exception:
                    pass
        data, _ = sock.recvfrom(1024)
        if data.startswith(UDP_RESPONSE):
            info = data[len(UDP_RESPONSE):].decode()
            ip, port = info.rsplit(':', 1)
            return ip, int(port)
    except Exception:
        pass
    finally:
        sock.close()
    return None, None


class StudentApp:
    WIN = 170

    def __init__(self):
        self.config = load_config()
        self.server_ip = None
        self.server_port = 8765
        self.ws = None
        self.loop = asyncio.new_event_loop()
        self.gui_queue = queue.Queue()
        self.teachers = []
        self.connected = False
        self.cant_active = False
        self.remaining = 0
        self._countdown_job = None

        threading.Thread(target=self._run_loop, daemon=True).start()

        self.root = tk.Tk()
        self.root.title('Wakaru')
        self.root.resizable(False, False)
        self.root.attributes('-topmost', self.config.get('always_on_top', False))
        self._build()
        self.root.geometry(f'{self.WIN}x{self.WIN}')
        self._poll_queue()
        self.root.after(400, self._auto_discover)

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    # ── UI ────────────────────────────────────────────────
    def _build(self):
        bg = '#1e1e1e'
        self.root.configure(bg=bg)

        # ヘッダー行
        header = tk.Frame(self.root, bg=bg)
        header.pack(fill=tk.X, padx=5, pady=(5, 0))

        tk.Button(
            header, text='⚙',
            command=self._open_setup,
            bg='#333', fg='#999', relief='flat',
            font=('Yu Gothic UI', 9), padx=4, pady=1
        ).pack(side=tk.LEFT)

        self.status_dot = tk.Label(header, text='●', fg='#444', bg=bg,
                                   font=('Yu Gothic UI', 10))
        self.status_dot.pack(side=tk.RIGHT)

        # メインボタン
        idx = self.config.get('cant_button_idx', 3)
        self.cant_btn = tk.Button(
            self.root,
            text=CANT_BUTTON_OPTIONS[idx],
            command=self._on_cant,
            font=('Yu Gothic UI', 15, 'bold'),
            bg='#555', fg='#888',
            relief='flat',
            state='disabled',
            wraplength=self.WIN - 20,
        )
        self.cant_btn.pack(fill=tk.BOTH, expand=True, padx=5, pady=(3, 0))

        # カウントダウン
        self.timer_lbl = tk.Label(self.root, text='', font=('Yu Gothic UI', 7),
                                   fg='#666', bg=bg)
        self.timer_lbl.pack(pady=(2, 1))

        # ステータスバー
        self.status_var = tk.StringVar(value='接続中...')
        tk.Label(
            self.root, textvariable=self.status_var,
            bg='#111', fg='#555', anchor='w',
            font=('Yu Gothic UI', 7), relief='sunken'
        ).pack(fill=tk.X, padx=5, pady=(0, 5))

    def _open_setup(self):
        SetupDialog(self.root, self)

    # ── Discovery / Connection ────────────────────────────
    def _auto_discover(self):
        self._set_status('検索中...')

        def task():
            ip, port = discover_server()
            if ip:
                self.server_ip = ip
                self.server_port = port
                self.gui_queue.put(('status', f'発見: {ip}'))
                asyncio.run_coroutine_threadsafe(self._connect(), self.loop)
            else:
                self.gui_queue.put(('status', '先生ツールが見つかりません'))
                self.root.after(15000, self._auto_discover)

        threading.Thread(target=task, daemon=True).start()

    async def _connect(self):
        uri = f'ws://{self.server_ip}:{self.server_port}'
        try:
            async with websockets.connect(uri) as ws:
                self.ws = ws
                self.gui_queue.put(('connected',))
                await ws.send(json.dumps({'type': 'get_teachers'}))

                cfg = self.config
                if cfg.get('student_id') and cfg.get('name'):
                    await ws.send(json.dumps({
                        'type': 'register',
                        'student_id': cfg['student_id'],
                        'class_name': cfg['class_name'],
                        'attendance_no': cfg['attendance_no'],
                        'name': cfg['name'],
                        'teacher': cfg['teacher'],
                        'seat_x': cfg.get('seat_x', -1),
                        'seat_y': cfg.get('seat_y', -1),
                    }))

                async for raw in ws:
                    try:
                        data = json.loads(raw)
                        if data.get('type') == 'teachers':
                            self.gui_queue.put(('teachers', data['list']))
                    except Exception:
                        pass
        except Exception:
            self.ws = None
            self.gui_queue.put(('disconnected',))
            await asyncio.sleep(10)
            asyncio.ensure_future(self._connect())

    # ── Queue processing ──────────────────────────────────
    def _poll_queue(self):
        try:
            while True:
                msg = self.gui_queue.get_nowait()
                kind = msg[0]
                if kind == 'status':
                    self._set_status(msg[1])
                elif kind == 'connected':
                    self.connected = True
                    self._set_status(f'接続済み: {self.server_ip}')
                    self.status_dot.config(fg='#27ae60')
                    self._refresh_btn()
                elif kind == 'disconnected':
                    self.connected = False
                    self._set_status('切断 再接続中...')
                    self.status_dot.config(fg='#e74c3c')
                    self._refresh_btn()
                elif kind == 'teachers':
                    self.teachers = msg[1]
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _set_status(self, text):
        self.status_var.set(text)

    def _refresh_btn(self):
        cfg = self.config
        has_info = bool(cfg.get('student_id') and cfg.get('name'))
        if self.connected and has_info and not self.cant_active:
            idx = cfg.get('cant_button_idx', 3)
            self.cant_btn.config(
                state='normal', bg='#e67e22', fg='white',
                text=CANT_BUTTON_OPTIONS[idx]
            )
        elif not self.connected or not has_info:
            self.cant_btn.config(state='disabled', bg='#555', fg='#888',
                                 text=CANT_BUTTON_OPTIONS[cfg.get('cant_button_idx', 3)])

    # ── Can't keep up ─────────────────────────────────────
    def _on_cant(self):
        if self.cant_active:
            return
        self.cant_active = True
        self.cant_btn.config(state='disabled', bg='#e74c3c', fg='white', text='📡\n通知中')
        self._send({'type': 'cant_keep_up'})
        self.remaining = RECOVERY_SECONDS
        self._tick()

    def _tick(self):
        if self.remaining > 0:
            self.timer_lbl.config(text=f'あと {self.remaining}秒で解除')
            self.remaining -= 1
            self._countdown_job = self.root.after(1000, self._tick)
        else:
            self._recover()

    def _recover(self):
        self.cant_active = False
        self.timer_lbl.config(text='')
        self._send({'type': 'recovered'})
        self._refresh_btn()

    def _send(self, data):
        if self.ws and self.connected:
            asyncio.run_coroutine_threadsafe(
                self.ws.send(json.dumps(data)), self.loop
            )

    def run(self):
        self.root.mainloop()


# ── Setup Dialog ──────────────────────────────────────────
class SetupDialog:
    SEAT_SIZE = 150

    def __init__(self, parent, app):
        self.app = app
        win = tk.Toplevel(parent)
        win.title('設定')
        win.resizable(False, False)
        win.grab_set()
        win.attributes('-topmost', True)
        self.win = win
        self._build()

    def _build(self):
        nb = ttk.Notebook(self.win)
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        info_f = ttk.Frame(nb)
        nb.add(info_f, text='学生情報')
        self._build_info(info_f)

        seat_f = ttk.Frame(nb)
        nb.add(seat_f, text='座席')
        self._build_seat(seat_f)

        btn_f = ttk.Frame(nb)
        nb.add(btn_f, text='ボタン')
        self._build_button(btn_f)

        tk.Button(
            self.win, text='保存して閉じる',
            command=self._save,
            font=('Yu Gothic UI', 10),
            bg='#4a90d9', fg='white', relief='flat',
            padx=20, pady=5
        ).pack(pady=10)

    def _build_info(self, f):
        cfg = self.app.config
        FIELDS = [
            ('学籍番号', 'student_id'),
            ('クラス名', 'class_name'),
            ('出席番号', 'attendance_no'),
            ('氏名',    'name'),
        ]
        self.reg_vars = {}
        for i, (label, key) in enumerate(FIELDS):
            tk.Label(f, text=label, font=('Yu Gothic UI', 10)).grid(
                row=i, column=0, sticky='e', padx=(10, 6), pady=5
            )
            var = tk.StringVar(value=cfg.get(key, ''))
            self.reg_vars[key] = var
            tk.Entry(f, textvariable=var, width=20).grid(
                row=i, column=1, sticky='w', padx=(0, 10), pady=5
            )

        tk.Label(f, text='先生', font=('Yu Gothic UI', 10)).grid(
            row=len(FIELDS), column=0, sticky='e', padx=(10, 6), pady=5
        )
        self.teacher_var = tk.StringVar(value=cfg.get('teacher', ''))
        self.teacher_cb = ttk.Combobox(
            f, textvariable=self.teacher_var,
            values=self.app.teachers, width=18, state='readonly'
        )
        self.teacher_cb.grid(row=len(FIELDS), column=1, sticky='w', padx=(0, 10), pady=5)

        self.top_var = tk.BooleanVar(value=cfg.get('always_on_top', False))
        tk.Checkbutton(f, text='常に最前面に表示',
                       variable=self.top_var).grid(
            row=len(FIELDS)+1, column=0, columnspan=2, pady=(10, 6)
        )

    def _build_seat(self, f):
        cfg = self.app.config
        tk.Label(f, text='教室の中の自席をクリック',
                 font=('Yu Gothic UI', 9), fg='#666').pack(pady=(8, 4))
        self.seat_canvas = tk.Canvas(
            f, width=self.SEAT_SIZE, height=self.SEAT_SIZE,
            bg='#1a1a2e', highlightthickness=1, highlightbackground='#555',
            cursor='crosshair'
        )
        self.seat_canvas.pack(pady=(0, 10))
        self._draw_seat(cfg)
        self.seat_canvas.bind('<Button-1>', self._on_seat_click)

    def _draw_seat(self, cfg):
        c = self.seat_canvas
        s = self.SEAT_SIZE
        c.delete('all')
        for i in range(0, s + 1, s // 4):
            c.create_line(i, 0, i, s, fill='#2a2a4e')
            c.create_line(0, i, s, i, fill='#2a2a4e')
        c.create_rectangle(s * 0.1, 3, s * 0.9, 14, fill='#2d5a2d', outline='#4a8a4a')
        c.create_text(s / 2, 9, text='黒板', fill='#8aaa8a', font=('Yu Gothic UI', 7))
        sx = cfg.get('seat_x', -1)
        sy = cfg.get('seat_y', -1)
        if 0.0 <= sx <= 1.0 and 0.0 <= sy <= 1.0:
            self._place_dot(int(sx * s), int(sy * s))

    def _place_dot(self, x, y):
        r = 7
        self.seat_canvas.create_oval(x - r, y - r, x + r, y + r,
                                     fill='#4a90d9', outline='#ffffff', width=1, tags='dot')

    def _on_seat_click(self, event):
        s = self.SEAT_SIZE
        self.seat_canvas.delete('dot')
        self._place_dot(event.x, event.y)
        self.app.config['seat_x'] = round(event.x / s, 4)
        self.app.config['seat_y'] = round(event.y / s, 4)

    def _build_button(self, f):
        cfg = self.app.config
        tk.Label(f, text='ボタンのデザインを選択',
                 font=('Yu Gothic UI', 10, 'bold')).pack(anchor='w', padx=10, pady=(10, 6))
        self.btn_idx_var = tk.IntVar(value=cfg.get('cant_button_idx', 3))
        labels = ['🆘', '🤯 ちょっと待って！', '😵 迷子です', '🙋 置いてかないで', '🐢 速すぎ！']
        for i, label in enumerate(labels):
            tk.Radiobutton(
                f, text=label,
                variable=self.btn_idx_var, value=i,
                font=('Yu Gothic UI', 12)
            ).pack(anchor='w', padx=20, pady=3)

    def _save(self):
        cfg = self.app.config
        for key, var in self.reg_vars.items():
            cfg[key] = var.get().strip()
        cfg['teacher'] = self.teacher_var.get()
        cfg['always_on_top'] = self.top_var.get()
        cfg['cant_button_idx'] = self.btn_idx_var.get()
        save_config(cfg)

        self.app.root.attributes('-topmost', cfg['always_on_top'])
        self.app._refresh_btn()

        if self.app.ws and self.app.connected:
            self.app._send({
                'type': 'register',
                'student_id': cfg['student_id'],
                'class_name': cfg['class_name'],
                'attendance_no': cfg['attendance_no'],
                'name': cfg['name'],
                'teacher': cfg['teacher'],
                'seat_x': cfg.get('seat_x', -1),
                'seat_y': cfg.get('seat_y', -1),
            })

        self.win.destroy()


if __name__ == '__main__':
    StudentApp().run()
