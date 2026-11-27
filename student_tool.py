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
    default = {'student_id': '', 'class_name': '', 'attendance_no': '', 'name': '', 'teacher': '', 'manual_server_ip': ''}
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
                row=i, column=0, sticky='e', padx=(10, 6), pady=6
            )
            var = tk.StringVar(value=self.config.get(key, ''))
            self.reg_vars[key] = var
            tk.Entry(f, textvariable=var, width=20).grid(
                row=i, column=1, sticky='w', padx=(0, 10), pady=6
            )

        tk.Label(f, text='先生', font=('Yu Gothic UI', 10)).grid(
            row=len(FIELDS), column=0, sticky='e', padx=(10, 6), pady=6
        )
        self.teacher_var = tk.StringVar(value=self.config.get('teacher', ''))
        self.teacher_cb = ttk.Combobox(
            f, textvariable=self.teacher_var,
            values=[], width=18, state='readonly'
        )
        self.teacher_cb.grid(row=len(FIELDS), column=1, sticky='w', padx=(0, 10), pady=6)

        tk.Button(
            f, text='保存して接続',
            command=self._save_and_register,
            font=('Yu Gothic UI', 10),
            bg='#4a90d9', fg='white', relief='flat',
            padx=16, pady=6
        ).grid(row=len(FIELDS)+1, column=0, columnspan=2, pady=12)

        # 手動IP接続（自動検出が失敗する環境向け）
        sep = tk.Frame(f, height=1, bg='#555')
        sep.grid(row=len(FIELDS)+2, column=0, columnspan=2, sticky='ew', padx=10, pady=(4, 6))

        tk.Label(f, text='先生PCのIPアドレス\n(自動検出できない場合)',
                 font=('Yu Gothic UI', 8), fg='#888', justify='right').grid(
            row=len(FIELDS)+3, column=0, sticky='e', padx=(10, 6), pady=4
        )
        manual_frame = tk.Frame(f)
        manual_frame.grid(row=len(FIELDS)+3, column=1, sticky='w', padx=(0, 10), pady=4)
        self.manual_ip_var = tk.StringVar(value=self.config.get('manual_server_ip', ''))
        tk.Entry(manual_frame, textvariable=self.manual_ip_var, width=14,
                 font=('Yu Gothic UI', 10)).pack(side=tk.LEFT)
        tk.Button(
            manual_frame, text='接続',
            command=self._manual_connect,
            font=('Yu Gothic UI', 9),
            bg='#555', fg='white', relief='flat',
            padx=8, pady=2
        ).pack(side=tk.LEFT, padx=(4, 0))

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
                manual_ip = self.config.get('manual_server_ip', '').strip()
                if manual_ip:
                    self.server_ip = manual_ip
                    self.server_port = 8765
                    self.gui_queue.put(('status', f'手動IPで接続試行: {manual_ip}'))
                    asyncio.run_coroutine_threadsafe(self._connect(), self.loop)
                else:
                    self.gui_queue.put(('status', 'サーバーが見つかりません。先生ツールが起動しているか確認してください。'))
                    # retry after 15s
                    self.root.after(15000, self._auto_discover)

        threading.Thread(target=task, daemon=True).start()

    def _manual_connect(self):
        ip = self.manual_ip_var.get().strip()
        if not ip:
            messagebox.showwarning('入力エラー', 'IPアドレスを入力してください。')
            return
        self.config['manual_server_ip'] = ip
        save_config(self.config)
        self.server_ip = ip
        self.server_port = 8765
        self._set_status(f'手動IPで接続試行: {ip}')
        asyncio.run_coroutine_threadsafe(self._connect(), self.loop)

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
            })

        self._refresh_btn()
        self.nb.select(1)
        messagebox.showinfo('保存完了', '情報を保存しました。')

    def run(self):
        self.root.mainloop()


if __name__ == '__main__':
    StudentApp().run()
