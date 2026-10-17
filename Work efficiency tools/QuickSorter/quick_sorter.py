# -*- coding: utf-8 -*-
"""
Quick Sorter - ダウンロード / デスクトップ整理ツール (Windows / Python 3.8+)

  ・ダウンロードとデスクトップ直下のファイルを1件ずつ表示
  ・移動先フォルダ(ダウンロード配下の全フォルダ)をキーボードで検索して Enter で移動
  ・過去の移動履歴から「このファイルはここに入れがち」を学習して上位に表示
  ・グローバルホットキー Ctrl+Alt+M で呼び出し (1プロセスで常駐)

起動:   pyw quick_sorter.py
自動起動の登録:  pyw quick_sorter.py --install-startup   (解除: --uninstall-startup)
"""
import ctypes
import datetime
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import traceback
import unicodedata
from dataclasses import dataclass
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import messagebox, simpledialog, ttk
except ImportError:
    tk = None

APP_DIR = "FileEditor"
DEFAULT_HOTKEY = "Ctrl+Alt+M"
PORT = 47653                     # 二重起動の検出と「表示して」の合図に使う (127.0.0.1 のみ)
SKIP_DIRS = {"$recycle.bin", "system volume information", "node_modules", "__pycache__"}
TEMP_EXT = (".crdownload", ".part", ".tmp", ".download", ".opdownload")
INVALID_CHARS = '<>:"/\\|?*'


# ======================================================================
# 共通ユーティリティ
# ======================================================================
def data_dir():
    base = os.environ.get("APPDATA") or str(Path.home())
    d = Path(base) / APP_DIR
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def log(msg):
    try:
        with open(data_dir() / "log.txt", "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")
    except OSError:
        pass


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, obj):
    try:
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        log(f"save failed: {e}")


def norm(s):
    """全角/半角・大文字小文字の揺れをなくす。"""
    return unicodedata.normalize("NFKC", s).lower()


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024


def downloads_dir():
    """ダウンロードフォルダ(OneDrive等に移動されていても取得できる)。"""
    if os.name == "nt":
        try:
            class GUID(ctypes.Structure):
                _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort),
                            ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]
            guid = GUID(0x374DE290, 0x123F, 0x4565,
                        (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))
            buf = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(buf)) == 0:
                p = buf.value
                ctypes.windll.ole32.CoTaskMemFree(buf)
                if p and os.path.isdir(p):
                    return Path(p)
        except Exception as e:
            log(f"downloads_dir fallback: {e}")
    return Path.home() / "Downloads"


def desktop_dir():
    """デスクトップフォルダ (OneDrive等に移動されていても取得できる)。"""
    if os.name == "nt":
        try:
            class GUID(ctypes.Structure):
                _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort),
                            ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]
            guid = GUID(0xB4BFCC3A, 0xDB2C, 0x424C,
                        (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41))
            buf = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(buf)) == 0:
                p = buf.value
                ctypes.windll.ole32.CoTaskMemFree(buf)
                if p and os.path.isdir(p):
                    return Path(p)
        except Exception as e:
            log(f"desktop_dir fallback: {e}")
    return Path.home() / "Desktop"


def unique_path(dst):
    """既に存在する場合は 'name (1).ext' のように連番を付ける。"""
    dst = Path(dst)
    if not os.path.lexists(dst):
        return dst
    stem, ext = os.path.splitext(dst.name)
    n = 1
    while True:
        cand = dst.with_name(f"{stem} ({n}){ext}")
        if not os.path.lexists(cand):
            return cand
        n += 1


def send_to_trash(path):
    if os.name != "nt":
        raise RuntimeError("ごみ箱への移動は Windows のみ対応です")
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]
    op = SHFILEOPSTRUCTW()
    op.wFunc = 3                                   # FO_DELETE
    op.pFrom = os.path.abspath(str(path)) + "\0\0"
    op.fFlags = 0x40 | 0x10 | 0x4                  # ALLOWUNDO | NOCONFIRMATION | SILENT
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0 or op.fAnyOperationsAborted:
        raise OSError(f"ごみ箱への移動に失敗しました (code {rc})")


# ======================================================================
# コアロジック: 移動元 / 移動先の索引 / 履歴学習
# ======================================================================
@dataclass
class Item:
    path: Path
    size: int
    mtime: float


def list_sources(sources):
    """整理元フォルダ直下のファイル(新しい順)。フォルダは移動先なので含めない。"""
    if isinstance(sources, (str, Path)):
        sources = [sources]
    out = []
    seen = set()
    for src in sources:
        try:
            with os.scandir(src) as it:
                for e in it:
                    try:
                        if not e.is_file():
                            continue
                        n = e.name.lower()
                        if n.endswith(TEMP_EXT) or n == "desktop.ini":
                            continue
                        path = Path(e.path)
                        identity = os.path.normcase(os.path.abspath(path))
                        if identity in seen:
                            continue
                        seen.add(identity)
                        st = e.stat()
                        out.append(Item(path, st.st_size, st.st_mtime))
                    except OSError:
                        pass
        except OSError:
            pass
    out.sort(key=lambda i: -i.mtime)
    return out


@dataclass
class Dest:
    path: Path
    key: str          # 履歴に保存するキー (ルートからの相対パス, "/"区切り)
    rel: str          # 画面表示用 ("仕事 › 2025 › 請求書")
    parts: tuple      # 正規化済みの各階層名
    hay: str          # 正規化済みの全体 ("仕事/2025/請求書")


def scan_folders(root, max_depth=8, limit=30000):
    out, stack = [], [(Path(root), 0)]
    while stack and len(out) < limit:
        cur, depth = stack.pop()
        try:
            it = os.scandir(cur)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    if not e.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if e.name.startswith(".") or e.name.lower() in SKIP_DIRS:
                    continue
                p = Path(e.path)
                out.append(p)
                if depth + 1 < max_depth:
                    stack.append((p, depth + 1))
    return out


def subseq(needle, hay):
    it = iter(hay)
    return all(c in it for c in needle)


def score_text(d, tokens):
    """全トークンが一致すれば得点、どれかが合わなければ None。"""
    last = d.parts[-1]
    total = 0.0
    for t in tokens:
        if t in last:
            s = 100.0
            if last == t:
                s += 50
            elif last.startswith(t):
                s += 25
        elif t in d.hay:
            s = 60.0                      # 親フォルダ名に含まれる
        elif subseq(t, last):
            s = 35.0                      # 飛び飛びの一致 (ske → sakusei_keihi)
        elif subseq(t, d.hay):
            s = 15.0
        else:
            return None
        total += s
    return total - len(d.parts)           # 浅い階層をわずかに優先


def name_tokens(name):
    stem = norm(os.path.splitext(name)[0])
    toks = [t for t in re.split(r"[\W_\d]+", stem) if len(t) >= 2]
    return list(dict.fromkeys(toks))


class History:
    """移動履歴からの学習データ + 取り消し用ログ (JSONで永続化)。"""
    MAX_UNDO = 200
    MAX_RECENT = 40

    def __init__(self, path):
        self.path = Path(path)
        d = load_json(self.path, {})
        self.ext = d.get("ext", {})       # {".pdf": {destkey: count}}
        self.tok = d.get("tok", {})       # {"請求書": {destkey: count}}
        self.dest = d.get("dest", {})     # {destkey: count}
        self.recent = d.get("recent", [])  # 新しい順
        self.undo = d.get("undo", [])     # [[src, dst, destkey], ...]

    def save(self):
        save_json(self.path, {"ext": self.ext, "tok": self.tok, "dest": self.dest,
                              "recent": self.recent, "undo": self.undo})

    def suggest(self, name):
        res = {}
        ext = os.path.splitext(name)[1].lower()
        for k, c in self.ext.get(ext, {}).items():
            res[k] = res.get(k, 0) + min(c, 5) * 0.5
        for t in name_tokens(name):
            for k, c in self.tok.get(t, {}).items():
                res[k] = res.get(k, 0) + min(c, 5) * 3
        return res

    def record(self, name, key, src, dst):
        ext = os.path.splitext(name)[1].lower()
        for bucket, k in [(self.ext, ext)] + [(self.tok, t) for t in name_tokens(name)]:
            if k:
                b = bucket.setdefault(k, {})
                b[key] = b.get(key, 0) + 1
        self.dest[key] = self.dest.get(key, 0) + 1
        if key in self.recent:
            self.recent.remove(key)
        self.recent.insert(0, key)
        del self.recent[self.MAX_RECENT:]
        self.undo.append([str(src), str(dst), key])
        del self.undo[:-self.MAX_UNDO]
        self.save()


class Finder:
    def __init__(self, root, history):
        self.root, self.history = Path(root), history
        self.dests = []

    def rescan(self):
        self.dests = []
        for p in scan_folders(self.root):
            d = self.make_dest(p, add=False)
            if d:
                self.dests.append(d)
        self.dests.sort(key=lambda d: d.hay)

    def make_dest(self, path, add=True):
        try:
            parts = Path(path).relative_to(self.root).parts
        except ValueError:
            return None
        d = Dest(Path(path), "/".join(parts), os.sep.join(parts),   # 表示は「仕事\2025\請求書」形式
                 tuple(norm(x) for x in parts), norm("/".join(parts)))
        if add:
            self.dests.append(d)
        return d

    def search(self, query, filename, limit=300):
        """[(Dest, 履歴タグ)] を関連度順で返す。"""
        hist = self.history.suggest(filename)
        tokens = [t for t in norm(query).split() if t]
        recent_rank = {k: i for i, k in enumerate(self.history.recent)}
        res = []
        if not tokens:
            for d in self.dests:
                res.append((d, (-hist.get(d.key, 0), recent_rank.get(d.key, 10 ** 6),
                                -self.history.dest.get(d.key, 0), d.hay)))
            res.sort(key=lambda x: x[1])
        else:
            for d in self.dests:
                s = score_text(d, tokens)
                if s is None:
                    continue
                s += min(hist.get(d.key, 0), 20) * 3
                if recent_rank.get(d.key, 99) < 5:
                    s += 10
                res.append((d, (-s, d.hay)))
            res.sort(key=lambda x: x[1])
        return [(d, "★ 履歴" if hist.get(d.key, 0) >= 1 else "") for d, _ in res[:limit]]


def move_file(src, dest_dir):
    dest_dir = Path(dest_dir)
    if not dest_dir.is_dir():
        raise FileNotFoundError(f"移動先が見つかりません: {dest_dir}")
    dst = unique_path(dest_dir / Path(src).name)
    shutil.move(str(src), str(dst))
    return dst


def validate_folder_name(name):
    if not name or not name.strip():
        return "名前が空です"
    if any(c in name for c in INVALID_CHARS):
        return "使用できない文字が含まれています"
    if name.endswith((" ", ".")):
        return "末尾に空白やピリオドは使えません"
    return None


# ======================================================================
# 常駐まわり: トレイ + ホットキー / 二重起動 / スタートアップ
# ======================================================================
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x1, 0x2, 0x4, 0x8


def parse_hotkey(text):
    """'Ctrl+Alt+M' -> (mods, vk)。不正なら ValueError。"""
    parts = [p.strip() for p in text.split("+") if p.strip()]
    if len(parts) < 2:
        raise ValueError("修飾キー + キー の形式で指定してください")
    table = {"ctrl": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}
    mods = 0
    for p in parts[:-1]:
        if p.lower() not in table:
            raise ValueError(f"不明な修飾キー: {p}")
        mods |= table[p.lower()]
    k = parts[-1].upper()
    if len(k) == 1 and k.isascii() and (k.isalpha() or k.isdigit()):
        vk = ord(k)
    elif re.fullmatch(r"F([1-9]|1\d|2[0-4])", k):
        vk = 0x70 + int(k[1:]) - 1
    else:
        raise ValueError(f"使えないキー: {parts[-1]}")
    return mods, vk


def format_hotkey(mods, vk):
    names = [n for n, m in (("Ctrl", MOD_CONTROL), ("Alt", MOD_ALT), ("Shift", MOD_SHIFT), ("Win", MOD_WIN)) if mods & m]
    key = chr(vk) if (0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A) else f"F{vk - 0x6F}"
    return "+".join(names + [key])


def hotkey_spec(text):
    mods, vk = parse_hotkey(text)
    return mods, vk, format_hotkey(mods, vk)


def make_icon_bytes():
    """トレイ用の 32x32 アイコン (黄色いフォルダ + 青い矢印) を .ico のバイト列で作る。"""
    import struct
    W = H = 32
    px = [[(0, 0, 0, 0)] * W for _ in range(H)]          # (B, G, R, A)

    def rect(x0, y0, x1, y1, c):
        for y in range(y0, y1):
            for x in range(x0, x1):
                px[y][x] = c
    tab, body, blue = (0x3A, 0xB4, 0xDC, 255), (0x5C, 0xD6, 0xF9, 255), (0xFF, 0xA3, 0x4D, 255)
    rect(2, 6, 14, 10, tab)
    rect(2, 10, 30, 28, body)
    rect(2, 10, 30, 12, tab)
    rect(9, 17, 21, 21, blue)
    for i in range(4):
        rect(21 + i, 15 + i, 22 + i, 23 - i, blue)
    pixels = b"".join(bytes(px[y][x]) for y in reversed(range(H)) for x in range(W))
    dib = struct.pack("<IiiHHIIiiII", 40, W, H * 2, 1, 32, 0, len(pixels) + 128, 0, 0, 0, 0) + pixels + b"\0" * 128
    return struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", W, H, 0, 0, 1, 32, len(dib), 22) + dib


class TrayThread(threading.Thread):
    """Win32 の隠しウィンドウ + タスクトレイのアイコン + ホットキーを1つのスレッドで担当する。
    ホットキーはこの隠しウィンドウに登録し、WM_HOTKEY をウィンドウプロシージャで受ける
    (Tk には直接触らず、結果はキュー経由でUIスレッドへ渡す)。"""
    WM_HOTKEY, WM_CLOSE, WM_DESTROY = 0x0312, 0x0010, 0x0002
    WM_TRAY, WM_CMD = 0x8001, 0x8002             # トレイからの通知 / UIスレッドからの要求
    MOD_NOREPEAT = 0x4000

    def __init__(self, q, spec, hook_mode=False):
        super().__init__(daemon=True)
        self.q = q
        self.cmds = queue.Queue()
        self.cmds.put(("reg", spec, 3))           # 起動時は他プロセスの解放を少し待つ
        if hook_mode:
            self.cmds.put(("hook", True))
        self.hwnd, self.cur, self.nid = None, None, None
        self.hook, self._eat = None, None         # キーボードフック(強力モード)
        self.taskbar_msg = 0

    # ---------------------------------------------------------- UIスレッドから呼ぶ
    def request(self, spec):
        """ホットキー変更の要求。結果は q に ('hotkey_ok'|'hotkey_fail', ...) で届く。"""
        if os.name != "nt":
            return
        self.cmds.put(("reg", spec, 1))
        if self.hwnd:
            self.u.PostMessageW(self.hwnd, self.WM_CMD, 0, 0)

    def set_hook_mode(self, on):
        """強力モード(キーボードフック方式)の切り替え。"""
        if os.name != "nt":
            return
        self.cmds.put(("hook", bool(on)))
        if self.hwnd:
            self.u.PostMessageW(self.hwnd, self.WM_CMD, 0, 0)

    def shutdown(self):
        """トレイアイコンを消してスレッドを止める (終了時)。"""
        if os.name == "nt" and self.hwnd:
            self.u.PostMessageW(self.hwnd, self.WM_CLOSE, 0, 0)
            self.join(1.5)

    # ---------------------------------------------------------- Win32 の準備
    def _setup_api(self):
        from ctypes import wintypes as w
        self.w = w
        u = self.u = ctypes.WinDLL("user32", use_last_error=True)
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        s = self.s = ctypes.WinDLL("shell32", use_last_error=True)
        LRESULT = ctypes.c_ssize_t
        self.WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

        class WNDCLASSW(ctypes.Structure):
            pass
        WNDCLASSW._fields_ = [("style", w.UINT), ("lpfnWndProc", self.WNDPROC), ("cbClsExtra", ctypes.c_int),
                              ("cbWndExtra", ctypes.c_int), ("hInstance", w.HINSTANCE), ("hIcon", w.HICON),
                              ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH), ("lpszMenuName", w.LPCWSTR),
                              ("lpszClassName", w.LPCWSTR)]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT), ("uFlags", w.UINT),
                        ("uCallbackMessage", w.UINT), ("hIcon", w.HICON), ("szTip", w.WCHAR * 128),
                        ("dwState", w.DWORD), ("dwStateMask", w.DWORD), ("szInfo", w.WCHAR * 256),
                        ("uVersion", w.UINT), ("szInfoTitle", w.WCHAR * 64), ("dwInfoFlags", w.DWORD),
                        ("guidItem", ctypes.c_ubyte * 16), ("hBalloonIcon", w.HICON)]
        self.WNDCLASSW, self.NOTIFYICONDATAW = WNDCLASSW, NOTIFYICONDATAW

        def sig(fn, args, res):
            fn.argtypes, fn.restype = args, res
        sig(u.DefWindowProcW, [w.HWND, w.UINT, w.WPARAM, w.LPARAM], LRESULT)
        sig(u.RegisterClassW, [ctypes.POINTER(WNDCLASSW)], w.ATOM)
        sig(u.CreateWindowExW, [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID], w.HWND)
        sig(u.DestroyWindow, [w.HWND], w.BOOL)
        sig(u.PostQuitMessage, [ctypes.c_int], None)
        sig(u.PostMessageW, [w.HWND, w.UINT, w.WPARAM, w.LPARAM], w.BOOL)
        sig(u.RegisterHotKey, [w.HWND, ctypes.c_int, w.UINT, w.UINT], w.BOOL)
        sig(u.UnregisterHotKey, [w.HWND, ctypes.c_int], w.BOOL)
        sig(u.GetMessageW, [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT], ctypes.c_int)
        sig(u.TranslateMessage, [ctypes.POINTER(w.MSG)], w.BOOL)
        sig(u.DispatchMessageW, [ctypes.POINTER(w.MSG)], LRESULT)
        sig(u.RegisterWindowMessageW, [w.LPCWSTR], w.UINT)
        sig(u.LoadImageW, [w.HINSTANCE, w.LPCWSTR, w.UINT, ctypes.c_int, ctypes.c_int, w.UINT], w.HANDLE)
        sig(u.LoadIconW, [w.HINSTANCE, ctypes.c_void_p], w.HICON)
        sig(u.CreatePopupMenu, [], w.HMENU)
        sig(u.AppendMenuW, [w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR], w.BOOL)
        sig(u.TrackPopupMenu, [w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, ctypes.c_void_p], ctypes.c_int)
        sig(u.DestroyMenu, [w.HMENU], w.BOOL)
        sig(u.GetCursorPos, [ctypes.POINTER(w.POINT)], w.BOOL)
        sig(u.SetForegroundWindow, [w.HWND], w.BOOL)
        sig(self.k.GetModuleHandleW, [w.LPCWSTR], w.HMODULE)
        sig(s.Shell_NotifyIconW, [w.DWORD, ctypes.POINTER(NOTIFYICONDATAW)], w.BOOL)

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("vkCode", w.DWORD), ("scanCode", w.DWORD), ("flags", w.DWORD),
                        ("time", w.DWORD), ("dwExtraInfo", ctypes.c_size_t)]
        self.KBDLL = KBDLLHOOKSTRUCT
        self.HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, w.WPARAM, w.LPARAM)
        sig(u.SetWindowsHookExW, [ctypes.c_int, self.HOOKPROC, w.HINSTANCE, w.DWORD], w.HHOOK)
        sig(u.UnhookWindowsHookEx, [w.HHOOK], w.BOOL)
        sig(u.CallNextHookEx, [w.HHOOK, ctypes.c_int, w.WPARAM, w.LPARAM], LRESULT)
        sig(u.GetAsyncKeyState, [ctypes.c_int], ctypes.c_short)

    def _create_window(self):
        self._wndproc = self.WNDPROC(self._proc)          # GCされないよう保持する
        hinst = self.k.GetModuleHandleW(None)
        wc = self._wc = self.WNDCLASSW()
        wc.lpfnWndProc, wc.hInstance = self._wndproc, hinst
        wc.lpszClassName = "FileEditorQuickSorterTray"
        if not self.u.RegisterClassW(ctypes.byref(wc)):
            err = ctypes.get_last_error()
            if err != 1410:                               # ERROR_CLASS_ALREADY_EXISTS 以外は失敗
                raise ctypes.WinError(err)
        # 表示しない通常のトップレベルウィンドウ (メッセージ専用ウィンドウだと TaskbarCreated が届かない)
        self.hwnd = self.u.CreateWindowExW(0, wc.lpszClassName, "File Editor Quick Sorter", 0,
                                           0, 0, 0, 0, None, None, hinst, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self.taskbar_msg = self.u.RegisterWindowMessageW("TaskbarCreated")
        log(f"tray window created (hwnd={self.hwnd})")

    # ---------------------------------------------------------- トレイアイコン
    def _load_icon(self):
        try:
            p = data_dir() / "icon.ico"
            p.write_bytes(make_icon_bytes())
            h = self.u.LoadImageW(None, str(p), 1, 0, 0, 0x10 | 0x40)    # IMAGE_ICON, LR_LOADFROMFILE|LR_DEFAULTSIZE
            if h:
                return h
        except Exception:
            log("icon load failed:\n" + traceback.format_exc())
        return self.u.LoadIconW(None, 32512)                              # 標準アイコンで代用

    def _tip(self):
        key = self.cur[2] if self.cur else "ホットキー未登録"
        return f"File Editor 仕分け ({key})"[:120]

    def _add_icon(self):
        nid = self.nid = self.NOTIFYICONDATAW()
        nid.cbSize, nid.hWnd, nid.uID = ctypes.sizeof(nid), self.hwnd, 1
        nid.uFlags = 0x1 | 0x2 | 0x4                      # NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = self.WM_TRAY
        nid.hIcon = self._load_icon()
        nid.szTip = self._tip()
        ok = self.s.Shell_NotifyIconW(0, ctypes.byref(nid))                # NIM_ADD
        log("tray icon added" if ok else f"tray icon add FAILED (error {ctypes.get_last_error()})")

    def _update_tip(self):
        if self.nid is not None:
            self.nid.szTip = self._tip()
            self.s.Shell_NotifyIconW(1, ctypes.byref(self.nid))            # NIM_MODIFY

    def _del_icon(self):
        if self.nid is not None:
            self.s.Shell_NotifyIconW(2, ctypes.byref(self.nid))            # NIM_DELETE
            self.nid = None

    def _menu(self):
        u = self.u
        pt = self.w.POINT()
        u.GetCursorPos(ctypes.byref(pt))
        m = u.CreatePopupMenu()
        u.AppendMenuW(m, 0, 1, f"開く ({self.cur[2] if self.cur else '未登録'})")
        u.AppendMenuW(m, 0, 2, "ホットキーを変更...")
        u.AppendMenuW(m, 0x8 if startup_enabled() else 0, 3, "Windows 起動時に自動起動")    # MF_CHECKED
        u.AppendMenuW(m, 0x800, 0, None)                                                  # MF_SEPARATOR
        u.AppendMenuW(m, 0, 4, "終了")
        u.SetForegroundWindow(self.hwnd)                  # これがないとメニューが閉じない
        cmd = u.TrackPopupMenu(m, 0x100 | 0x2, pt.x, pt.y, 0, self.hwnd, None)    # TPM_RETURNCMD|TPM_RIGHTBUTTON
        u.PostMessageW(self.hwnd, 0, 0, 0)
        u.DestroyMenu(m)
        if cmd == 1:
            self.q.put(("show",))
        elif cmd == 2:
            self.q.put(("hotkey_dialog",))
        elif cmd == 3:
            try:
                set_startup(not startup_enabled())
            except Exception as e:
                log(f"startup toggle failed: {e}")
            self.q.put(("startup_changed",))
        elif cmd == 4:
            self.q.put(("quit",))

    # ---------------------------------------------------------- ホットキー登録
    def _register(self, spec, tries):
        mods, vk, _label = spec
        err = 0
        for i in range(tries):
            if self.u.RegisterHotKey(self.hwnd, 1, mods | self.MOD_NOREPEAT, vk):
                return 0
            err = ctypes.get_last_error() or 1
            if err != 1409 or i == tries - 1:             # 1409 = 他のプログラムが使用中
                break
            threading.Event().wait(1.0)
        return err

    def _apply(self, spec, tries):
        prev = self.cur
        if prev:
            self.u.UnregisterHotKey(self.hwnd, 1)
            self.cur = None
        err = self._register(spec, tries)
        if err == 0:
            self.cur = spec
            log(f"hotkey registered: {spec[2]}")
            self.q.put(("hotkey_ok", spec[2]))
        else:
            log(f"hotkey failed: {spec[2]} (error {err})")
            if prev and self._register(prev, 1) == 0:      # 使えなかったら元のキーに戻す
                self.cur = prev
            self.q.put(("hotkey_fail", spec[2], err, self.cur[2] if self.cur else None))
        self._update_tip()

    def _drain(self):
        while True:
            try:
                cmd = self.cmds.get_nowait()
            except queue.Empty:
                return
            if cmd[0] == "reg":
                self._apply(cmd[1], cmd[2])
            elif cmd[0] == "hook":
                self._set_hook(cmd[1])

    # ---------------------------------------------------------- 強力モード (キーボードフック)
    def _mods_down(self):
        g = self.u.GetAsyncKeyState
        m = 0
        if g(0x11) & 0x8000:
            m |= MOD_CONTROL
        if g(0x12) & 0x8000:
            m |= MOD_ALT
        if g(0x10) & 0x8000:
            m |= MOD_SHIFT
        if (g(0x5B) | g(0x5C)) & 0x8000:
            m |= MOD_WIN
        return m

    def _ll_proc(self, code, wp, lp):
        """低レベルキーボードフック: ホットキーのキーを直接監視する。
        登録方式より手前で動くため、他のソフトが先に処理していても拾いやすい。"""
        try:
            if code == 0 and self.cur:
                info = ctypes.cast(lp, ctypes.POINTER(self.KBDLL)).contents
                mods, vk, _label = self.cur
                if info.vkCode == vk and not (info.flags & 0x10):          # 0x10 = 他プログラムが送った入力は無視
                    if wp in (0x100, 0x104):                                # KEYDOWN / SYSKEYDOWN
                        if self._eat == vk:                                 # 押しっぱなしの繰り返し
                            return 1
                        if self._mods_down() == mods:
                            self._eat = vk
                            log("hook: hotkey chord detected -> show")
                            self.q.put(("hotkey_fired",))
                            return 1                                        # アプリには渡さない
                    elif wp in (0x101, 0x105) and self._eat == vk:          # KEYUP / SYSKEYUP
                        self._eat = None
                        return 1
        except Exception:
            log("hook proc error:\n" + traceback.format_exc())
        return self.u.CallNextHookEx(None, code, wp, lp)

    def _set_hook(self, on):
        if on and not self.hook:
            self._hookproc = self.HOOKPROC(self._ll_proc)                   # GCされないよう保持する
            self.hook = self.u.SetWindowsHookExW(13, self._hookproc, self.k.GetModuleHandleW(None), 0)   # WH_KEYBOARD_LL
            if self.hook:
                log("keyboard hook ON (強力モード)")
            else:
                log(f"keyboard hook FAILED (error {ctypes.get_last_error()})")
                self.q.put(("hotkey_info", "✕ 強力モードを有効にできませんでした (log.txt 参照)"))
        elif not on and self.hook:
            self.u.UnhookWindowsHookEx(self.hook)
            self.hook, self._eat = None, None
            log("keyboard hook OFF")

    # ---------------------------------------------------------- メッセージ処理
    def _proc(self, hwnd, msg, wp, lp):
        try:
            if msg == self.WM_HOTKEY:
                log("hotkey pressed -> show")
                self.q.put(("hotkey_fired",))
                return 0
            if msg == self.WM_TRAY:
                ev = lp & 0xFFFF
                if ev in (0x0202, 0x0203):                 # 左クリック / ダブルクリック
                    self.q.put(("show",))
                elif ev == 0x0205:                         # 右クリック
                    self._menu()
                return 0
            if msg == self.WM_CMD:
                self._drain()
                return 0
            if self.taskbar_msg and msg == self.taskbar_msg:   # エクスプローラー再起動でアイコンが消えた時
                self._add_icon()
                return 0
            if msg == self.WM_CLOSE:
                self.u.DestroyWindow(hwnd)
                return 0
            if msg == self.WM_DESTROY:
                self._set_hook(False)
                self._del_icon()
                self.u.PostQuitMessage(0)
                return 0
        except Exception:
            log("wndproc error:\n" + traceback.format_exc())
        return self.u.DefWindowProcW(hwnd, msg, wp, lp)

    def run(self):
        if os.name != "nt":
            self.q.put(("hotkey_info", "Windows以外では無効"))
            return
        try:
            self._setup_api()
            self._create_window()
            self._add_icon()
            self._drain()
            msg = self.w.MSG()
            while self.u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                self.u.TranslateMessage(ctypes.byref(msg))
                self.u.DispatchMessageW(ctypes.byref(msg))
            log("tray thread finished")
        except Exception:
            log("tray thread crashed:\n" + traceback.format_exc())
            self.q.put(("hotkey_info", "✕ トレイ/ホットキーが異常終了しました (log.txt 参照)"))


def start_single_instance(q, background):
    """最初の1つなら待ち受けソケットを返す。既に起動中なら合図を送って None。"""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", PORT))
        s.listen(5)
    except OSError:
        s.close()
        try:
            c = socket.create_connection(("127.0.0.1", PORT), timeout=2)
            c.sendall(b"bg" if background else b"show")
            c.close()
        except OSError:
            pass
        return None

    def loop():
        while True:
            try:
                conn, _ = s.accept()
                conn.settimeout(1)
                data = conn.recv(8)
                conn.close()
                if data == b"show":
                    q.put(("show",))
            except OSError:
                continue
    threading.Thread(target=loop, daemon=True).start()
    return s


def startup_script_path():
    base = os.environ.get("APPDATA")
    if not base:
        return None
    return Path(base) / "Microsoft/Windows/Start Menu/Programs/Startup/FileEditorQuickSorter.vbs"


def startup_enabled():
    p = startup_script_path()
    return bool(p and p.exists())


def pythonw_path():
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        w = exe.with_name("pythonw.exe")
        if w.exists():
            return str(w)
    return str(exe)


def set_startup(enable):
    p = startup_script_path()
    if p is None:
        raise RuntimeError("スタートアップフォルダが見つかりません (Windows専用)")
    if not enable:
        if p.exists():
            p.unlink()
        return
    script = os.path.abspath(__file__)
    text = ('CreateObject("WScript.Shell").Run """%s"" ""%s"" --background", 0, False\r\n'
            % (pythonw_path(), script))
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_bytes(text.encode("mbcs"))
    except (LookupError, UnicodeEncodeError):
        p.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
    # 旧版のホットキー常駐は同じキーを奪い合うので、自動起動の登録は外す
    old = p.parent / "File Editor Hotkey.lnk"
    if old.exists():
        old.unlink()


# ======================================================================
# GUI
# ======================================================================
if tk is not None:

    class App(tk.Tk):
        def __init__(self, source, q, background=False, hotkey=None, hotkey_label=DEFAULT_HOTKEY, hook_mode=False):
            super().__init__()
            self.source, self.q = Path(source), q  # 移動先の基準は従来どおりダウンロード
            self.sources = list(dict.fromkeys((self.source, desktop_dir())))
            self.history = History(data_dir() / "history.json")
            self.finder = Finder(self.source, self.history)
            self.files, self.cands, self.cur = [], [], None
            self._dest_by_iid = {}
            self._expanded_dest_keys = set()
            self._dest_tree_initialized = False
            self.hk = hotkey                      # TrayThread
            self.hook_mode = hook_mode            # 強力モード (キーボードフック方式)
            self.hotkey_label = hotkey_label      # 現在有効 (または登録を試みた) ホットキー
            self.hotkey_state = "起動中…"
            self._hk_win = None
            self.icon_folder = self._make_folder_icon()
            self._flash_job = None
            self._flash_on = False
            self.title("File Editor - 仕分け")
            self.geometry("1040x640")
            self.minsize(760, 440)
            self._setup_theme()
            self._build_ui()
            self._setup_keys()
            self.protocol("WM_DELETE_WINDOW", self.hide)
            self.report_callback_exception = self._cb_exc
            self.reload()
            if background:
                self.withdraw()
            self.after(100, self._poll)

        # ---------------------------------------------------------- 見た目
        def _setup_theme(self):
            import tkinter.font as tkfont
            for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
                try:
                    tkfont.nametofont(name).configure(family="Yu Gothic UI", size=10)
                except tk.TclError:
                    pass
            st = ttk.Style(self)
            st.theme_use("clam")
            bg, panel, field = "#1e1e1e", "#252526", "#333337"
            fg, muted, accent = "#e6e6e6", "#b8b8b8", "#4da3ff"
            self.c = {"bg": bg, "panel": panel, "fg": fg, "muted": muted, "accent": accent}
            self.configure(background=bg)
            st.configure(".", background=bg, foreground=fg, fieldbackground=field)
            st.configure("TFrame", background=bg)
            st.configure("TLabel", background=bg, foreground=fg)
            st.configure("TButton", background=field, foreground=fg, bordercolor="#555555", padding=(8, 3))
            st.map("TButton", background=[("active", "#45454a")])
            st.configure("TEntry", fieldbackground=field, foreground=fg, insertcolor=fg, bordercolor=accent, padding=6)
            st.configure("Treeview", rowheight=26, background=panel, fieldbackground=panel, foreground=fg, borderwidth=0)
            st.map("Treeview", background=[("selected", "#264f78")], foreground=[("selected", "#ffffff")])
            st.configure("Treeview.Heading", background=field, foreground=fg)
            st.configure("TScrollbar", background=field, troughcolor=bg, bordercolor=bg, arrowcolor=fg)
            st.configure("TPanedwindow", background=bg)

        def _build_ui(self):
            c = self.c
            top = ttk.Frame(self, padding=(10, 8, 10, 4))
            top.pack(fill="x")
            ttk.Label(top, text="整理元: ダウンロード + デスクトップ (各フォルダ直下)", foreground=c["muted"]).pack(side="left")
            ttk.Button(top, text="終了", command=self.quit_app).pack(side="right")
            self.btn_startup = ttk.Button(top, command=self.toggle_startup)
            self.btn_startup.pack(side="right", padx=6)
            ttk.Button(top, text="ホットキー変更", command=self.open_hotkey_dialog).pack(side="right")
            ttk.Button(top, text="更新 (F5)", command=self.reload).pack(side="right")
            self._update_startup_btn()

            pw = ttk.PanedWindow(self, orient="horizontal")
            pw.pack(fill="both", expand=True, padx=10, pady=4)
            left, right = ttk.Frame(pw), ttk.Frame(pw)
            pw.add(left, weight=1)
            pw.add(right, weight=3)

            self.ftree = ttk.Treeview(left, columns=("size",), show="tree headings", selectmode="browse")
            self.ftree.heading("#0", text="未整理のファイル (新しい順)")
            self.ftree.heading("size", text="サイズ")
            self.ftree.column("#0", width=230)
            self.ftree.column("size", width=70, anchor="e")
            fsb = ttk.Scrollbar(left, command=self.ftree.yview)
            self.ftree.configure(yscrollcommand=fsb.set)
            self.ftree.pack(side="left", fill="both", expand=True)
            fsb.pack(side="right", fill="y")

            self.lbl_name = ttk.Label(right, text="", font=("Yu Gothic UI", 14, "bold"), wraplength=620)
            self.lbl_name.pack(anchor="w", padx=8, pady=(2, 0))
            self.lbl_info = ttk.Label(right, text="", foreground=c["muted"])
            self.lbl_info.pack(anchor="w", padx=8, pady=(0, 6))
            self.qvar = tk.StringVar()
            self.entry = ttk.Entry(right, textvariable=self.qvar, font=("Yu Gothic UI", 13))
            self.entry.pack(fill="x", padx=8)
            self.qvar.trace_add("write", lambda *_: self.refresh_cands())
            ttk.Label(right, text="移動先を入力して絞り込み / フォルダをクリックで展開   ↑↓ 選択   Enter 移動",
                      foreground=c["muted"]).pack(anchor="w", padx=8, pady=(2, 4))

            box = ttk.Frame(right)
            box.pack(fill="both", expand=True, padx=8)
            self.ctree = ttk.Treeview(box, columns=("tag",), show="tree headings", selectmode="browse")
            self.ctree.heading("#0", text="移動先フォルダ", anchor="w")
            self.ctree.heading("tag", text="履歴", anchor="center")
            self.ctree.column("#0", width=430)
            self.ctree.column("tag", width=70, anchor="center", stretch=False)
            csb = ttk.Scrollbar(box, command=self.ctree.yview)
            self.ctree.configure(yscrollcommand=csb.set)
            self.ctree.pack(side="left", fill="both", expand=True)
            csb.pack(side="right", fill="y")
            self.ctree.bind("<<TreeviewOpen>>", self._remember_dest_open)
            self.ctree.bind("<<TreeviewClose>>", self._remember_dest_close)

            self.status = ttk.Label(self, text="", anchor="w", padding=(12, 6))
            self.status.pack(fill="x")
            ttk.Label(self, foreground=c["muted"], anchor="w", padding=(12, 0, 12, 8),
                      text="Enter 移動 / Ctrl+Enter 新規フォルダを作って移動 / Ctrl+↑↓ ファイル切替 / Ctrl+Z 取消 / "
                           "Ctrl+O 開く / Ctrl+E 場所を表示 / Ctrl+D ごみ箱 / Esc 隠す").pack(fill="x")

        def _setup_keys(self):
            e = self.entry
            e.bind("<Up>", lambda _e: self.step(self.ctree, -1))
            e.bind("<Down>", lambda _e: self.step(self.ctree, 1))
            e.bind("<Prior>", lambda _e: self.step(self.ctree, -8))
            e.bind("<Next>", lambda _e: self.step(self.ctree, 8))
            e.bind("<Control-Up>", lambda _e: self.step(self.ftree, -1))
            e.bind("<Control-Down>", lambda _e: self.step(self.ftree, 1))
            e.bind("<Return>", lambda _e: self.commit())
            e.bind("<Control-Return>", lambda _e: self.commit_new_folder())
            e.bind("<Escape>", self.on_escape)
            for key, fn in (("<Control-z>", self.undo), ("<Control-o>", self.open_file),
                            ("<Control-e>", self.show_in_explorer), ("<Control-d>", self.trash_file),
                            ("<Control-q>", self.quit_app), ("<F5>", self.reload)):
                self.bind(key, lambda _e, fn=fn: (fn(), "break")[1])
            self.ftree.bind("<<TreeviewSelect>>", self.on_file_select)
            self.ctree.bind("<Double-1>", lambda _e: self.commit())
            for t in (self.ftree, self.ctree):
                t.bind("<ButtonRelease-1>", lambda _e: self.after_idle(self.entry.focus_set))

        # ---------------------------------------------------------- 表示/非表示
        def show(self):
            log("show requested")
            try:
                self.reload()                      # 失敗してもウィンドウは必ず出す
                self.qvar.set("")
                self._update_startup_btn()
            except Exception:
                log("reload failed in show:\n" + traceback.format_exc())
            self.deiconify()
            self.lift()
            try:
                self.attributes("-topmost", True)
                self.after(300, lambda: self.attributes("-topmost", False))
                if os.name == "nt":
                    ctypes.windll.user32.SetForegroundWindow(ctypes.windll.user32.GetParent(self.winfo_id()) or self.winfo_id())
            except Exception:
                pass
            self.focus_force()
            self.entry.focus_set()

        def hide(self):
            self.withdraw()

        def on_escape(self, _e=None):
            if self.qvar.get():
                self.qvar.set("")
            else:
                self.hide()
            return "break"

        def quit_app(self):
            if self.hk is not None:
                self.hk.shutdown()                 # トレイのアイコンを消してから終了
            self.destroy()

        def _handle(self, m):
            kind = m[0]
            if kind == "show":
                self.show()
            elif kind == "hotkey_fired":
                self.on_hotkey_fired()
            elif kind == "hotkey_dialog":
                self.show()
                self.open_hotkey_dialog()
            elif kind == "startup_changed":
                self._update_startup_btn()
            elif kind == "quit":
                self.quit_app()
            elif kind == "hotkey_ok":
                self.on_hotkey_ok(m[1])
            elif kind == "hotkey_fail":
                self.on_hotkey_fail(m[1], m[2], m[3])
            elif kind == "hotkey_info":
                self.hotkey_state = m[1]
                self.update_status()

        def _poll(self):
            while True:
                try:
                    m = self.q.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle(m)
                except Exception:                  # 1件の失敗で受信ループを止めない
                    log(f"handling {m[0]} failed:\n" + traceback.format_exc())
            try:
                self.after(100, self._poll)
            except tk.TclError:
                pass                               # 終了処理中

        def _cb_exc(self, exc, val, tb):
            log("UI error:\n" + "".join(traceback.format_exception(exc, val, tb)))
            self.flash(f"エラー: {val}", True)

        # ---------------------------------------------------------- 一覧
        def reload(self):
            keep = self.cur.path if self.cur else None
            self.files = list_sources(self.sources)
            self.finder.rescan()
            self.fill_files(keep=keep)
            self.update_status()

        def fill_files(self, keep=None, index=0):
            self.ftree.delete(*self.ftree.get_children())
            for i, it in enumerate(self.files):
                self.ftree.insert("", "end", iid=str(i), text=it.path.name, values=(fmt_size(it.size),))
            if not self.files:
                self.cur = None
                self.on_file_select()
                return
            idx = min(index, len(self.files) - 1)
            if keep is not None:
                for i, it in enumerate(self.files):
                    if it.path == keep:
                        idx = i
                        break
            self.ftree.selection_set(str(idx))
            self.ftree.see(str(idx))
            self.on_file_select()

        def on_file_select(self, _e=None):
            sel = self.ftree.selection()
            self.cur = self.files[int(sel[0])] if sel else None
            if self.cur:
                p = self.cur.path
                ext = p.suffix.lstrip(".").upper() or "ファイル"
                when = datetime.datetime.fromtimestamp(self.cur.mtime).strftime("%Y/%m/%d %H:%M")
                self.lbl_name.config(text=p.name)
                self.lbl_info.config(text=f"{ext} · {fmt_size(self.cur.size)} · {when}")
            else:
                self.lbl_name.config(text="整理するファイルはありません 🎉")
                self.lbl_info.config(text="ダウンロード / デスクトップ直下のファイルがなくなりました")
            self.refresh_cands()

        def refresh_cands(self):
            # 再描画前に展開中の項目を記録する。移動後も同じ階層状態を戻す。
            for iid in self._dest_by_iid:
                if self.ctree.item(iid, "open"):
                    self._expanded_dest_keys.add(iid)
                else:
                    self._expanded_dest_keys.discard(iid)
            self.ctree.delete(*self.ctree.get_children())
            self._dest_by_iid = {}
            name = self.cur.path.name if self.cur else ""
            query = self.qvar.get()
            searching = bool(query.strip())
            if not self.cur:
                self.cands = []
                return
            # 未検索時は全フォルダをツリー表示し、検索時は順位上位300件に絞る。
            self.cands = (self.finder.search(query, name) if searching else
                          self.finder.search("", name, limit=len(self.finder.dests)))
            candidate_by_key = {d.key: (d, tag) for d, tag in self.cands}
            included = set(candidate_by_key)
            for d, _tag in self.cands:
                parent = d.key.rpartition("/")[0]
                while parent:
                    included.add(parent)
                    parent = parent.rpartition("/")[0]
            # 親から順に挿入して、相対パスをフォルダ階層として表示する。
            by_key = {d.key: d for d in self.finder.dests}
            for key in sorted(included, key=lambda value: (value.count("/"), value.casefold())):
                d = by_key.get(key)
                if d is None:
                    continue
                parent = key.rpartition("/")[0]
                parent_iid = parent if parent in included else ""
                tag = candidate_by_key.get(key, (None, ""))[1]
                self.ctree.insert(parent_iid, "end", iid=key, text=d.path.name,
                                  image=self.icon_folder, values=(tag,))
                self._dest_by_iid[key] = d
            # 初回は最上位だけ展開。以降はユーザーが開閉した状態を復元する。
            if not self._dest_tree_initialized:
                self._expanded_dest_keys = {key for key in included if "/" not in key}
                self._dest_tree_initialized = True
            for key in self._expanded_dest_keys:
                if key in included:
                    self.ctree.item(key, open=True)
            # 検索中は候補までの経路も開いて見つけやすくする。
            if searching:
                for key in included:
                    parent = key.rpartition("/")[0]
                    while parent:
                        if parent in included:
                            self.ctree.item(parent, open=True)
                            self._expanded_dest_keys.add(parent)
                        parent = parent.rpartition("/")[0]
            if self.cands:
                first = self.cands[0][0].key
                if not searching and first.count("/") > 0:
                    parts = first.split("/")
                    first = "/".join(parts[:2])
                self.ctree.selection_set(first)
                if searching:
                    self.ctree.see(first)

        def _remember_dest_open(self, event=None):
            tree = event.widget if event else self.ctree
            iid = tree.focus()
            if iid in self._dest_by_iid:
                self._expanded_dest_keys.add(iid)

        def _remember_dest_close(self, event=None):
            tree = event.widget if event else self.ctree
            iid = tree.focus()
            if iid in self._dest_by_iid:
                self._expanded_dest_keys.discard(iid)

        def step(self, tree, d):
            def visible(parent=""):
                rows = []
                for item in tree.get_children(parent):
                    rows.append(item)
                    if tree.item(item, "open"):
                        rows.extend(visible(item))
                return rows

            rows = visible()
            if not rows:
                return "break"
            sel = tree.selection()
            i = rows.index(sel[0]) + d if sel and sel[0] in rows else 0
            i = max(0, min(len(rows) - 1, i))
            tree.selection_set(rows[i])
            tree.see(rows[i])
            return "break"

        def selected_dest(self):
            sel = self.ctree.selection()
            return self._dest_by_iid.get(sel[0]) if sel else None

        # ---------------------------------------------------------- 操作
        def commit(self):
            if not self.cur:
                return "break"
            d = self.selected_dest()
            if d is None:
                self.flash("一致する移動先がありません。Ctrl+Enter で新しいフォルダを作れます", True)
                return "break"
            self.do_move(d)
            return "break"

        def commit_new_folder(self):
            if not self.cur:
                return "break"
            parent = self.selected_dest()
            parent_path = parent.path if parent else self.source
            label = parent.rel if parent else "(ダウンロード直下)"
            name = simpledialog.askstring("新規フォルダ", f"作成場所: {label}\nフォルダ名:",
                                          initialvalue=self.qvar.get().strip(), parent=self)
            if not name:
                self.entry.focus_set()
                return "break"
            name = name.strip()
            err = validate_folder_name(name)
            if err:
                messagebox.showerror("名前が不正です", err, parent=self)
                return "break"
            try:
                path = parent_path / name
                path.mkdir(exist_ok=True)
            except OSError as e:
                messagebox.showerror("作成できません", str(e), parent=self)
                return "break"
            d = self.finder.make_dest(path)
            if d:
                self.do_move(d)
            return "break"

        def do_move(self, d):
            src = self.cur.path
            idx = self.files.index(self.cur)
            try:
                dst = move_file(src, d.path)
            except Exception as e:
                log(f"move failed: {src} -> {d.path}: {e}")
                messagebox.showerror("移動に失敗しました", str(e), parent=self)
                return
            self.history.record(src.name, d.key, src, dst)
            del self.files[idx]
            self.qvar.set("")
            self.fill_files(index=idx)
            self.flash(f"「{src.name}」→ {d.rel}   (Ctrl+Z で取り消し)")
            self.entry.focus_set()

        def undo(self):
            if not self.history.undo:
                return self.flash("取り消せる操作がありません")
            src, dst, _key = self.history.undo[-1]
            src, dst = Path(src), Path(dst)
            if not dst.exists():
                self.history.undo.pop()
                self.history.save()
                return self.flash(f"「{dst.name}」が見つからないため取り消せません", True)
            try:
                back = unique_path(src)
                src.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dst), str(back))
            except Exception as e:
                return messagebox.showerror("取り消せません", str(e), parent=self)
            self.history.undo.pop()
            self.history.save()
            self.files = list_sources(self.sources)
            self.fill_files(keep=back)
            self.flash(f"「{back.name}」を元の場所に戻しました")

        def open_file(self):
            if self.cur and os.name == "nt":
                try:
                    os.startfile(str(self.cur.path))
                except OSError as e:
                    self.flash(f"開けません: {e}", True)

        def show_in_explorer(self):
            if self.cur and os.name == "nt":
                subprocess.Popen(["explorer", "/select,", str(self.cur.path)])

        def trash_file(self):
            if not self.cur:
                return
            if not messagebox.askyesno("削除の確認", f"「{self.cur.path.name}」をごみ箱に移動しますか?", parent=self):
                self.entry.focus_set()
                return
            idx = self.files.index(self.cur)
            try:
                send_to_trash(self.cur.path)
            except Exception as e:
                return messagebox.showerror("削除に失敗しました", str(e), parent=self)
            name = self.cur.path.name
            del self.files[idx]
            self.fill_files(index=idx)
            self.flash(f"「{name}」をごみ箱へ移動しました")
            self.entry.focus_set()

        # ---------------------------------------------------------- ホットキー変更
        def _make_folder_icon(self):
            f = tk.PhotoImage(width=16, height=16)
            f.put("#dcb43a", to=(1, 3, 7, 5))
            f.put("#f9d65c", to=(1, 5, 15, 14))
            f.put("#dcb43a", to=(1, 5, 15, 6))
            return f

        @staticmethod
        def _pressed_mods(e):
            if os.name == "nt":
                gs = ctypes.windll.user32.GetAsyncKeyState
                m = 0
                if gs(0x11) & 0x8000:
                    m |= MOD_CONTROL
                if gs(0x12) & 0x8000:
                    m |= MOD_ALT
                if gs(0x10) & 0x8000:
                    m |= MOD_SHIFT
                if gs(0x5B) & 0x8000 or gs(0x5C) & 0x8000:
                    m |= MOD_WIN
                return m
            return (MOD_CONTROL if e.state & 4 else 0) | (MOD_ALT if e.state & 8 else 0) | (MOD_SHIFT if e.state & 1 else 0)

        def open_hotkey_dialog(self):
            if self._hk_win is not None and self._hk_win.winfo_exists():
                self._hk_win.lift()
                return
            c = self.c
            w = tk.Toplevel(self)
            self._hk_win = w
            w.title("ホットキーの変更")
            w.configure(background=c["bg"])
            w.geometry("600x430")
            w.transient(self)
            ttk.Label(w, text="現在のホットキー", foreground=c["muted"]).pack(pady=(18, 0))
            self._hk_cur = ttk.Label(w, text=self.hotkey_label, font=("Yu Gothic UI", 20, "bold"))
            self._hk_cur.pack(pady=(0, 10))
            ttk.Label(w, justify="center", wraplength=520, foreground=c["muted"],
                      text="この画面を開いたまま、割り当てたいキーの組み合わせを押してください。\n"
                           "押した瞬間に登録を試し、使えるかどうかをここに表示します。\n"
                           "(Ctrl / Alt / Shift / Win のどれか + 英数字キー or F1〜F24)").pack()
            self._hk_msg = ttk.Label(w, text="キーを押してください", wraplength=540, justify="center")
            self._hk_msg.pack(pady=12)
            self._hk_hook_var = tk.BooleanVar(value=self.hook_mode)
            ttk.Checkbutton(w, text="強力モード (キーボードフック方式)", variable=self._hk_hook_var,
                            command=self.on_hook_toggle).pack()
            ttk.Label(w, justify="center", wraplength=540, foreground=c["muted"],
                      text="登録できたのに押しても反応しないときにオンにしてください。キー入力を直接監視するので、"
                           "他のソフトが先にキーを処理していても届きやすくなります。").pack(pady=(2, 12))
            ttk.Button(w, text="閉じる", command=w.destroy).pack()
            w.bind("<KeyPress>", self._hk_key)
            w.bind("<Escape>", lambda _e: w.destroy())
            w.focus_force()

        def _hk_say(self, text, err=False):
            if self._hk_win is not None and self._hk_win.winfo_exists():
                self._hk_msg.config(text=text, foreground="#ff6b6b" if err else "#6fdc8c")

        def _hk_key(self, e):
            if e.keysym in ("Escape",):
                return None
            vk = e.keycode
            if vk in (0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5):
                return "break"                           # 修飾キーだけの入力は待つ
            mods = self._pressed_mods(e)
            if not mods:
                self._hk_say("Ctrl / Alt / Shift / Win のどれかと一緒に押してください", True)
                return "break"
            if not (0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A or 0x70 <= vk <= 0x87):
                self._hk_say("そのキーは使えません。英数字キーか F1〜F24 を使ってください", True)
                return "break"
            spec = (mods, vk, format_hotkey(mods, vk))
            if self.hk is None:
                self._hk_say("ホットキー機能が起動していません", True)
                return "break"
            if self.hotkey_state.startswith(spec[2]) and not self.hook_mode:
                # 登録済みのキーなのにこの画面に届いた = Windows がホットキーとして横取りできていない
                log(f"hotkey {spec[2]} reached the dialog instead of being intercepted")
                self._hk_say(f"「{spec[2]}」は登録済みですが、押したキーが Windows に横取りされず、この画面に直接届きました。"
                             "他のソフトがキーを先に処理している可能性があります。"
                             "下の「強力モード」をオンにするか、別のキーを試してください", True)
                return "break"
            self._hk_msg.config(text=f"「{spec[2]}」を試しています…", foreground=self.c["fg"])
            self.hk.request(spec)
            return "break"

        def on_hotkey_ok(self, label):
            self.hotkey_label = label
            self.hotkey_state = f"{label} 有効 (トレイのアイコンからも開けます)"
            cfg = load_json(data_dir() / "config.json", {})
            cfg["hotkey"] = label
            save_json(data_dir() / "config.json", cfg)
            if self._hk_win is not None and self._hk_win.winfo_exists():
                self._hk_cur.config(text=label)
                self._hk_say(f"「{label}」を登録しました (保存済み)。いま実際にそのキーを押して、届くか確認してください")
            self.update_status()

        def on_hotkey_fired(self):
            """ホットキーが実際に押されて届いた時。"""
            self.show()
            if self._hk_win is not None and self._hk_win.winfo_exists():
                self._hk_win.lift()
                self._hk_say(f"✓ 「{self.hotkey_label}」が届きました。このキーで呼び出せます")

        def on_hook_toggle(self):
            self.hook_mode = bool(self._hk_hook_var.get())
            cfg = load_json(data_dir() / "config.json", {})
            cfg["hook_mode"] = self.hook_mode
            save_json(data_dir() / "config.json", cfg)
            if self.hk is not None:
                self.hk.set_hook_mode(self.hook_mode)
            self._hk_say("強力モードをオンにしました。いま登録中のキーを押して、届くか確認してください"
                         if self.hook_mode else "強力モードをオフにしました")

        def on_hotkey_fail(self, label, err, active):
            reason = "他のプログラムが使用中です" if err == 1409 else f"登録できませんでした (エラー {err})"
            if active:
                self.hotkey_label = active
                self.hotkey_state = f"{active} 有効"
            else:
                self.hotkey_state = f"✕ {label} を登録できません ({reason})。「ホットキー変更」から別のキーを選んでください"
            if self._hk_win is not None and self._hk_win.winfo_exists():
                self._hk_cur.config(text=active or "(なし)")
                self._hk_say(f"✕ 「{label}」は{reason}。" + (f"現在は「{active}」のままです" if active else "別のキーを試してください"), True)
            self.update_status()

        # ---------------------------------------------------------- 自動起動 / ステータス
        def _update_startup_btn(self):
            self.btn_startup.config(text="自動起動: ON" if startup_enabled() else "自動起動: OFF")

        def toggle_startup(self):
            try:
                set_startup(not startup_enabled())
            except Exception as e:
                messagebox.showerror("自動起動の設定に失敗しました", str(e), parent=self)
            self._update_startup_btn()
            self.entry.focus_set()

        def flash(self, msg, err=False):
            self._flash_on = True
            self.status.config(text=msg, foreground="#ff6b6b" if err else self.c["accent"])
            if self._flash_job:
                self.after_cancel(self._flash_job)
            self._flash_job = self.after(6000, self._end_flash)

        def _end_flash(self):
            self._flash_on, self._flash_job = False, None
            self.update_status()

        def update_status(self):
            if self._flash_on:
                return
            bad = self.hotkey_state.startswith("✕")
            self.status.config(
                text=f"未整理 {len(self.files)} 件   /   移動先フォルダ {len(self.finder.dests)} 個   /   {self.hotkey_state}",
                foreground="#ff6b6b" if bad else self.c["fg"])


def main():
    args = sys.argv[1:]
    if "--install-startup" in args or "--uninstall-startup" in args:
        try:
            set_startup("--install-startup" in args)
            print("自動起動を登録しました" if "--install-startup" in args else "自動起動を解除しました")
        except Exception as e:
            sys.exit(str(e))
        return
    if tk is None:
        sys.exit("tkinter が見つかりません。python.org 版 Python (tcl/tk 同梱) を使用してください。")
    sys.excepthook = lambda *a: log("uncaught:\n" + "".join(traceback.format_exception(*a)))
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    background = "--background" in args
    q = queue.Queue()
    server = start_single_instance(q, background)
    if server is None:
        return                       # 既に起動中 (そちらを前面に出した)
    cfg = load_json(data_dir() / "config.json", {})
    source = cfg.get("source") or downloads_dir()
    try:
        spec = hotkey_spec(cfg.get("hotkey") or DEFAULT_HOTKEY)
    except ValueError:
        spec = hotkey_spec(DEFAULT_HOTKEY)
    hook_mode = bool(cfg.get("hook_mode", False))
    hk = TrayThread(q, spec, hook_mode)
    app = App(source, q, background, hk, spec[2], hook_mode)
    hk.start()
    app.mainloop()


if __name__ == "__main__":
    main()
