import asyncio
import csv
import ctypes
import json
import os
import socket
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime, time as dtime
import websockets
import sys
import queue
from pathlib import Path

if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent

CONFIG_FILE = BASE_DIR / 'teacher_config.json'
WEBSOCKET_PORT = 8765
UDP_PORT = 9999
UDP_DISCOVER = b'WAKARU_DISCOVER'
UDP_RESPONSE = b'WAKARU_SERVER:'


RULE_TCP = 'Wakaru-WebSocket'
RULE_UDP = 'Wakaru-UDP-Discovery'

LOG_DIR = BASE_DIR / 'logs'

PERIODS = [
    ('1・2限', dtime(9, 30),  dtime(11, 0)),
    ('3・4限', dtime(11, 10), dtime(12, 40)),
    ('5・6限', dtime(13, 30), dtime(15, 0)),
    ('7・8限', dtime(15, 10), dtime(16, 40)),
]


def get_current_period():
    now = datetime.now().time()
    for name, start, end in PERIODS:
        if start <= now <= end:
            return name
    return '時間外'


def write_log(student_info, start_dt, end_dt):
    LOG_DIR.mkdir(exist_ok=True)
    today = start_dt.strftime('%Y-%m-%d')
    log_file = LOG_DIR / f'{today}.csv'
    new_file = not log_file.exists()
    with open(log_file, 'a', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(['日付', '時限', '学籍番号', 'クラス名', '出席番号', '氏名', '担当教師', '開始時刻', '解除時刻'])
        w.writerow([
            today,
            get_current_period(),
            student_info.get('student_id', ''),
            student_info.get('class_name', ''),
            student_info.get('attendance_no', ''),
            student_info.get('name', ''),
            student_info.get('teacher', ''),
            start_dt.strftime('%H:%M:%S'),
            end_dt.strftime('%H:%M:%S'),
        ])


def _is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _rule_exists(name):
    try:
        r = subprocess.run(
            f'netsh advfirewall firewall show rule name="{name}"',
            shell=True, capture_output=True, text=True,
            encoding='cp932', errors='ignore'
        )
        stdout = r.stdout or ''
        return 'No rules match' not in stdout and r.returncode == 0
    except Exception:
        return False


def _add_rule(name, protocol, port):
    subprocess.run(
        f'netsh advfirewall firewall add rule '
        f'name="{name}" dir=in action=allow protocol={protocol} localport={port}',
        shell=True, capture_output=True,
        encoding='cp932', errors='ignore'
    )


def setup_firewall():
    """ファイアウォールルールを追加（管理者権限必要）。"""
    if not _rule_exists(RULE_TCP):
        _add_rule(RULE_TCP, 'TCP', WEBSOCKET_PORT)
    if not _rule_exists(RULE_UDP):
        _add_rule(RULE_UDP, 'UDP', UDP_PORT)


def ensure_firewall():
    """
    初回起動時にファイアウォールを設定する。
    管理者権限がなければ昇格して再起動する。
    """
    if _rule_exists(RULE_TCP) and _rule_exists(RULE_UDP):
        return  # 既に設定済み

    if _is_admin():
        setup_firewall()
        return

    ans = messagebox.askyesno(
        'ファイアウォール設定',
        'ポート 8765 (TCP) と 9999 (UDP) をファイアウォールで許可する必要があります。\n'
        '管理者権限で自動設定しますか？\n\n'
        '「いいえ」を選ぶと手動設定が必要です。'
    )
    if ans:
        exe = sys.executable
        args = ' '.join(f'"{a}"' for a in sys.argv)
        ctypes.windll.shell32.ShellExecuteW(None, 'runas', exe, args, None, 1)
        sys.exit(0)


def load_config():
    default = {
        'teachers': [],
        'threshold_type': 'percentage',
        'threshold_value': 30,
        'always_on_top': True,
    }
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                default.update(json.load(f))
        except Exception:
            pass
    return default


def save_config(config):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


class AppState:
    def __init__(self):
        self.config = load_config()
        self.connected = {}        # id(ws) -> student_info dict
        self.cant_keep_up = set()  # student_id
        self.cant_start = {}       # student_id -> datetime
        self.lock = threading.Lock()
        self.gui_queue = queue.Queue()

    def total(self):
        with self.lock:
            return len(self.connected)

    def cant_count(self):
        with self.lock:
            return len(self.cant_keep_up)

    def threshold_exceeded(self):
        total = self.total()
        count = self.cant_count()
        if total == 0:
            return False
        cfg = self.config
        if cfg['threshold_type'] == 'percentage':
            return (count / total * 100) > cfg['threshold_value']
        return count > cfg['threshold_value']


state = AppState()


# ── WebSocket ──────────────────────────────────────────────
async def ws_handler(websocket, path=None):
    sid = None
    try:
        async for raw in websocket:
            try:
                data = json.loads(raw)
            except Exception:
                continue
            t = data.get('type')

            if t == 'get_teachers':
                await websocket.send(json.dumps({
                    'type': 'teachers',
                    'list': state.config['teachers']
                }))

            elif t == 'register':
                sid = data.get('student_id', f'unknown_{id(websocket)}')
                with state.lock:
                    state.connected[id(websocket)] = data
                    state.connected[id(websocket)]['_sid'] = sid
                state.gui_queue.put(('update',))

            elif t == 'cant_keep_up':
                if sid:
                    with state.lock:
                        state.cant_keep_up.add(sid)
                        state.cant_start[sid] = datetime.now()
                    state.gui_queue.put(('update',))

            elif t == 'recovered':
                if sid:
                    _flush_log(sid, websocket)
                    with state.lock:
                        state.cant_keep_up.discard(sid)
                    state.gui_queue.put(('update',))

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if sid:
            _flush_log(sid, websocket)
        with state.lock:
            state.connected.pop(id(websocket), None)
            if sid:
                state.cant_keep_up.discard(sid)
        state.gui_queue.put(('update',))


def _flush_log(sid, websocket):
    """未記録の cant_keep_up イベントをログに書き出す。"""
    with state.lock:
        start = state.cant_start.pop(sid, None)
        info = state.connected.get(id(websocket), {})
    if start:
        threading.Thread(
            target=write_log, args=(info, start, datetime.now()), daemon=True
        ).start()


async def run_ws():
    async with websockets.serve(ws_handler, '0.0.0.0', WEBSOCKET_PORT):
        await asyncio.Future()


# ── UDP discovery ──────────────────────────────────────────
def run_udp():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('', UDP_PORT))
    sock.settimeout(1.0)

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        local_ip = s.getsockname()[0]
    except Exception:
        local_ip = '127.0.0.1'
    finally:
        s.close()

    response = UDP_RESPONSE + f'{local_ip}:{WEBSOCKET_PORT}'.encode()
    while True:
        try:
            data, addr = sock.recvfrom(1024)
            if data == UDP_DISCOVER:
                sock.sendto(response, addr)
        except socket.timeout:
            continue
        except Exception:
            break


# ── GUI ───────────────────────────────────────────────────
class TeacherGUI:
    def __init__(self, root):
        self.root = root
        self.root.title('Wakaru 管理ツール')
        self.root.resizable(False, False)
        self.root.attributes('-topmost', state.config['always_on_top'])
        self._blink_job = None
        self._blink_on = False
        self._build()
        self._poll_queue()

    def _build(self):
        bg = '#1e1e1e'
        self.root.configure(bg=bg)

        # ステータスボックス
        self.status_box = tk.Frame(self.root, bg='#27ae60', width=320, height=90)
        self.status_box.pack(padx=12, pady=(12, 6))
        self.status_box.pack_propagate(False)

        self.status_lbl = tk.Label(
            self.status_box, text='● 授業中 - 全員理解中',
            bg='#27ae60', fg='white',
            font=('Yu Gothic UI', 13, 'bold')
        )
        self.status_lbl.place(relx=0.5, rely=0.5, anchor='center')

        # 時限 + カウント行
        info_row = tk.Frame(self.root, bg=bg)
        info_row.pack(fill=tk.X, padx=12, pady=(0, 4))

        self.period_lbl = tk.Label(
            info_row, text=get_current_period(),
            bg='#333', fg='#f0c040',
            font=('Yu Gothic UI', 10, 'bold'),
            padx=8, pady=2, relief='flat'
        )
        self.period_lbl.pack(side=tk.LEFT)

        self.count_lbl = tk.Label(
            info_row, text='接続: 0人  /  未理解: 0人',
            bg=bg, fg='#aaaaaa',
            font=('Yu Gothic UI', 10)
        )
        self.count_lbl.pack(side=tk.LEFT, padx=(10, 0))

        # ボタン行
        btn_row = tk.Frame(self.root, bg=bg)
        btn_row.pack(padx=12, pady=(0, 12), fill=tk.X)

        tk.Button(
            btn_row, text='⚙ 設定',
            command=self._open_settings,
            bg='#444', fg='white', relief='flat',
            font=('Yu Gothic UI', 9), padx=10, pady=4
        ).pack(side=tk.LEFT)

        tk.Button(
            btn_row, text='📋 ログ',
            command=self._open_log,
            bg='#444', fg='white', relief='flat',
            font=('Yu Gothic UI', 9), padx=10, pady=4
        ).pack(side=tk.LEFT, padx=(6, 0))

        self.top_var = tk.BooleanVar(value=state.config['always_on_top'])
        tk.Checkbutton(
            btn_row, text='最前面に表示',
            variable=self.top_var,
            command=self._toggle_top,
            bg=bg, fg='#cccccc', selectcolor=bg,
            activebackground=bg,
            font=('Yu Gothic UI', 9)
        ).pack(side=tk.RIGHT)

    def _toggle_top(self):
        v = self.top_var.get()
        self.root.attributes('-topmost', v)
        state.config['always_on_top'] = v
        save_config(state.config)

    def _open_settings(self):
        SettingsWin(self.root)

    def _open_log(self):
        LogViewer(self.root)

    def _poll_queue(self):
        try:
            while True:
                state.gui_queue.get_nowait()
                self._refresh()
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _refresh(self):
        total = state.total()
        count = state.cant_count()
        self.period_lbl.config(text=get_current_period())
        self.count_lbl.config(text=f'接続: {total}人  /  未理解: {count}人')

        if state.threshold_exceeded():
            if self._blink_job is None:
                self._start_blink(total, count)
        else:
            self._stop_blink()
            msg = '● 授業中 - 全員理解中' if count == 0 else f'● 授業中  {count}人が未理解'
            self._set_color('#27ae60', msg)

    def _start_blink(self, total, count):
        self._blink_tick(total, count)

    def _blink_tick(self, total, count):
        if not state.threshold_exceeded():
            self._stop_blink()
            return
        self._blink_on = not self._blink_on
        color = '#e74c3c' if self._blink_on else '#c0392b'
        self._set_color(color, f'⚠ 警告!  {count}/{total}人が理解できていません')
        self._blink_job = self.root.after(500, lambda: self._blink_tick(state.total(), state.cant_count()))

    def _stop_blink(self):
        if self._blink_job:
            self.root.after_cancel(self._blink_job)
            self._blink_job = None

    def _set_color(self, color, text):
        self.status_box.config(bg=color)
        self.status_lbl.config(bg=color, text=text)


class SettingsWin:
    def __init__(self, parent):
        win = tk.Toplevel(parent)
        win.title('設定')
        win.resizable(False, False)
        win.grab_set()
        win.attributes('-topmost', True)
        self.win = win

        nb = ttk.Notebook(win)
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        tf = ttk.Frame(nb)
        nb.add(tf, text='先生管理')
        self._teachers_tab(tf)

        thf = ttk.Frame(nb)
        nb.add(thf, text='閾値設定')
        self._threshold_tab(thf)

        tk.Button(
            win, text='保存して閉じる',
            command=self._save,
            font=('Yu Gothic UI', 10),
            bg='#4a90d9', fg='white', relief='flat',
            padx=20, pady=5
        ).pack(pady=10)

    def _teachers_tab(self, f):
        tk.Label(f, text='先生リスト', font=('Yu Gothic UI', 10, 'bold')).pack(anchor='w', padx=10, pady=(10, 4))

        lf = tk.Frame(f)
        lf.pack(fill=tk.BOTH, expand=True, padx=10)

        self.lb = tk.Listbox(lf, height=8, width=28)
        self.lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = tk.Scrollbar(lf, command=self.lb.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.lb.config(yscrollcommand=sb.set)

        for t in state.config['teachers']:
            self.lb.insert(tk.END, t)

        ef = tk.Frame(f)
        ef.pack(fill=tk.X, padx=10, pady=6)

        self.entry = tk.Entry(ef)
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Button(ef, text='追加', command=self._add).pack(side=tk.LEFT, padx=(4, 0))
        tk.Button(ef, text='削除', command=self._del).pack(side=tk.LEFT, padx=(4, 0))

    def _add(self):
        name = self.entry.get().strip()
        if name and name not in state.config['teachers']:
            state.config['teachers'].append(name)
            self.lb.insert(tk.END, name)
            self.entry.delete(0, tk.END)

    def _del(self):
        sel = self.lb.curselection()
        if sel:
            name = self.lb.get(sel[0])
            state.config['teachers'].remove(name)
            self.lb.delete(sel[0])

    def _threshold_tab(self, f):
        tk.Label(f, text='閾値タイプ', font=('Yu Gothic UI', 10, 'bold')).pack(anchor='w', padx=10, pady=(10, 4))
        self.th_type = tk.StringVar(value=state.config['threshold_type'])
        tk.Radiobutton(f, text='割合 (%)', variable=self.th_type, value='percentage').pack(anchor='w', padx=24)
        tk.Radiobutton(f, text='人数', variable=self.th_type, value='count').pack(anchor='w', padx=24)

        vf = tk.Frame(f)
        vf.pack(anchor='w', padx=10, pady=12)
        tk.Label(vf, text='閾値:').pack(side=tk.LEFT)
        self.th_val = tk.IntVar(value=state.config['threshold_value'])
        tk.Spinbox(vf, from_=1, to=200, textvariable=self.th_val, width=6).pack(side=tk.LEFT, padx=6)
        tk.Label(vf, text='%(または 人)').pack(side=tk.LEFT)

    def _save(self):
        state.config['threshold_type'] = self.th_type.get()
        state.config['threshold_value'] = self.th_val.get()
        save_config(state.config)
        self.win.destroy()


# ── Log Viewer ────────────────────────────────────────────
class LogViewer:
    COLS = ('日付', '時限', '学籍番号', 'クラス名', '出席番号', '氏名', '担当教師', '開始時刻', '解除時刻')

    def __init__(self, parent):
        win = tk.Toplevel(parent)
        win.title('授業ログ')
        win.attributes('-topmost', True)
        win.resizable(True, True)
        self.win = win
        self._build(win)
        self._load_today()

    def _build(self, win):
        top = tk.Frame(win)
        top.pack(fill=tk.X, padx=10, pady=(10, 4))

        tk.Label(top, text='日付:').pack(side=tk.LEFT)
        self.date_var = tk.StringVar(value=datetime.now().strftime('%Y-%m-%d'))
        tk.Entry(top, textvariable=self.date_var, width=12).pack(side=tk.LEFT, padx=4)
        tk.Button(top, text='読み込み', command=self._load).pack(side=tk.LEFT)
        tk.Button(top, text='フォルダを開く', command=self._open_dir).pack(side=tk.RIGHT)

        frame = tk.Frame(win)
        frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 4))

        self.tree = ttk.Treeview(frame, columns=self.COLS, show='headings', height=16)
        col_widths = [80, 60, 80, 70, 60, 90, 90, 70, 70]
        for col, w in zip(self.COLS, col_widths):
            self.tree.heading(col, text=col)
            self.tree.column(col, width=w, anchor='center')

        vsb = ttk.Scrollbar(frame, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self.summary = tk.Label(win, text='', fg='#555', font=('Yu Gothic UI', 9))
        self.summary.pack(pady=(0, 8))

    def _load_today(self):
        self._load()

    def _load(self):
        for row in self.tree.get_children():
            self.tree.delete(row)

        date = self.date_var.get().strip()
        log_file = LOG_DIR / f'{date}.csv'
        if not log_file.exists():
            self.summary.config(text=f'{date} のログはありません')
            return

        rows = []
        with open(log_file, 'r', encoding='utf-8-sig', newline='') as f:
            reader = csv.DictReader(f)
            for r in reader:
                rows.append(r)
                self.tree.insert('', tk.END, values=[r.get(c, '') for c in self.COLS])

        # 時限ごとの集計
        from collections import Counter
        period_cnt = Counter(r['時限'] for r in rows)
        summary = '  '.join(f'{p}: {n}件' for p, n in sorted(period_cnt.items()))
        self.summary.config(text=f'合計 {len(rows)} 件  |  {summary}')

    def _open_dir(self):
        LOG_DIR.mkdir(exist_ok=True)
        os.startfile(str(LOG_DIR))


# ── Entry point ───────────────────────────────────────────
def main():
    # tkinter root が必要な messagebox を使うため先に生成
    root = tk.Tk()
    root.withdraw()
    ensure_firewall()
    root.deiconify()

    loop = asyncio.new_event_loop()

    def run_loop():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run_ws())

    threading.Thread(target=run_loop, daemon=True).start()
    threading.Thread(target=run_udp, daemon=True).start()

    TeacherGUI(root)
    root.mainloop()


if __name__ == '__main__':
    main()
