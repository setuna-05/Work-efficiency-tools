# -*- coding: utf-8 -*-
"""フォルダ化/zip化ツール (Windows用)

エクスプローラーで選択したファイル・フォルダを、ショートカットキーで
  - 新しいフォルダへ「移動」
  - 「コピー」してzip化
する常駐アプリ。ショートカットキーは設定画面から変更できます。
"""
import ctypes
import json
import os
import queue
import re
import sys
import threading
from ctypes import wintypes

import tkinter as tk
from tkinter import ttk

import core

APP_NAME = "FolderZipTool"
APP_TITLE = "フォルダ化/zip化ツール"
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
DEFAULT_CONFIG = {
    "hotkey_folder": "Ctrl+Alt+F",
    "hotkey_zip": "Ctrl+Alt+Z",
    "keep_folder": False,   # zip化後に作業フォルダも残すか
    "ask_name": True,       # 実行時に名前を確認するか
    "autostart": False,     # Windows起動時に自動起動
}

WM_HOTKEY, WM_QUIT, WM_USER, WM_APP = 0x0312, 0x0012, 0x0400, 0x8000
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
ID_FOLDER, ID_ZIP = 1, 2
LABELS = {ID_FOLDER: "フォルダ化", ID_ZIP: "zip化"}

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetForegroundWindow.restype = wintypes.HWND
kernel32.GetCurrentThreadId.restype = wintypes.DWORD


# ---------------------------------------------------------------- 設定
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


LOG_PATH = os.path.join(CONFIG_DIR, "log.txt")


def log(msg):
    """動作確認用ログ(%APPDATA%\\FolderZipTool\\log.txt)。"""
    try:
        import datetime
        os.makedirs(CONFIG_DIR, exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 200000:
            os.remove(LOG_PATH)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def parse_hotkey(text):
    """'Ctrl+Alt+F' -> (modifiers, virtual-key)。不正なら None。"""
    parts = [p.strip() for p in (text or "").split("+") if p.strip()]
    if len(parts) < 2:
        return None
    *mod_names, key = parts
    mods = 0
    for m in mod_names:
        m = m.lower()
        if m in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif m == "alt":
            mods |= MOD_ALT
        elif m == "shift":
            mods |= MOD_SHIFT
        else:
            return None
    if len(key) == 1 and key.isascii() and key.isalnum():
        return mods, ord(key.upper())
    m = re.fullmatch(r"[Ff](\d{1,2})", key)
    if m and 1 <= int(m.group(1)) <= 24:
        return mods, 0x70 + int(m.group(1)) - 1
    return None


def autostart_command():
    if getattr(sys, "frozen", False):
        return '"%s" --tray' % sys.executable
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        exe = exe[:-10] + "pythonw.exe"
    return '"%s" "%s" --tray' % (exe, os.path.abspath(__file__))


def set_autostart(enabled):
    import winreg
    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0, winreg.KEY_SET_VALUE,
    )
    try:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
    finally:
        winreg.CloseKey(key)


# ------------------------------------------------- エクスプローラーの選択取得
def _pywin32_inner(hwnd):
    """pywin32経由で、前面にあったエクスプローラー(またはデスクトップ)の選択項目を返す。"""
    import pythoncom
    import win32com.client

    shell = win32com.client.Dispatch("Shell.Application")

    def paths_of(doc):
        out = []
        for item in doc.SelectedItems():
            p = item.Path
            if p and os.path.exists(p):
                out.append(p)
        return out

    # エクスプローラー(Windows 11のタブ複数時も、選択のあるタブを採用)
    for w in shell.Windows():
        try:
            if int(w.HWND) != int(hwnd or 0):
                continue
            paths = paths_of(w.Document)
            if paths:
                return paths
        except Exception:
            continue

    # デスクトップ(ベストエフォート)
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    if buf.value in ("Progman", "WorkerW"):
        try:
            V = win32com.client.VARIANT
            hw = V(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
            disp = shell.Windows().FindWindowSW(
                V(pythoncom.VT_I4, 0), V(pythoncom.VT_I4, 0), 8, hw, 1
            )
            return paths_of(disp.Document)
        except Exception:
            pass
    return []


def _paths_via_pywin32(hwnd):
    import pythoncom
    pythoncom.CoInitialize()
    try:
        return _pywin32_inner(hwnd)
    finally:
        pythoncom.CoUninitialize()


_PS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$target = [int64]%d
$sh = New-Object -ComObject Shell.Application
foreach ($w in $sh.Windows()) {
  try {
    if ([int64]$w.HWND -eq $target) {
      $n = 0
      foreach ($i in $w.Document.SelectedItems()) { [Console]::Out.WriteLine($i.Path); $n++ }
      if ($n -gt 0) { break }
    }
  } catch {}
}
"""


def _paths_via_powershell(hwnd):
    """pywin32が使えない場合の代替(PowerShellでエクスプローラーの選択項目を取得)。"""
    import subprocess
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
           "-Command", _PS_SCRIPT % int(hwnd or 0)]
    res = subprocess.run(cmd, capture_output=True, timeout=20, creationflags=0x08000000)  # CREATE_NO_WINDOW
    text = res.stdout.decode("utf-8", errors="replace")
    return [ln.strip() for ln in text.splitlines() if ln.strip() and os.path.exists(ln.strip())]


def get_selected_paths(hwnd):
    try:
        return _paths_via_pywin32(hwnd)
    except Exception:
        import traceback
        log("pywin32 failed, fallback to PowerShell:\n" + traceback.format_exc())
        return _paths_via_powershell(hwnd)


# ------------------------------------------------------------ ホットキー受信
class HotkeyThread(threading.Thread):
    def __init__(self, on_trigger, on_status):
        super().__init__(daemon=True)
        self.on_trigger = on_trigger
        self.on_status = on_status
        self.ready = threading.Event()
        self.tid = 0
        self.pending = {}

    def run(self):
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, WM_USER, WM_USER, 0)  # メッセージキュー作成
        self.tid = kernel32.GetCurrentThreadId()
        self.ready.set()
        while True:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r in (0, -1):
                break
            if msg.message == WM_HOTKEY:
                try:
                    self.on_trigger(int(msg.wParam), user32.GetForegroundWindow())
                except Exception:
                    pass
            elif msg.message == WM_APP:
                self._apply()
        self._unregister()

    def _unregister(self):
        for hid in (ID_FOLDER, ID_ZIP):
            user32.UnregisterHotKey(None, hid)

    def _apply(self):
        self._unregister()
        errors = []
        for hid, text in self.pending.items():
            parsed = parse_hotkey(text)
            if parsed is None:
                errors.append("%s: 「%s」は無効なキーです" % (LABELS[hid], text))
            elif not user32.RegisterHotKey(None, hid, parsed[0] | MOD_NOREPEAT, parsed[1]):
                errors.append("%s: 「%s」は他のアプリが使用中の可能性があります" % (LABELS[hid], text))
        log("hotkey register: %s errors=%s" % (self.pending, errors))
        self.on_status(errors)

    def set_hotkeys(self, folder_key, zip_key):
        self.ready.wait()
        self.pending = {ID_FOLDER: folder_key, ID_ZIP: zip_key}
        user32.PostThreadMessageW(self.tid, WM_APP, 0, 0)

    def stop(self):
        if self.tid:
            user32.PostThreadMessageW(self.tid, WM_QUIT, 0, 0)


# ------------------------------------------------------------------- 本体
class App:
    def __init__(self, start_hidden):
        self.cfg = load_config()
        self.q = queue.Queue()
        self.busy = threading.Lock()
        self.icon = None

        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.root.withdraw)
        self._build_ui()
        self.root.after(100, self._poll)

        self.hk = HotkeyThread(self._on_hotkey, lambda errs: self.call_main(lambda: self.show_status(errs)))
        self.hk.start()
        self.hk.set_hotkeys(self.cfg["hotkey_folder"], self.cfg["hotkey_zip"])
        self._start_tray()
        if start_hidden:
            self.root.withdraw()

    # --- スレッド間
    def call_main(self, fn):
        self.q.put(fn)

    def _poll(self):
        try:
            while True:
                self.q.get_nowait()()
        except queue.Empty:
            pass
        except Exception:
            pass
        self.root.after(100, self._poll)

    # --- トレイ
    def _start_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.rounded_rectangle((4, 14, 60, 54), 6, fill=(245, 190, 60, 255))
            d.rounded_rectangle((4, 8, 28, 20), 4, fill=(225, 165, 40, 255))
            d.rectangle((26, 26, 38, 46), fill=(255, 255, 255, 255))
            menu = pystray.Menu(
                pystray.MenuItem("設定を開く", lambda *_: self.call_main(self.show_window), default=True),
                pystray.MenuItem("終了", lambda *_: self.call_main(self.quit)),
            )
            self.icon = pystray.Icon(APP_NAME, img, APP_TITLE, menu)
            self.icon.run_detached()
        except Exception:
            self.icon = None

    def notify(self, msg, title=APP_TITLE):
        try:
            if self.icon:
                self.icon.notify(msg, title)
        except Exception:
            pass

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def quit(self):
        self.hk.stop()
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass
        self.root.quit()

    # --- 設定画面
    def _build_ui(self):
        r = self.root
        pad = {"padx": 10, "pady": 4}
        self.var_folder = tk.StringVar(value=self.cfg["hotkey_folder"])
        self.var_zip = tk.StringVar(value=self.cfg["hotkey_zip"])
        self.var_keep = tk.BooleanVar(value=self.cfg["keep_folder"])
        self.var_ask = tk.BooleanVar(value=self.cfg["ask_name"])
        self.var_auto = tk.BooleanVar(value=self.cfg["autostart"])
        self.var_status = tk.StringVar(value="")

        lf = ttk.LabelFrame(r, text="ショートカットキー")
        lf.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        for i, (text, var) in enumerate((("フォルダ化(移動):", self.var_folder), ("zip化(コピー):", self.var_zip))):
            ttk.Label(lf, text=text).grid(row=i, column=0, sticky="w", **pad)
            e = ttk.Entry(lf, textvariable=var, width=22, state="readonly")
            e.grid(row=i, column=1, **pad)
            e.bind("<KeyPress>", lambda ev, v=var: self._capture(ev, v))
            e.bind("<Button-1>", lambda ev: ev.widget.focus_set())
        ttk.Label(
            lf, foreground="#666",
            text="入力欄をクリックして、押したいキーを同時に押す\n(Ctrl/Alt/Shift + 英数字 または F1〜F12)",
        ).grid(row=2, column=0, columnspan=2, sticky="w", **pad)

        of = ttk.LabelFrame(r, text="オプション")
        of.grid(row=1, column=0, sticky="ew", padx=10, pady=4)
        ttk.Checkbutton(of, text="zip化したあとも作業フォルダを残す", variable=self.var_keep).grid(row=0, sticky="w", **pad)
        ttk.Checkbutton(of, text="実行時に名前を確認する", variable=self.var_ask).grid(row=1, sticky="w", **pad)
        ttk.Checkbutton(of, text="Windows起動時に自動で起動する", variable=self.var_auto).grid(row=2, sticky="w", **pad)

        ttk.Button(r, text="保存して適用", command=self.on_save).grid(row=2, column=0, pady=6)
        self.lbl_status = ttk.Label(r, textvariable=self.var_status, foreground="#b00020", wraplength=330, justify="left")
        self.lbl_status.grid(row=3, column=0, sticky="w", padx=10, pady=(0, 10))

    def _capture(self, event, var):
        ks = event.keysym
        if ks in ("Tab", "ISO_Left_Tab"):
            return None
        if ks in ("Control_L", "Control_R", "Shift_L", "Shift_R", "Alt_L", "Alt_R", "Win_L", "Win_R", "Caps_Lock"):
            return "break"
        mods = []
        if event.state & 0x4:
            mods.append("Ctrl")
        if event.state & 0x20000:
            mods.append("Alt")
        if event.state & 0x1:
            mods.append("Shift")
        if len(ks) == 1 and ks.isascii() and ks.isalnum():
            key = ks.upper()
        elif re.fullmatch(r"F\d{1,2}", ks) and 1 <= int(ks[1:]) <= 24:
            key = ks
        else:
            self.var_status.set("使えるのは英数字とF1〜F24です")
            return "break"
        if not mods:
            self.var_status.set("Ctrl / Alt / Shift のどれかと一緒に押してください")
            return "break"
        var.set("+".join(mods + [key]))
        self.var_status.set("")
        return "break"

    def on_save(self):
        f, z = self.var_folder.get(), self.var_zip.get()
        if parse_hotkey(f) is None or parse_hotkey(z) is None:
            self.var_status.set("ショートカットキーが無効です")
            return
        if f.lower() == z.lower():
            self.var_status.set("2つの機能に同じキーは割り当てられません")
            return
        self.cfg.update(
            hotkey_folder=f, hotkey_zip=z,
            keep_folder=self.var_keep.get(), ask_name=self.var_ask.get(), autostart=self.var_auto.get(),
        )
        try:
            save_config(self.cfg)
            set_autostart(self.cfg["autostart"])
        except Exception as e:
            self.var_status.set("設定の保存に失敗: %s" % e)
            return
        self.var_status.set("")
        self.hk.set_hotkeys(f, z)

    def show_status(self, errors):
        if errors:
            self.lbl_status.configure(foreground="#b00020")
            self.var_status.set("\n".join(errors))
            self.notify("\n".join(errors))
        else:
            self.lbl_status.configure(foreground="#1b7a2f")
            self.var_status.set("✔ ショートカットキーを登録しました(動作ログ: %s)" % LOG_PATH)

    def alert(self, msg):
        """通知が見落とされないよう、最前面の小さなウィンドウで知らせる。"""
        def job():
            top = tk.Toplevel(self.root)
            top.title(APP_TITLE)
            top.attributes("-topmost", True)
            top.resizable(False, False)
            ttk.Label(top, text=msg, wraplength=360, justify="left").grid(padx=16, pady=(16, 8))
            ttk.Button(top, text="OK", command=top.destroy).grid(pady=(0, 14))
            top.bind("<Return>", lambda *_: top.destroy())
            top.update_idletasks()
            x = (top.winfo_screenwidth() - top.winfo_width()) // 2
            y = (top.winfo_screenheight() - top.winfo_height()) // 3
            top.geometry("+%d+%d" % (x, y))
            top.lift()
            top.focus_force()

        self.call_main(job)

    # --- アクション
    def _on_hotkey(self, hid, hwnd):
        log("WM_HOTKEY received: id=%s" % hid)
        threading.Thread(target=self.run_action, args=(hid, hwnd), daemon=True).start()

    def _name_dialog(self, title, default):
        top = tk.Toplevel(self.root)
        top.title(title)
        top.attributes("-topmost", True)
        top.resizable(False, False)
        var = tk.StringVar(value=default)
        result = [None]
        ttk.Label(top, text="名前を入力してください:").grid(row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(12, 4))
        ent = ttk.Entry(top, textvariable=var, width=40)
        ent.grid(row=1, column=0, columnspan=2, padx=12, pady=4)

        def ok(*_):
            result[0] = var.get()
            top.destroy()

        ttk.Button(top, text="OK", command=ok).grid(row=2, column=0, sticky="e", padx=6, pady=12)
        ttk.Button(top, text="キャンセル", command=top.destroy).grid(row=2, column=1, sticky="w", padx=6, pady=12)
        top.bind("<Return>", ok)
        top.bind("<Escape>", lambda *_: top.destroy())
        top.update_idletasks()
        x = (top.winfo_screenwidth() - top.winfo_width()) // 2
        y = (top.winfo_screenheight() - top.winfo_height()) // 3
        top.geometry("+%d+%d" % (x, y))
        top.lift()
        top.focus_force()
        ent.focus_set()
        ent.selection_range(0, "end")
        self.root.wait_window(top)
        return result[0]

    def ask_name(self, title, default):
        ev, box = threading.Event(), []

        def job():
            try:
                box.append(self._name_dialog(title, default))
            finally:
                ev.set()

        self.call_main(job)
        ev.wait()
        return box[0] if box else None

    def run_action(self, hid, hwnd):
        if not self.busy.acquire(blocking=False):
            return
        try:
            log("triggered: %s hwnd=%s" % (LABELS[hid], hwnd))
            paths = get_selected_paths(hwnd)
            log("selected: %d items %s" % (len(paths), paths[:3]))
            if not paths:
                self.alert("選択中のファイル・フォルダを取得できませんでした。\n"
                           "エクスプローラーで項目を選択した状態(その画面が一番手前)でキーを押してください。")
                return
            name = core.default_name(paths)
            if self.cfg.get("ask_name", True):
                name = self.ask_name("%s - %d個の項目" % (LABELS[hid], len(paths)), name)
                if name is None:
                    return
            if hid == ID_FOLDER:
                dest = core.move_to_folder(paths, name)
                self.notify("%d個を移動しました\n%s" % (len(paths), dest))
            else:
                zip_path, folder = core.zip_items(paths, name, self.cfg.get("keep_folder", False))
                self.notify("zipを作成しました\n%s" % zip_path)
        except Exception as e:
            import traceback
            log("error: " + traceback.format_exc())
            self.alert("失敗しました:\n%s" % e)
        finally:
            self.busy.release()


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    mutex = kernel32.CreateMutexW(None, False, APP_NAME + "_SingleInstance")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        ctypes.windll.user32.MessageBoxW(0, "すでに起動しています(タスクトレイを確認してください)", APP_TITLE, 0x40)
        return
    app = App(start_hidden="--tray" in sys.argv)
    app.root.mainloop()
    del mutex


if __name__ == "__main__":
    main()
