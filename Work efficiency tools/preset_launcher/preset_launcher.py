#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
プリセットランチャー (Windows 向け)

作業環境ごとに「起動するアプリの組み合わせ」をプリセットとして保存し、
プリセット一覧をダブルクリックするだけで複数のアプリを同時に起動します。
アプリごとにウィンドウ状態 (通常 / 最小化 / 最大化) を指定できます。

使い方:
    python preset_launcher.py                  GUI を開く
    python preset_launcher.py --startup        PC 起動時用 (最小化して開き、自動実行プリセットを起動)
    python preset_launcher.py --run "名前"     GUI なしでプリセットを起動
    python preset_launcher.py --list           プリセット一覧を表示
    python preset_launcher.py --run "名前" --dry-run   起動せず内容だけ表示

設定ファイル:
    Windows : %APPDATA%\\PresetLauncher\\presets.json
    その他  : ~/.config/PresetLauncher/presets.json
    (環境変数 PRESET_LAUNCHER_CONFIG または --config で変更可)

Python 3.8 以降 / 標準ライブラリのみで動作します。
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog, ttk
except ImportError:  # tkinter が無くても --run / --list は使える
    tk = None
    filedialog = messagebox = simpledialog = ttk = None

APP_NAME = "PresetLauncher"
APP_TITLE = "プリセットランチャー"
CONFIG_VERSION = 1

DEFAULT_STATE = "normal"
STATE_LABELS = {"normal": "通常", "minimized": "最小化", "maximized": "最大化"}
STATE_ORDER = ("normal", "minimized", "maximized")

# 作業フォルダ省略時に「そのファイルのあるフォルダ」を使う拡張子
SCRIPT_EXTS = (".exe", ".bat", ".cmd", ".com")


# --------------------------------------------------------------------------
# 設定 (JSON) の読み書き
# --------------------------------------------------------------------------
def default_config_path():
    env = os.environ.get("PRESET_LAUNCHER_CONFIG")
    if env:
        return env
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_NAME, "presets.json")


def clean_path(text):
    """前後の空白と、コピー時に付きがちな引用符を取り除く。"""
    text = (text or "").strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1].strip()
    return text


def make_app(path, args="", workdir="", window=DEFAULT_STATE):
    return {
        "path": clean_path(path),
        "args": (args or "").strip(),
        "workdir": clean_path(workdir),
        "window": window if window in STATE_LABELS else DEFAULT_STATE,
    }


def normalize_config(data):
    """読み込んだ JSON を安全な形に整える (壊れた項目は捨てる)。"""
    cfg = {"version": CONFIG_VERSION, "autostart_preset": "", "presets": []}
    if not isinstance(data, dict):
        return cfg
    names = set()
    presets = data.get("presets")
    if isinstance(presets, list):
        for p in presets:
            if not isinstance(p, dict):
                continue
            name = str(p.get("name", "")).strip()
            if not name or name in names:
                continue
            apps = []
            raw_apps = p.get("apps")
            if isinstance(raw_apps, list):
                for a in raw_apps:
                    if not isinstance(a, dict):
                        continue
                    path = clean_path(str(a.get("path", "")))
                    if not path:
                        continue
                    apps.append(make_app(path, str(a.get("args", "")),
                                         str(a.get("workdir", "")),
                                         a.get("window", DEFAULT_STATE)))
            names.add(name)
            cfg["presets"].append({"name": name, "apps": apps})
    auto = data.get("autostart_preset", "")
    cfg["autostart_preset"] = auto if isinstance(auto, str) and auto in names else ""
    return cfg


def load_config(path):
    """(設定, 警告メッセージ or None) を返す。壊れた設定ファイルは退避して空で始める。"""
    if not os.path.exists(path):
        return normalize_config({}), None
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        backup = "%s.broken-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
        try:
            os.replace(path, backup)
            where = "元のファイルは次の名前で退避しました:\n%s" % backup
        except OSError:
            where = "元のファイルを退避できませんでした。"
        return normalize_config({}), (
            "設定ファイルを読み込めなかったため、空の状態で開始します。\n"
            "原因: %s\n%s" % (exc, where))
    return normalize_config(data), None


def save_config(cfg, path):
    """一時ファイルに書いてから置き換える (書き込み途中の破損を避ける)。"""
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def find_preset(cfg, name):
    for p in cfg["presets"]:
        if p["name"] == name:
            return p
    return None


# --------------------------------------------------------------------------
# 起動処理
# --------------------------------------------------------------------------
def expand(text):
    """~ と環境変数 (%VAR% / $VAR) を展開する。"""
    return os.path.expandvars(os.path.expanduser(text or ""))


def is_url(target):
    return "://" in target


def looks_like_path(target):
    return "/" in target or "\\" in target


def display_name(path):
    if is_url(path):
        return path
    base = os.path.basename(path.replace("\\", "/").rstrip("/"))
    return base or path


def check_target(target):
    """起動前の簡易チェック。問題があればメッセージを返す。"""
    if not target:
        return "パスが空です"
    if is_url(target) or not looks_like_path(target):
        return None  # URL や "notepad" のようなコマンド名は起動側に任せる
    if not os.path.exists(target):
        return "ファイルが見つかりません: %s" % target
    return None


def resolve_workdir(target, workdir):
    """(作業フォルダ or None, エラー or None)"""
    if workdir:
        if not os.path.isdir(workdir):
            return None, "作業フォルダが見つかりません: %s" % workdir
        return workdir, None
    if os.path.isfile(target) and target.lower().endswith(SCRIPT_EXTS):
        return os.path.dirname(os.path.abspath(target)), None
    return None, None


class PosixBackend:
    """Windows 以外用 (開発・動作確認用)。ウィンドウ状態の指定は無視される。"""

    def launch(self, app):
        target = expand(clean_path(app["path"]))
        err = check_target(target)
        if err:
            return err
        try:
            argv = [target] + shlex.split(expand(app.get("args", "")))
        except ValueError as exc:
            return "引数を解釈できません: %s" % exc
        cwd, err = resolve_workdir(target, expand(clean_path(app.get("workdir", ""))))
        if err:
            return err
        try:
            proc = subprocess.Popen(
                argv, cwd=cwd, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
        except OSError as exc:
            return "起動に失敗しました: %s" % exc
        threading.Thread(target=proc.wait, daemon=True).start()  # ゾンビ回収
        return None

    def wait_pending(self, timeout=0.0):
        pass


class WindowsBackend:
    """ShellExecuteEx で起動し、必要なら起動後にウィンドウ状態を補正する。

    nShow (SW_*) を渡すだけでは無視するアプリがあるため、最小化/最大化の指定が
    あるアプリは、起動後に新しく現れたウィンドウへ ShowWindowAsync を適用する。
    """

    SHOW_CMD = {"normal": 1, "minimized": 7, "maximized": 3}  # SW_SHOWNORMAL / SW_SHOWMINNOACTIVE / SW_SHOWMAXIMIZED
    FIND_TIMEOUT = 20.0   # ウィンドウが現れるのを待つ最大秒数
    SETTLE_TIME = 3.0     # 最初のウィンドウを処理した後、追加ウィンドウを待つ秒数
    POLL_INTERVAL = 0.25

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SEE_MASK_NOASYNC = 0x00000100
    SEE_MASK_FLAG_NO_UI = 0x00000400

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._ct = ctypes
        wt = wintypes

        class SHELLEXECUTEINFOW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wt.DWORD),
                ("fMask", ctypes.c_ulong),
                ("hwnd", wt.HWND),
                ("lpVerb", wt.LPCWSTR),
                ("lpFile", wt.LPCWSTR),
                ("lpParameters", wt.LPCWSTR),
                ("lpDirectory", wt.LPCWSTR),
                ("nShow", ctypes.c_int),
                ("hInstApp", wt.HINSTANCE),
                ("lpIDList", ctypes.c_void_p),
                ("lpClass", wt.LPCWSTR),
                ("hkeyClass", wt.HKEY),
                ("dwHotKey", wt.DWORD),
                ("hIconOrMonitor", wt.HANDLE),
                ("hProcess", wt.HANDLE),
            ]

        self._Info = SHELLEXECUTEINFOW
        self._DWORD = wt.DWORD
        self._EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

        self.shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.ole32 = ctypes.WinDLL("ole32")

        self.shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
        self.shell32.ShellExecuteExW.restype = wt.BOOL
        self.kernel32.GetProcessId.argtypes = [wt.HANDLE]
        self.kernel32.GetProcessId.restype = wt.DWORD
        self.kernel32.CloseHandle.argtypes = [wt.HANDLE]
        self.kernel32.CloseHandle.restype = wt.BOOL
        self.kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        self.kernel32.OpenProcess.restype = wt.HANDLE
        self.kernel32.QueryFullProcessImageNameW.argtypes = [
            wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
        self.kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
        self.user32.EnumWindows.argtypes = [self._EnumProc, wt.LPARAM]
        self.user32.EnumWindows.restype = wt.BOOL
        self.user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
        self.user32.GetWindowThreadProcessId.restype = wt.DWORD
        self.user32.IsWindowVisible.argtypes = [wt.HWND]
        self.user32.IsWindowVisible.restype = wt.BOOL
        self.user32.GetWindow.argtypes = [wt.HWND, wt.UINT]
        self.user32.GetWindow.restype = wt.HWND
        self.user32.GetWindowTextLengthW.argtypes = [wt.HWND]
        self.user32.GetWindowTextLengthW.restype = ctypes.c_int
        self.user32.ShowWindowAsync.argtypes = [wt.HWND, ctypes.c_int]
        self.user32.ShowWindowAsync.restype = wt.BOOL
        self.ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wt.DWORD]
        self.ole32.CoInitializeEx.restype = ctypes.c_long
        self.ole32.CoUninitialize.argtypes = []
        self.ole32.CoUninitialize.restype = None

        self._pending = []
        self._lock = threading.Lock()

    # ---- ウィンドウ列挙 ----
    def _top_windows(self):
        """可視・オーナー無し・タイトルありのトップレベルウィンドウ [(hwnd, pid)]"""
        found = []
        user32 = self.user32

        def callback(hwnd, _lparam):
            if (user32.IsWindowVisible(hwnd)
                    and not user32.GetWindow(hwnd, 4)  # GW_OWNER
                    and user32.GetWindowTextLengthW(hwnd) > 0):
                pid = self._DWORD(0)
                user32.GetWindowThreadProcessId(hwnd, self._ct.byref(pid))
                found.append((hwnd, pid.value))
            return True

        proc = self._EnumProc(callback)  # EnumWindows の間だけ参照を保持
        user32.EnumWindows(proc, 0)
        return found

    def _image_name(self, pid):
        handle = self.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            buf = self._ct.create_unicode_buffer(1024)
            size = self._DWORD(1024)
            if self.kernel32.QueryFullProcessImageNameW(handle, 0, buf, self._ct.byref(size)):
                return os.path.basename(buf.value).lower()
        finally:
            self.kernel32.CloseHandle(handle)
        return ""

    def _image_path(self, pid):
        """プロセスの実行ファイル絶対パスを取得する。"""
        handle = self.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return ""
        try:
            buf = self._ct.create_unicode_buffer(32768)
            size = self._DWORD(len(buf))
            if self.kernel32.QueryFullProcessImageNameW(
                    handle, 0, buf, self._ct.byref(size)):
                return os.path.normcase(os.path.abspath(buf.value))
        finally:
            self.kernel32.CloseHandle(handle)
        return ""

    def _resolve_shortcut(self, path):
        """.lnk のリンク先を Windows ShellLink COM API で取得する。"""
        class GUID(self._ct.Structure):
            _fields_ = [("Data1", self._ct.c_uint32), ("Data2", self._ct.c_uint16),
                        ("Data3", self._ct.c_uint16), ("Data4", self._ct.c_ubyte * 8)]

        def make_guid(value):
            raw = self._ct.create_unicode_buffer(value)
            guid = GUID()
            self.ole32.IIDFromString(raw, self._ct.byref(guid))
            return guid

        ole = self.ole32
        ole.IIDFromString.argtypes = [self._ct.c_wchar_p, self._ct.POINTER(GUID)]
        ole.IIDFromString.restype = self._ct.c_long
        ole.CoCreateInstance.argtypes = [self._ct.POINTER(GUID), self._ct.c_void_p,
                                         self._ct.c_uint32, self._ct.POINTER(GUID),
                                         self._ct.POINTER(self._ct.c_void_p)]
        ole.CoCreateInstance.restype = self._ct.c_long
        initialized = ole.CoInitializeEx(None, 0x2 | 0x4)  # STA
        if initialized not in (0, 1, -2147417850):  # allow RPC_E_CHANGED_MODE
            return ""
        link = self._ct.c_void_p()
        persist = self._ct.c_void_p()
        try:
            clsid = make_guid("{00021401-0000-0000-C000-000000000046}")
            iid_link = make_guid("{000214F9-0000-0000-C000-000000000046}")
            iid_persist = make_guid("{0000010B-0000-0000-C000-000000000046}")
            hr = ole.CoCreateInstance(self._ct.byref(clsid), None, 1,
                                      self._ct.byref(iid_link), self._ct.byref(link))
            if hr < 0 or not link:
                return ""
            vtable = self._ct.cast(link, self._ct.POINTER(self._ct.POINTER(self._ct.c_void_p))).contents
            query = self._ct.WINFUNCTYPE(self._ct.c_long, self._ct.c_void_p,
                                          self._ct.POINTER(GUID), self._ct.POINTER(self._ct.c_void_p))(vtable[0])
            if query(link, self._ct.byref(iid_persist), self._ct.byref(persist)) < 0 or not persist:
                return ""
            pvtable = self._ct.cast(persist, self._ct.POINTER(self._ct.POINTER(self._ct.c_void_p))).contents
            load = self._ct.WINFUNCTYPE(self._ct.c_long, self._ct.c_void_p,
                                        self._ct.c_wchar_p, self._ct.c_uint32)(pvtable[5])
            if load(persist, path, 0) < 0:
                return ""
            lvtable = self._ct.cast(link, self._ct.POINTER(self._ct.POINTER(self._ct.c_void_p))).contents
            get_path = self._ct.WINFUNCTYPE(self._ct.c_long, self._ct.c_void_p,
                                            self._ct.c_wchar_p, self._ct.c_int,
                                            self._ct.c_void_p, self._ct.c_uint32)(lvtable[3])
            buffer = self._ct.create_unicode_buffer(32768)
            if get_path(link, buffer, len(buffer), None, 0) < 0:
                return ""
            return buffer.value
        except Exception:
            return ""
        finally:
            release_type = self._ct.WINFUNCTYPE(self._ct.c_uint32, self._ct.c_void_p)
            for obj in (persist, link):
                if obj:
                    vtable = self._ct.cast(obj, self._ct.POINTER(self._ct.POINTER(self._ct.c_void_p))).contents
                    release_type(vtable[2])(obj)
            if initialized in (0, 1):
                ole.CoUninitialize()

    def stop_apps(self, apps):
        """登録された実行ファイルの全ウィンドウへ通常終了 (WM_CLOSE) を送る。"""
        exact_paths = set()
        command_names = set()
        unsupported = []
        for app in apps:
            target = expand(clean_path(app.get("path", "")))
            lower = target.lower()
            if not target or "://" in lower or lower.startswith(("mailto:", "ms-settings:")):
                unsupported.append(display_name(target) if target else "(空の登録)")
                continue
            ext = os.path.splitext(target)[1].lower()
            if ext == ".lnk":
                target = expand(self._resolve_shortcut(target))
                if not target:
                    unsupported.append(display_name(app.get("path", "")))
                    continue
                ext = os.path.splitext(target)[1].lower()
            if ext not in (".exe", ".com"):
                if not ext and not (chr(92) in target or "/" in target):
                    command_names.add((target if target.lower().endswith(".exe")
                                       else target + ".exe").lower())
                else:
                    unsupported.append(display_name(app.get("path", "")))
                continue
            if os.path.isabs(target):
                exact_paths.add(os.path.normcase(os.path.abspath(target)))
            else:
                command_names.add(os.path.basename(target).lower())

        visible = []
        def callback(hwnd, _lparam):
            if self.user32.IsWindowVisible(hwnd) and not self.user32.GetWindow(hwnd, 4):
                pid = self._DWORD(0)
                self.user32.GetWindowThreadProcessId(hwnd, self._ct.byref(pid))
                visible.append((hwnd, pid.value))
            return True

        enum_proc = self._EnumProc(callback)
        self.user32.EnumWindows(enum_proc, 0)
        self.user32.PostMessageW.argtypes = [self._ct.c_void_p, self._DWORD,
                                             self._ct.c_size_t, self._ct.c_ssize_t]
        self.user32.PostMessageW.restype = self._ct.c_int
        matched = sent = 0
        for hwnd, pid in visible:
            image = self._image_path(pid)
            if not image:
                continue
            is_target = image in exact_paths or os.path.basename(image).lower() in command_names
            if is_target:
                matched += 1
                if self.user32.PostMessageW(hwnd, 0x0010, 0, 0):  # WM_CLOSE
                    sent += 1
        return {"matched": matched, "sent": sent, "unsupported": unsupported}

    # ---- 起動 ----
    def launch(self, app):
        target = expand(clean_path(app["path"]))
        err = check_target(target)
        if err:
            return err
        workdir, err = resolve_workdir(target, expand(clean_path(app.get("workdir", ""))))
        if err:
            return err
        args = expand(app.get("args", "")).strip()
        state = app.get("window", DEFAULT_STATE)
        if state not in self.SHOW_CMD:
            state = DEFAULT_STATE

        # ShellExecuteEx を呼ぶスレッドでは COM を初期化しておく (推奨)
        hr = self.ole32.CoInitializeEx(None, 0x2 | 0x4)  # APARTMENTTHREADED | DISABLE_OLE1DDE
        try:
            return self._shell_execute(target, args, workdir, state)
        finally:
            if hr in (0, 1):  # S_OK / S_FALSE のときだけ対で解除
                self.ole32.CoUninitialize()

    def _shell_execute(self, target, args, workdir, state):
        before = None
        if state != "normal":
            before = set(h for h, _ in self._top_windows())

        info = self._Info()
        info.cbSize = self._ct.sizeof(info)
        info.fMask = (self.SEE_MASK_NOCLOSEPROCESS | self.SEE_MASK_NOASYNC
                      | self.SEE_MASK_FLAG_NO_UI)
        info.lpVerb = "open"
        info.lpFile = target
        info.lpParameters = args or None
        info.lpDirectory = workdir or None
        info.nShow = self.SHOW_CMD[state]

        if not self.shell32.ShellExecuteExW(self._ct.byref(info)):
            code = self._ct.get_last_error()
            return "起動に失敗しました (エラー %s): %s" % (code, self._ct.FormatError(code).strip())

        pid = 0
        if info.hProcess:
            pid = self.kernel32.GetProcessId(info.hProcess)
            self.kernel32.CloseHandle(info.hProcess)

        if state != "normal":
            thread = threading.Thread(
                target=self._enforce_state,
                args=(pid, os.path.basename(target).lower(), before, state),
                daemon=True)
            with self._lock:
                self._pending.append(thread)
            thread.start()
        return None

    def _enforce_state(self, pid, exe_name, before, state):
        """起動したアプリの新しいウィンドウに最小化/最大化を適用する。"""
        cmd = self.SHOW_CMD[state]
        handled = set()
        started = time.time()
        first_hit = None
        while True:
            now = time.time()
            if first_hit is None:
                if now - started > self.FIND_TIMEOUT:
                    return
            elif now - first_hit > self.SETTLE_TIME:
                return
            try:
                for hwnd, wpid in self._top_windows():
                    if hwnd in handled:
                        continue
                    mine = bool(pid) and wpid == pid
                    if not mine and hwnd not in before and exe_name:
                        mine = self._image_name(wpid) == exe_name
                    if mine:
                        self.user32.ShowWindowAsync(hwnd, cmd)
                        handled.add(hwnd)
                        if first_hit is None:
                            first_hit = time.time()
            except Exception:
                return
            time.sleep(self.POLL_INTERVAL)

    def wait_pending(self, timeout=30.0):
        """ウィンドウ状態の補正スレッドが終わるまで待つ (GUI なし実行用)。"""
        deadline = time.time() + timeout
        with self._lock:
            threads = list(self._pending)
        for thread in threads:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            thread.join(remaining)


class DryRunBackend:
    def __init__(self):
        self.lines = []

    def launch(self, app):
        self.lines.append(app)
        return None

    def wait_pending(self, timeout=0.0):
        pass


def make_backend():
    return WindowsBackend() if os.name == "nt" else PosixBackend()


def launch_preset(preset, backend):
    """プリセット内の全アプリを同時に起動する。[(app, エラー or None)] を元の順で返す。"""
    apps = list(preset.get("apps", []))
    if not apps:
        return []

    def one(app):
        try:
            return app, backend.launch(app)
        except Exception as exc:  # 1 つの失敗で他を止めない
            return app, "%s: %s" % (type(exc).__name__, exc)

    with ThreadPoolExecutor(max_workers=min(len(apps), 16)) as pool:
        return list(pool.map(one, apps))


def safe_print(text, stream=None):
    stream = stream or sys.stdout
    if stream is None:  # pythonw では標準出力が無い
        return
    enc = getattr(stream, "encoding", None) or "utf-8"
    try:
        stream.write(text + "\n")
    except UnicodeEncodeError:
        stream.write(text.encode(enc, "replace").decode(enc) + "\n")
    stream.flush()


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
class AppDialog:
    """アプリ 1 件の追加・編集ダイアログ。"""

    def __init__(self, parent, title, app=None):
        self.parent = parent
        self.result = None
        app = app or {}
        top = self.top = tk.Toplevel(parent)
        top.title(title)
        top.transient(parent)
        top.resizable(False, False)

        self.path_var = tk.StringVar(value=app.get("path", ""))
        self.args_var = tk.StringVar(value=app.get("args", ""))
        self.dir_var = tk.StringVar(value=app.get("workdir", ""))
        self.state_var = tk.StringVar(value=app.get("window", DEFAULT_STATE))

        frm = ttk.Frame(top, padding=12)
        frm.grid(row=0, column=0, sticky="nsew")

        ttk.Label(frm, text="アプリ / ファイル / URL").grid(row=0, column=0, sticky="w", pady=4)
        self.path_entry = ttk.Entry(frm, textvariable=self.path_var, width=58)
        self.path_entry.grid(row=0, column=1, padx=6, pady=4)
        ttk.Button(frm, text="参照…", command=self._browse_path).grid(row=0, column=2, pady=4)

        ttk.Label(frm, text="引数 (省略可)").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=self.args_var, width=58).grid(row=1, column=1, padx=6, pady=4)

        ttk.Label(frm, text="作業フォルダ (省略可)").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=self.dir_var, width=58).grid(row=2, column=1, padx=6, pady=4)
        ttk.Button(frm, text="参照…", command=self._browse_dir).grid(row=2, column=2, pady=4)

        ttk.Label(frm, text="ウィンドウ").grid(row=3, column=0, sticky="w", pady=4)
        box = ttk.Frame(frm)
        box.grid(row=3, column=1, sticky="w", padx=6, pady=4)
        for key in STATE_ORDER:
            ttk.Radiobutton(box, text=STATE_LABELS[key], value=key,
                            variable=self.state_var).pack(side="left", padx=(0, 12))

        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="OK", command=self._ok).pack(side="left", padx=4)
        ttk.Button(btns, text="キャンセル", command=self._cancel).pack(side="left", padx=4)

        top.bind("<Return>", lambda _e: self._ok())
        top.bind("<Escape>", lambda _e: self._cancel())
        top.protocol("WM_DELETE_WINDOW", self._cancel)

    def show(self):
        self.top.update_idletasks()
        try:
            self.top.grab_set()
        except tk.TclError:
            pass
        self.path_entry.focus_set()
        self.parent.wait_window(self.top)
        return self.result

    def _browse_path(self):
        path = filedialog.askopenfilename(
            parent=self.top, title="アプリを選択",
            filetypes=[("アプリ / ショートカット", "*.exe *.lnk *.bat *.cmd"),
                       ("すべてのファイル", "*.*")])
        if path:
            self.path_var.set(os.path.normpath(path))

    def _browse_dir(self):
        path = filedialog.askdirectory(parent=self.top, title="作業フォルダを選択")
        if path:
            self.dir_var.set(os.path.normpath(path))

    def _ok(self):
        app = make_app(self.path_var.get(), self.args_var.get(),
                       self.dir_var.get(), self.state_var.get())
        if not app["path"]:
            messagebox.showwarning(APP_TITLE, "アプリのパスを入力してください。", parent=self.top)
            return
        self.result = app
        self.top.destroy()

    def _cancel(self):
        self.result = None
        self.top.destroy()


class LauncherGUI:
    def __init__(self, root, config_path, backend=None, startup=False):
        self.root = root
        self._configure_dark_theme()
        self.config_path = config_path
        self.backend = backend or make_backend()
        self._q = queue.Queue()
        self.cfg, warning = load_config(config_path)

        root.title(APP_TITLE)
        root.geometry("880x500")
        root.minsize(740, 380)
        self._build()
        self._refresh_presets(select=0 if self.cfg["presets"] else None)
        self._poll()

        if not self.cfg["presets"]:
            self.status("「新規」ボタンでプリセットを作成してください。")
        if warning:
            root.after(200, lambda: messagebox.showwarning(APP_TITLE, warning))
        if startup:
            root.iconify()
            root.after(800, self._run_autostart)

    def _configure_dark_theme(self):
        """Tk/ttk のウィジェットを読みやすいダーク配色に統一する。"""
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        bg = "#1e1e1e"
        panel = "#252526"
        field = "#2d2d30"
        fg = "#e6e6e6"
        muted = "#a0a0a0"
        accent = "#0e639c"
        hover = "#3e3e42"
        self.root.configure(background=bg)
        style.configure(".", background=bg, foreground=fg, fieldbackground=field,
                        bordercolor="#454545", lightcolor="#454545",
                        darkcolor="#454545", troughcolor=panel,
                        font=("Yu Gothic UI", 9))
        style.configure("TFrame", background=bg)
        style.configure("TLabelframe", background=bg, foreground=fg,
                        bordercolor="#454545")
        style.configure("TLabelframe.Label", background=bg, foreground=fg)
        style.configure("TLabel", background=bg, foreground=fg)
        style.configure("TButton", background="#333337", foreground=fg,
                        bordercolor="#505055", padding=(8, 4))
        style.map("TButton", background=[("active", hover), ("pressed", accent)],
                  foreground=[("disabled", muted)])
        style.configure("TEntry", fieldbackground=field, foreground=fg,
                        insertcolor=fg, bordercolor="#505055")
        style.configure("TRadiobutton", background=bg, foreground=fg)
        style.map("TRadiobutton", background=[("active", bg)],
                  foreground=[("disabled", muted)])
        style.configure("TCombobox", fieldbackground=field, background="#333337",
                        foreground=fg, arrowcolor=fg)
        style.map("TCombobox", fieldbackground=[("readonly", field)],
                  foreground=[("readonly", fg)],
                  selectbackground=[("readonly", accent)],
                  selectforeground=[("readonly", "#ffffff")])
        style.configure("Treeview", background=field, fieldbackground=field,
                        foreground=fg, bordercolor="#454545", rowheight=25)
        style.configure("Treeview.Heading", background="#333337", foreground=fg,
                        relief="flat")
        style.map("Treeview", background=[("selected", accent)],
                  foreground=[("selected", "#ffffff")])
        style.map("Treeview.Heading", background=[("active", hover)])
        style.configure("Vertical.TScrollbar", background="#333337",
                        troughcolor=panel, arrowcolor=fg, bordercolor=panel)
        self._dark_colors = {"background": bg, "fieldbackground": field,
                             "foreground": fg, "selectbackground": accent,
                             "selectforeground": "#ffffff"}

    # ---- 画面構築 ----
    def _build(self):
        root = self.root
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)

        # 左: プリセット一覧
        left = ttk.LabelFrame(root, text="プリセット", padding=8)
        left.grid(row=0, column=0, sticky="ns", padx=(10, 5), pady=(10, 5))
        left.rowconfigure(0, weight=1)
        self.preset_list = tk.Listbox(
            left, width=24, exportselection=False, activestyle="none",
            background="#2d2d30", foreground="#e6e6e6",
            selectbackground="#0e639c", selectforeground="#ffffff",
            highlightthickness=1, highlightbackground="#454545",
            highlightcolor="#0e639c", relief="flat")
        self.preset_list.grid(row=0, column=0, sticky="ns")
        sb = ttk.Scrollbar(left, orient="vertical", command=self.preset_list.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.preset_list.configure(yscrollcommand=sb.set)
        self.preset_list.bind("<<ListboxSelect>>", lambda _e: self._on_preset_select())
        self.preset_list.bind("<Double-Button-1>", self._on_preset_double_click)
        self.preset_list.bind("<Return>", lambda _e: self.launch_selected())
        pb = ttk.Frame(left)
        pb.grid(row=1, column=0, columnspan=2, pady=(8, 0))
        ttk.Button(pb, text="新規", width=7, command=self.add_preset).pack(side="left", padx=2)
        ttk.Button(pb, text="名前変更", width=8, command=self.rename_preset).pack(side="left", padx=2)
        ttk.Button(pb, text="削除", width=7, command=self.delete_preset).pack(side="left", padx=2)
        ttk.Label(left, text="ダブルクリックで起動", foreground="#a0a0a0").grid(
            row=2, column=0, columnspan=2, pady=(8, 0))

        # 右: アプリ一覧
        right = ttk.LabelFrame(root, text="アプリ (選択中のプリセット)", padding=8)
        right.grid(row=0, column=1, sticky="nsew", padx=(5, 10), pady=(10, 5))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)
        cols = ("name", "window", "path", "args")
        self.tree = ttk.Treeview(right, columns=cols, show="headings", selectmode="extended")
        for col, text, width, stretch in (("name", "アプリ", 150, False),
                                          ("window", "ウィンドウ", 80, False),
                                          ("path", "パス", 300, True),
                                          ("args", "引数", 130, False)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, stretch=stretch, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        tsb = ttk.Scrollbar(right, orient="vertical", command=self.tree.yview)
        tsb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.bind("<Double-1>", self._on_tree_double_click)

        ab = ttk.Frame(right)
        ab.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(ab, text="ファイルを選んで追加", command=self.add_apps_by_browse).pack(side="left", padx=2)
        ttk.Button(ab, text="手入力で追加", command=self.add_app_by_input).pack(side="left", padx=2)
        ttk.Button(ab, text="編集", command=self.edit_app).pack(side="left", padx=2)
        ttk.Button(ab, text="削除", command=self.remove_apps).pack(side="left", padx=2)
        ttk.Button(ab, text="↑", width=3, command=lambda: self.move_apps(-1)).pack(side="left", padx=(10, 2))
        ttk.Button(ab, text="↓", width=3, command=lambda: self.move_apps(1)).pack(side="left", padx=2)

        sb2 = ttk.Frame(right)
        sb2.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(sb2, text="選択したアプリのウィンドウ状態:").pack(side="left", padx=(2, 6))
        for key in STATE_ORDER:
            ttk.Button(sb2, text=STATE_LABELS[key], width=7,
                       command=lambda k=key: self.set_state(k)).pack(side="left", padx=2)

        # 下: 起動ボタンと自動実行設定
        bottom = ttk.Frame(root)
        bottom.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 4))
        bottom.columnconfigure(1, weight=1)
        ttk.Button(bottom, text="▶ このプリセットを起動", command=self.launch_selected).grid(
            row=0, column=0, sticky="w")
        ttk.Button(bottom, text="■ このプリセットを終了", command=self.stop_selected).grid(
            row=0, column=1, sticky="w", padx=(8, 0))
        auto = ttk.Frame(bottom)
        auto.grid(row=0, column=2, sticky="e")
        ttk.Label(auto, text="PC起動時に自動実行するプリセット:").pack(side="left", padx=(0, 6))
        self.auto_var = tk.StringVar()
        self.auto_combo = ttk.Combobox(auto, textvariable=self.auto_var, state="readonly", width=24)
        self.auto_combo.pack(side="left")
        self.auto_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_autostart_changed())

        self.status_var = tk.StringVar()
        ttk.Label(root, textvariable=self.status_var, anchor="w",
                  padding=(6, 3), background="#252526", foreground="#c8c8c8").grid(
                      row=2, column=0, columnspan=2, sticky="ew")
    # ---- 共通 ----
    NO_AUTOSTART = "(なし)"

    def status(self, text):
        self.status_var.set(text)

    def _save(self):
        try:
            save_config(self.cfg, self.config_path)
            return True
        except OSError as exc:
            messagebox.showerror(APP_TITLE, "設定を保存できませんでした:\n%s" % exc)
            return False

    def current_index(self):
        sel = self.preset_list.curselection()
        return int(sel[0]) if sel else None

    def current_preset(self):
        idx = self.current_index()
        if idx is None or idx >= len(self.cfg["presets"]):
            return None
        return self.cfg["presets"][idx]

    def _selected_app_indices(self):
        return sorted(int(i) for i in self.tree.selection())

    def _refresh_presets(self, select=None):
        self.preset_list.delete(0, "end")
        for p in self.cfg["presets"]:
            self.preset_list.insert("end", p["name"])
        if select is not None and self.cfg["presets"]:
            select = max(0, min(select, len(self.cfg["presets"]) - 1))
            self.preset_list.selection_set(select)
            self.preset_list.activate(select)
            self.preset_list.see(select)
        self._refresh_autostart_combo()
        self._refresh_apps()

    def _refresh_autostart_combo(self):
        names = [self.NO_AUTOSTART] + [p["name"] for p in self.cfg["presets"]]
        self.auto_combo.configure(values=names)
        self.auto_var.set(self.cfg["autostart_preset"] or self.NO_AUTOSTART)

    def _refresh_apps(self, select=()):
        self.tree.delete(*self.tree.get_children())
        preset = self.current_preset()
        if not preset:
            return
        for i, app in enumerate(preset["apps"]):
            self.tree.insert("", "end", iid=str(i), values=(
                display_name(app["path"]), STATE_LABELS[app["window"]], app["path"], app["args"]))
        for i in select:
            if 0 <= i < len(preset["apps"]):
                self.tree.selection_add(str(i))

    # ---- プリセット操作 ----
    def _on_preset_select(self):
        self._refresh_apps()

    def _on_preset_double_click(self, event):
        idx = self.preset_list.nearest(event.y)
        box = self.preset_list.bbox(idx)
        if not box or not (box[1] <= event.y <= box[1] + box[3]):
            return  # 項目の無い空白部分のダブルクリックでは起動しない
        self.preset_list.selection_clear(0, "end")
        self.preset_list.selection_set(idx)
        self._on_preset_select()
        self.launch_selected()

    def _ask_name(self, title, initial=""):
        name = simpledialog.askstring(APP_TITLE, title, initialvalue=initial, parent=self.root)
        return None if name is None else name.strip()

    def _name_error(self, name, ignore=None):
        if not name:
            return "プリセット名を入力してください。"
        for p in self.cfg["presets"]:
            if p["name"] == name and p is not ignore:
                return "同じ名前のプリセットが既にあります。"
        return None

    def add_preset(self):
        name = self._ask_name("新しいプリセットの名前:", "新しいプリセット")
        if name is None:
            return
        return self._create_preset(name)

    def _create_preset(self, name):
        err = self._name_error(name)
        if err:
            messagebox.showwarning(APP_TITLE, err, parent=self.root)
            return False
        self.cfg["presets"].append({"name": name, "apps": []})
        self._save()
        self._refresh_presets(select=len(self.cfg["presets"]) - 1)
        self.status("プリセット「%s」を作成しました。アプリを追加してください。" % name)
        return True

    def rename_preset(self):
        preset = self.current_preset()
        if not preset:
            return
        name = self._ask_name("プリセット名:", preset["name"])
        if name is None:
            return
        return self._rename_preset(preset, name)

    def _rename_preset(self, preset, name):
        err = self._name_error(name, ignore=preset)
        if err:
            messagebox.showwarning(APP_TITLE, err, parent=self.root)
            return False
        if self.cfg["autostart_preset"] == preset["name"]:
            self.cfg["autostart_preset"] = name
        preset["name"] = name
        self._save()
        self._refresh_presets(select=self.current_index())
        return True

    def delete_preset(self):
        preset = self.current_preset()
        if not preset:
            return
        if not messagebox.askyesno(APP_TITLE, "プリセット「%s」を削除しますか?" % preset["name"],
                                   parent=self.root):
            return
        self._delete_preset(preset)

    def _delete_preset(self, preset):
        idx = self.current_index()
        self.cfg["presets"].remove(preset)
        if self.cfg["autostart_preset"] == preset["name"]:
            self.cfg["autostart_preset"] = ""
        self._save()
        self._refresh_presets(select=idx)

    def _on_autostart_changed(self):
        value = self.auto_var.get()
        self.cfg["autostart_preset"] = "" if value == self.NO_AUTOSTART else value
        self._save()

    # ---- アプリ操作 ----
    def _need_preset(self):
        preset = self.current_preset()
        if not preset:
            messagebox.showinfo(APP_TITLE, "先にプリセットを選択、または作成してください。", parent=self.root)
        return preset

    def add_apps_by_browse(self):
        if not self._need_preset():
            return
        paths = filedialog.askopenfilenames(
            parent=self.root, title="起動するアプリを選択 (複数選択可)",
            filetypes=[("アプリ / ショートカット", "*.exe *.lnk *.bat *.cmd"),
                       ("すべてのファイル", "*.*")])
        self._add_paths(paths)

    def _add_paths(self, paths):
        preset = self.current_preset()
        if not preset or not paths:
            return
        start = len(preset["apps"])
        for p in paths:
            preset["apps"].append(make_app(os.path.normpath(p)))
        self._save()
        self._refresh_apps(select=range(start, len(preset["apps"])))
        self.status("%d 件追加しました。ウィンドウ状態は下のボタンで変更できます。" % len(paths))

    def add_app_by_input(self):
        if not self._need_preset():
            return
        app = AppDialog(self.root, "アプリを追加").show()
        if app:
            self._add_app(app)

    def _add_app(self, app):
        preset = self.current_preset()
        if not preset:
            return
        preset["apps"].append(app)
        self._save()
        self._refresh_apps(select=[len(preset["apps"]) - 1])

    def edit_app(self):
        preset = self.current_preset()
        sel = self._selected_app_indices()
        if not preset or not sel:
            return
        idx = sel[0]
        app = AppDialog(self.root, "アプリを編集", preset["apps"][idx]).show()
        if app:
            self._replace_app(idx, app)

    def _replace_app(self, idx, app):
        preset = self.current_preset()
        preset["apps"][idx] = app
        self._save()
        self._refresh_apps(select=[idx])

    def _on_tree_double_click(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            self.edit_app()

    def remove_apps(self):
        preset = self.current_preset()
        sel = self._selected_app_indices()
        if not preset or not sel:
            return
        for i in reversed(sel):
            del preset["apps"][i]
        self._save()
        self._refresh_apps()

    def move_apps(self, delta):
        preset = self.current_preset()
        sel = self._selected_app_indices()
        if not preset or not sel:
            return
        apps = preset["apps"]
        chosen = set(sel)
        if delta < 0:
            for i in sel:
                if i > 0 and (i - 1) not in chosen:
                    apps[i - 1], apps[i] = apps[i], apps[i - 1]
                    chosen.remove(i)
                    chosen.add(i - 1)
        else:
            for i in reversed(sel):
                if i < len(apps) - 1 and (i + 1) not in chosen:
                    apps[i + 1], apps[i] = apps[i], apps[i + 1]
                    chosen.remove(i)
                    chosen.add(i + 1)
        self._save()
        self._refresh_apps(select=chosen)

    def set_state(self, state):
        preset = self.current_preset()
        sel = self._selected_app_indices()
        if not preset or not sel:
            self.status("先にアプリ一覧で対象を選択してください。")
            return
        for i in sel:
            preset["apps"][i]["window"] = state
        self._save()
        self._refresh_apps(select=sel)

    # ---- 起動 ----
    def launch_selected(self):
        preset = self.current_preset()
        if not preset:
            return
        self.launch_async(preset)

    def launch_async(self, preset):
        if not preset["apps"]:
            self.status("「%s」にはアプリが登録されていません。" % preset["name"])
            return
        self.status("起動中: %s ..." % preset["name"])
        snapshot = {"name": preset["name"], "apps": [dict(a) for a in preset["apps"]]}

        def work():
            self._q.put(("launch", snapshot["name"], launch_preset(snapshot, self.backend)))

        threading.Thread(target=work, daemon=True).start()

    def stop_selected(self):
        preset = self.current_preset()
        if not preset:
            return
        if not preset["apps"]:
            self.status("「%s」にはアプリが登録されていません。" % preset["name"])
            return
        snapshot = {"name": preset["name"], "apps": [dict(a) for a in preset["apps"]]}
        self.status("終了要求を送信中: %s ..." % snapshot["name"])

        def work():
            stop = getattr(self.backend, "stop_apps", None)
            if stop is None:
                result = {"matched": 0, "sent": 0, "unsupported": [],
                          "error": "この環境ではアプリの終了に対応していません。"}
            else:
                try:
                    result = stop(snapshot["apps"])
                except Exception as exc:
                    result = {"matched": 0, "sent": 0, "unsupported": [],
                              "error": "%s: %s" % (type(exc).__name__, exc)}
            self._q.put(("stop", snapshot["name"], result))

        threading.Thread(target=work, daemon=True).start()

    def _on_stopped(self, name, result):
        if result.get("error"):
            self.status("終了要求に失敗しました: %s" % result["error"])
            return
        matched = result.get("matched", 0)
        sent = result.get("sent", 0)
        unsupported = result.get("unsupported", [])
        if not matched:
            message = "「%s」の起動中ウィンドウは見つかりませんでした。" % name
        else:
            message = "「%s」へ終了要求を送信しました (%d/%d ウィンドウ)。" % (name, sent, matched)
        if unsupported:
            message += " 識別できず対象外: %s" % "、".join(unsupported[:3])
            if len(unsupported) > 3:
                message += " ほか%d件" % (len(unsupported) - 3)
        self.status(message)

    def _poll(self):
        try:
            while True:
                operation, name, results = self._q.get_nowait()
                if operation == "stop":
                    self._on_stopped(name, results)
                else:
                    self._on_launched(name, results)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _on_launched(self, name, results):
        failures = [(a, e) for a, e in results if e]
        ok = len(results) - len(failures)
        if not failures:
            self.status("起動しました: %s (%d 件)" % (name, ok))
            return
        self.status("「%s」: %d 件起動、%d 件失敗" % (name, ok, len(failures)))
        detail = "\n".join("・%s\n    %s" % (display_name(a["path"]), e) for a, e in failures)
        messagebox.showwarning(APP_TITLE, "起動できなかったアプリがあります:\n\n%s" % detail,
                               parent=self.root)

    def _run_autostart(self):
        name = self.cfg.get("autostart_preset")
        preset = find_preset(self.cfg, name) if name else None
        if preset:
            self.launch_async(preset)


# --------------------------------------------------------------------------
# エントリポイント
# --------------------------------------------------------------------------
def run_gui(config_path, startup=False, backend=None):
    if tk is None:
        sys.stderr and sys.stderr.write("tkinter が利用できないため GUI を開けません。\n")
        return 1
    if os.name == "nt":
        try:  # 高 DPI 環境でぼやけないようにする
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        sys.stderr and sys.stderr.write("GUI を開始できません: %s\n" % exc)
        return 1
    LauncherGUI(root, config_path, backend=backend, startup=startup)
    root.mainloop()
    return 0


def cmd_list(cfg):
    if not cfg["presets"]:
        safe_print("(プリセットはありません)")
    for p in cfg["presets"]:
        mark = " [PC起動時に自動実行]" if p["name"] == cfg["autostart_preset"] else ""
        safe_print("%s%s" % (p["name"], mark))
        for a in p["apps"]:
            safe_print("    - %s [%s] %s" % (display_name(a["path"]),
                                              STATE_LABELS[a["window"]], a["args"]))
    return 0


def cmd_run(cfg, name, dry_run=False, backend=None):
    preset = find_preset(cfg, name)
    if not preset:
        safe_print("プリセット「%s」が見つかりません。" % name, sys.stderr)
        return 2
    backend = DryRunBackend() if dry_run else (backend or make_backend())
    results = launch_preset(preset, backend)
    backend.wait_pending()
    failed = 0
    for app, err in results:
        label = "%s [%s]" % (display_name(app["path"]), STATE_LABELS[app["window"]])
        if err:
            failed += 1
            safe_print("NG  %s : %s" % (label, err), sys.stderr)
        else:
            safe_print("%s %s" % ("DRY" if dry_run else "OK ", label))
    if not results:
        safe_print("プリセット「%s」にはアプリが登録されていません。" % name)
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("--run", metavar="NAME", help="GUI を開かずにプリセットを起動する")
    parser.add_argument("--list", action="store_true", help="プリセット一覧を表示する")
    parser.add_argument("--startup", action="store_true",
                        help="PC 起動時用: 最小化して開き、自動実行プリセットを起動する")
    parser.add_argument("--dry-run", action="store_true", help="--run と併用: 起動せず内容だけ表示")
    parser.add_argument("--config", metavar="PATH", help="設定ファイルの場所")
    args = parser.parse_args(argv)

    config_path = args.config or default_config_path()
    if args.list or args.run:
        cfg, warning = load_config(config_path)
        if warning:
            safe_print(warning, sys.stderr)
        if args.list:
            return cmd_list(cfg)
        return cmd_run(cfg, args.run, dry_run=args.dry_run)
    return run_gui(config_path, startup=args.startup)


if __name__ == "__main__":
    sys.exit(main())
