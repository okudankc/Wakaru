import asyncio
import json
import socket
import threading
import tkinter as tk
from tkinter import ttk, messagebox
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


def load_config():
    default = {'student_id': '', 'class_name': '', 'attendance_no': '', 'name': '', 'teacher': '',
               'seat_x': -1.0, 'seat_y': -1.0}
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
    # 自分のIPを取得してサブネットを割り出す
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
        # まずブロードキャスト（届く環境では最速）
        sock.sendto(UDP_DISCOVER, ('255.255.255.255', UDP_PORT))

        # AP分離でブロードキャストが届かない場合のため /24 ユニキャストスキャン
        # ルーター経由のユニキャストはAP分離に影響されない
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
        self.root.title('Wakaru 学生ツール')
        self.root.resizable(False, False)
        self._build()
        self._poll_queue()
        self.root.after(400, self._auto_discover)

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    # ── UI ────────────────────────────────────────────────
    def _build(self):
        bg = '#1e1e1e'
        self.root.configure(bg=bg)

        nb = ttk.Notebook(self.root)
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.nb = nb

        # ── 学生情報タブ ──
        reg = ttk.Frame(nb)
        nb.add(reg, text='学生情報')
        self._build_reg(reg)

        # ── 授業タブ ──
        main = ttk.Frame(nb)
        nb.add(main, text='授業')
        self._build_main(main)

        # ステータスバー
        self.status_var = tk.StringVar(value='サーバーを検索中...')
        tk.Label(
            self.root, textvariable=self.status_var,
            bg='#111', fg='#888', anchor='w',
            font=('Yu Gothic UI', 8), relief='sunken'
        ).pack(fill=tk.X, padx=10, pady=(0, 6))

    SEAT_SIZE = 180

    def _build_reg(self, f):
        FIELDS = [
            ('学籍番号', 'student_id'),
            ('クラス名', 'class_name'),
            ('出席番号', 'attendance_no'),
            ('氏名',    'name'),
        ]
        self.reg_vars = {}
        for i, (label, key) in enumerate(FIELDS):
            tk.Label(f, text=label, font=('Yu Gothic UI', 10)).grid(
                row=i, column=0, sticky='e', padx=(10, 6), pady=4
            )
            var = tk.StringVar(value=self.config.get(key, ''))
            self.reg_vars[key] = var
            tk.Entry(f, textvariable=var, width=20).grid(
                row=i, column=1, sticky='w', padx=(0, 10), pady=4
            )

        tk.Label(f, text='先生', font=('Yu Gothic UI', 10)).grid(
            row=len(FIELDS), column=0, sticky='e', padx=(10, 6), pady=4
        )
        self.teacher_var = tk.StringVar(value=self.config.get('teacher', ''))
        self.teacher_cb = ttk.Combobox(
            f, textvariable=self.teacher_var,
            values=[], width=18, state='readonly'
        )
        self.teacher_cb.grid(row=len(FIELDS), column=1, sticky='w', padx=(0, 10), pady=4)

        tk.Button(
            f, text='保存して接続',
            command=self._save_and_register,
            font=('Yu Gothic UI', 10),
            bg='#4a90d9', fg='white', relief='flat',
            padx=16, pady=6
        ).grid(row=len(FIELDS)+1, column=0, columnspan=2, pady=(8, 4))

        # 座席位置
        tk.Frame(f, height=1, bg='#555').grid(
            row=len(FIELDS)+2, column=0, columnspan=2, sticky='ew', padx=10, pady=(2, 6)
        )
        tk.Label(f, text='自席の位置をクリック', font=('Yu Gothic UI', 9), fg='#aaa').grid(
            row=len(FIELDS)+3, column=0, columnspan=2
        )
        self.seat_canvas = tk.Canvas(
            f, width=self.SEAT_SIZE, height=self.SEAT_SIZE,
            bg='#1a1a2e', cursor='crosshair',
            highlightthickness=1, highlightbackground='#555'
        )
        self.seat_canvas.grid(row=len(FIELDS)+4, column=0, columnspan=2, pady=(4, 10))
        self._draw_seat_grid()
        self.seat_canvas.bind('<Button-1>', self._on_seat_click)

    def _build_main(self, f):
        self.info_lbl = tk.Label(
            f, text='← 学生情報タブで情報を入力してください',
            font=('Yu Gothic UI', 9), fg='#888'
        )
        self.info_lbl.pack(pady=(16, 4))

        self.cant_btn = tk.Button(
            f, text='ついていけない',
            command=self._on_cant,
            font=('Yu Gothic UI', 18, 'bold'),
            bg='#555', fg='#aaa',
            relief='flat', width=14, height=3,
            state='disabled'
        )
        self.cant_btn.pack(pady=16)

        self.timer_lbl = tk.Label(f, text='', font=('Yu Gothic UI', 10), fg='#888')
        self.timer_lbl.pack()

    def _draw_seat_grid(self):
        c = self.seat_canvas
        s = self.SEAT_SIZE
        c.delete('all')
        for i in range(0, s + 1, s // 4):
            c.create_line(i, 0, i, s, fill='#2a2a4e')
            c.create_line(0, i, s, i, fill='#2a2a4e')
        c.create_rectangle(s * 0.1, 3, s * 0.9, 14, fill='#2d5a2d', outline='#4a8a4a')
        c.create_text(s / 2, 9, text='黒板', fill='#8a8', font=('Yu Gothic UI', 7))
        sx = self.config.get('seat_x', -1)
        sy = self.config.get('seat_y', -1)
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
        self.config['seat_x'] = round(event.x / s, 4)
        self.config['seat_y'] = round(event.y / s, 4)

    # ── Discovery / Connection ────────────────────────────
    def _auto_discover(self):
        self._set_status('サーバーを検索中...')

        def task():
            ip, port = discover_server()
            if ip:
                self.server_ip = ip
                self.server_port = port
                self.gui_queue.put(('status', f'サーバー発見: {ip}'))
                asyncio.run_coroutine_threadsafe(self._connect(), self.loop)
            else:
                self.gui_queue.put(('status', 'サーバーが見つかりません。先生ツールが起動しているか確認してください。'))
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
                    self._refresh_btn()
                elif kind == 'disconnected':
                    self.connected = False
                    self._set_status('切断されました。再接続中...')
                    self._refresh_btn()
                elif kind == 'teachers':
                    self.teachers = msg[1]
                    self.teacher_cb['values'] = self.teachers
                    if not self.teacher_var.get() and self.teachers:
                        self.teacher_var.set(self.teachers[0])
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _set_status(self, text):
        self.status_var.set(text)

    def _refresh_btn(self):
        cfg = self.config
        has_info = bool(cfg.get('student_id') and cfg.get('name'))
        if self.connected and has_info and not self.cant_active:
            self.cant_btn.config(state='normal', bg='#e67e22', fg='white', text='ついていけない')
        elif not self.connected or not has_info:
            self.cant_btn.config(state='disabled', bg='#555', fg='#aaa', text='ついていけない')

    # ── Can't keep up ─────────────────────────────────────
    def _on_cant(self):
        if self.cant_active:
            return
        self.cant_active = True
        self.cant_btn.config(
            state='disabled', bg='#e74c3c', fg='white',
            text='通知中\n(自動解除まで待ってください)'
        )
        self._send({'type': 'cant_keep_up'})
        self.remaining = RECOVERY_SECONDS
        self._tick()

    def _tick(self):
        if self.remaining > 0:
            self.timer_lbl.config(text=f'あと {self.remaining} 秒で自動解除')
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

    # ── Save & Register ───────────────────────────────────
    def _save_and_register(self):
        for key, var in self.reg_vars.items():
            self.config[key] = var.get().strip()
        self.config['teacher'] = self.teacher_var.get()
        save_config(self.config)

        self.info_lbl.config(
            text=(
                f"{self.config['class_name']}  出席{self.config['attendance_no']}番  "
                f"{self.config['name']}  /  先生: {self.config['teacher']}"
            )
        )

        if self.ws and self.connected:
            self._send({
                'type': 'register',
                'student_id': self.config['student_id'],
                'class_name': self.config['class_name'],
                'attendance_no': self.config['attendance_no'],
                'name': self.config['name'],
                'teacher': self.config['teacher'],
                'seat_x': self.config.get('seat_x', -1),
                'seat_y': self.config.get('seat_y', -1),
            })

        self._refresh_btn()
        self.nb.select(1)
        messagebox.showinfo('保存完了', '情報を保存しました。')

    def run(self):
        self.root.mainloop()


if __name__ == '__main__':
    StudentApp().run()
