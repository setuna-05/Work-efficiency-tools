"""FileFinder - Win+R をファイル検索向けに使いやすくしたランチャー (MVP)

- Ctrl+Alt+R で検索ウィンドウを表示 (ホットキーは config.json で変更可)
- 左: 登録フォルダ(クリックで中身を表示 / 選択中はその中だけ検索)
       「すべて」を選ぶと Windows 検索インデックスで全体検索
- 右: エクスプローラー「詳細」風の一覧
- Enter / ダブルクリックで開く(開いたらウィンドウは自動で閉じる)
- Ctrl+C でパスコピー、右クリックで「フォルダを開く」など
- Esc で閉じる / タスクトレイに常駐
- 登録フォルダを直接エクスプローラーで開く(階層を辿らずに到達):
    ・ウィンドウ内: Alt+1 〜 Alt+9 (左の登録フォルダの上から順) / ダブルクリック / Enter
    ・どこからでも: フォルダごとに個別ホットキーを設定可
      (左のフォルダを右クリック →「ホットキーを設定…」。例: ctrl+alt+1)

必要: pip install PySide6 pywin32
"""
import ctypes
import datetime
import json
import os
import subprocess
import sys
import traceback
import unicodedata
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QFileInfo, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QDialogButtonBox, QFileDialog,
    QFileIconProvider, QHBoxLayout, QHeaderView, QKeySequenceEdit, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton,
    QSplitter, QStyle, QSystemTrayIcon, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

APP_NAME = "FileFinder"
MAX_RESULTS = 300
# folder_hotkeys: {"登録フォルダのパス": "ctrl+alt+1", ...} どこからでもそのフォルダを開くホットキー
DEFAULT_CONFIG = {"hotkey": "ctrl+alt+r", "folders": [], "folder_hotkeys": {}}
SINGLE_INSTANCE_NAME = "FileFinder_single_instance_v1"


# ---------------------------------------------------------------- 設定
def config_path() -> Path:
    base = Path(os.environ.get("APPDATA", Path.home()))
    d = base / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d / "config.json"


def load_config() -> dict:
    p = config_path()
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # 深いコピー
    if p.exists():
        try:
            cfg.update(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass
    if not isinstance(cfg.get("folder_hotkeys"), dict):
        cfg["folder_hotkeys"] = {}
    return cfg


def save_config(cfg: dict) -> None:
    config_path().write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def log_path() -> Path:
    return config_path().with_name("log.txt")


def log(msg: str) -> None:
    """動作記録 (%APPDATA%\\FileFinder\\log.txt)。黒い画面なしでも原因を追えるようにする"""
    try:
        with open(log_path(), "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} [{os.getpid()}] {msg}\n")
    except Exception:
        pass


def _excepthook(exc_type, exc, tb):
    log("UNCAUGHT " + "".join(traceback.format_exception(exc_type, exc, tb)))


# ---------------------------------------------------------------- ホットキー
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012


_MOD_ALIASES = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL, "ctl": MOD_CONTROL,
    "alt": MOD_ALT, "option": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN, "windows": MOD_WIN, "meta": MOD_WIN, "super": MOD_WIN,
}
_NAMED_KEYS = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09,
    "esc": 0x1B, "escape": 0x1B, "backspace": 0x08,
    "pgup": 0x21, "pageup": 0x21, "pgdn": 0x22, "pgdown": 0x22, "pagedown": 0x22,
    "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "ins": 0x2D, "insert": 0x2D, "del": 0x2E, "delete": 0x2E,
}


def parse_hotkey(text: str):
    """"Ctrl+Alt+R" のような文字列を (修飾キー, 仮想キーコード) にする。
    全角文字・大文字小文字・空白を吸収し、解釈できない単語は黙って無視せずエラーにする"""
    norm = unicodedata.normalize("NFKC", text or "").lower()  # 全角の ＋ や ｃｔｒｌ も半角に
    mods, vk = 0, None
    for tok in [t.strip() for t in norm.split("+")]:
        if not tok:
            continue
        if tok in _MOD_ALIASES:
            mods |= _MOD_ALIASES[tok]
            continue
        if vk is not None:
            raise ValueError(f"キーが2つ指定されています: {text}")
        if tok in _NAMED_KEYS:
            vk = _NAMED_KEYS[tok]
        elif tok.startswith("f") and tok[1:].isdigit() and 1 <= int(tok[1:]) <= 24:
            vk = 0x70 + int(tok[1:]) - 1
        elif len(tok) == 1 and tok.isascii() and tok.isalnum():
            vk = ord(tok.upper())
        else:
            raise ValueError(f"「{tok}」を解釈できません: {text}")
    if vk is None:
        raise ValueError(f"キーが指定されていません: {text}")
    if mods == 0 and not (0x70 <= vk <= 0x87):  # 単独の文字キーを全体で横取りしないための安全策
        raise ValueError(f"Ctrl / Alt / Shift / Win のどれかと組み合わせてください: {text}")
    return mods, vk


def same_hotkey(a: str, b: str) -> bool:
    try:
        return parse_hotkey(a) == parse_hotkey(b)
    except ValueError:
        return False


class HotkeyDialog(QDialog):
    """キーを実際に押して登録するダイアログ(文字入力の打ち間違い・IMEの影響を避ける)"""

    def __init__(self, title: str, current: str, allow_empty: bool):
        super().__init__(None)
        self.setWindowTitle(title)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        lay = QVBoxLayout(self)
        note = "下の欄をクリックして、割り当てたいキーを実際に押してください。\n例: Ctrl と Alt を押しながら 1"
        if allow_empty:
            note += "\n空欄のまま OK で解除します。"
        lay.addWidget(QLabel(note))
        self.edit = QKeySequenceEdit()
        self.edit.setMaximumSequenceLength(1)
        if current:
            self.edit.setKeySequence(QKeySequence(current))
        lay.addWidget(self.edit)
        row = QHBoxLayout()
        clear = QPushButton("クリア")
        clear.clicked.connect(self.edit.clear)
        row.addWidget(clear)
        row.addStretch(1)
        lay.addLayout(row)
        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        lay.addWidget(box)
        self.edit.setFocus()

    def value(self) -> str:
        return self.edit.keySequence().toString(QKeySequence.PortableText).lower()


def ask_hotkey(title: str, current: str, allow_empty: bool = True) -> str | None:
    """キャンセルなら None、OKならホットキー文字列(解除なら空文字)"""
    dlg = HotkeyDialog(title, current, allow_empty)
    if dlg.exec() != QDialog.Accepted:
        return None
    return dlg.value()


MAIN_HOTKEY_ID = 1       # 検索ウィンドウを出すホットキー
FOLDER_HOTKEY_BASE = 100  # 登録フォルダ用: 100 + (登録順の番号)


class HotkeyThread(QThread):
    """複数のホットキーをまとめて登録して待ち受ける。
    hotkeys: {id: "ctrl+alt+r", ...}  押されると pressed(id) を出す"""
    pressed = Signal(int)
    failed = Signal(str)

    def __init__(self, hotkeys: dict[int, str]):
        super().__init__()
        self.hotkeys = dict(hotkeys)
        self._tid = None

    def run(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)  # 正確なエラーコードを取るため
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self._tid = ctypes.windll.kernel32.GetCurrentThreadId()
        registered: list[int] = []
        errors: list[str] = []
        for hid, text in self.hotkeys.items():
            try:
                mods, vk = parse_hotkey(text)
            except ValueError as e:
                log(f"hotkey parse error: {e}")
                errors.append(str(e))
                continue
            if user32.RegisterHotKey(None, hid, mods | MOD_NOREPEAT, vk):
                registered.append(hid)
                log(f"hotkey registered: id={hid} {text} (mods={mods}, vk={vk})")
            else:
                err = ctypes.get_last_error()
                log(f"RegisterHotKey FAILED id={hid} {text} mods={mods} vk={vk} err={err}")
                why = "他のアプリがすでに使っています" if err == 1409 else f"エラーコード {err}"
                errors.append(f"{text} を登録できませんでした({why})")
        if errors:
            self.failed.emit("ホットキーの問題:\n" + "\n".join(errors))
        if not registered:
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_HOTKEY:
                hid = int(msg.wParam)
                log(f"hotkey pressed id={hid}")
                self.pressed.emit(hid)
        for hid in registered:
            user32.UnregisterHotKey(None, hid)
        log("hotkey thread ended")

    def stop(self):
        if self._tid:
            ctypes.windll.user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        self.wait(1000)


def open_folder(path: str) -> str | None:
    """フォルダをエクスプローラーで開く。失敗したら理由の文字列、成功なら None"""
    if not os.path.isdir(path):
        return f"フォルダが見つかりません:\n{path}"
    try:
        # 他のプロセスが前面に出られるように許可する(失敗しても無視)
        try:
            ctypes.windll.user32.AllowSetForegroundWindow(0xFFFFFFFF)
        except Exception:  # noqa: BLE001
            pass
        os.startfile(path)
    except OSError as e:
        return f"開けませんでした:\n{path}\n{e}"
    return None


# ---------------------------------------------------------------- 検索
def _sql_str(s: str) -> str:
    return s.replace("'", "''")


def _fmt_date(v) -> str:
    if v is None:
        return ""
    try:
        return v.strftime("%Y/%m/%d %H:%M")
    except Exception:
        return str(v)


def search_index(query: str, folder: str | None) -> list[dict]:
    """Windows Search インデックス (SystemIndex) をファイル名の部分一致で検索"""
    import pythoncom
    import win32com.client

    terms = [t for t in query.split() if t]
    conds = [f"System.FileName LIKE '%{_sql_str(t)}%'" for t in terms]
    if folder:
        conds.append(f"SCOPE='file:{_sql_str(folder)}'")
    sql = (
        f"SELECT TOP {MAX_RESULTS} System.ItemPathDisplay, System.DateModified, "
        "System.ItemTypeText, System.Size, System.FileAttributes "
        "FROM SystemIndex WHERE " + " AND ".join(conds) +
        " ORDER BY System.DateModified DESC"
    )
    rows = []
    pythoncom.CoInitialize()
    try:
        conn = win32com.client.Dispatch("ADODB.Connection")
        conn.Open("Provider=Search.CollatorDSO;Extended Properties='Application=Windows';")
        rs = win32com.client.Dispatch("ADODB.Recordset")
        rs.Open(sql, conn)
        while not rs.EOF:
            path = rs.Fields.Item("System.ItemPathDisplay").Value
            if path:
                attrs = rs.Fields.Item("System.FileAttributes").Value or 0
                size = rs.Fields.Item("System.Size").Value
                rows.append({
                    "name": os.path.basename(path) or path,
                    "path": path,
                    "modified": _fmt_date(rs.Fields.Item("System.DateModified").Value),
                    "type": rs.Fields.Item("System.ItemTypeText").Value or "",
                    "size": int(size) if size is not None else None,
                    "isdir": bool(int(attrs) & 0x10),
                })
            rs.MoveNext()
        rs.Close()
        conn.Close()
    finally:
        pythoncom.CoUninitialize()
    return rows


def list_folder(folder: str) -> list[dict]:
    """登録フォルダの直下を一覧(インデックス非依存)"""
    rows = []
    with os.scandir(folder) as it:
        for e in it:
            try:
                st = e.stat()
                isdir = e.is_dir()
            except OSError:
                continue
            ext = os.path.splitext(e.name)[1].lstrip(".").upper()
            rows.append({
                "name": e.name,
                "path": e.path,
                "modified": _fmt_date(datetime.datetime.fromtimestamp(st.st_mtime)),
                "type": "ファイル フォルダー" if isdir else (f"{ext} ファイル" if ext else "ファイル"),
                "size": None if isdir else st.st_size,
                "isdir": isdir,
            })
    rows.sort(key=lambda r: (not r["isdir"], r["name"].lower()))
    return rows[:2000]


class SearchWorker(QThread):
    done = Signal(int, list, str)

    def __init__(self, seq: int, query: str, folder: str | None):
        super().__init__()
        self.seq, self.query, self.folder = seq, query, folder

    def run(self):
        try:
            if self.query:
                rows = search_index(self.query, self.folder)
            elif self.folder:
                rows = list_folder(self.folder)
            else:
                rows = []
            self.done.emit(self.seq, rows, "")
        except Exception as e:  # noqa: BLE001
            self.done.emit(self.seq, [], str(e))


# ---------------------------------------------------------------- UI部品
class SearchBox(QLineEdit):
    down_pressed = Signal()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Down:
            self.down_pressed.emit()
            return
        super().keyPressEvent(e)


class SortItem(QTableWidgetItem):
    """数値・日付を正しく並べ替えるためのアイテム"""

    def __lt__(self, other):
        a, b = self.data(Qt.UserRole), other.data(Qt.UserRole)
        if a is not None and b is not None:
            return a < b
        return super().__lt__(other)


def fmt_size(n) -> str:
    if n is None:
        return ""
    kb = max(1, (n + 1023) // 1024)
    return f"{kb:,} KB"


ALL_LABEL = "★ すべて(全体検索)"
COLUMNS = ["名前", "更新日時", "種類", "サイズ", "場所"]


class MainWindow(QWidget):
    hotkeys_changed = Signal()  # フォルダごとのホットキー設定が変わった(再登録が必要)

    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.seq = 0
        self.workers: set[SearchWorker] = set()
        self.icons = QFileIconProvider()
        self.rows: list[dict] = []

        self.setWindowTitle(APP_NAME)
        self.resize(1000, 560)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)

        # 検索ボックス
        self.search = SearchBox()
        self.search.setPlaceholderText("ファイル名を入力 (空欄 + フォルダ選択でフォルダ内を一覧)")
        self.search.textChanged.connect(lambda: self.timer.start())
        self.search.returnPressed.connect(self.open_current_or_first)
        self.search.down_pressed.connect(self.focus_table)

        self.timer = QTimer(self, singleShot=True, interval=250)
        self.timer.timeout.connect(self.refresh)

        # 左: 登録フォルダ
        self.folder_list = QListWidget()
        self.folder_list.currentRowChanged.connect(lambda _: self.refresh())
        self.folder_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.folder_list.customContextMenuRequested.connect(self.folder_menu)
        # ダブルクリック / Enter で、そのフォルダをエクスプローラーで開く
        self.folder_list.itemActivated.connect(self.open_folder_item)
        folder_hint = QLabel("Alt+数字 / ダブルクリックで\nエクスプローラーで開く")
        folder_hint.setStyleSheet("color: gray;")
        add_btn = QPushButton("＋ 追加")
        add_btn.clicked.connect(self.add_folder)
        del_btn = QPushButton("－ 削除")
        del_btn.clicked.connect(self.remove_folder)
        btns = QHBoxLayout()
        btns.addWidget(add_btn)
        btns.addWidget(del_btn)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(QLabel("登録フォルダ"))
        lv.addWidget(self.folder_list)
        lv.addWidget(folder_hint)
        lv.addLayout(btns)

        # 右: 詳細リスト
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setSortingEnabled(True)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.Interactive)
        h.setStretchLastSection(True)
        for i, w in enumerate([300, 130, 130, 80, 300]):
            self.table.setColumnWidth(i, w)
        self.table.itemActivated.connect(lambda _: self.open_current())
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.table_menu)
        QShortcut(QKeySequence.Copy, self.table, activated=self.copy_path,
                  context=Qt.WidgetShortcut)

        self.status = QLabel("")

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(self.table)
        split.setStretchFactor(1, 1)
        split.setSizes([220, 780])

        root = QVBoxLayout(self)
        root.addWidget(self.search)
        root.addWidget(split, 1)
        root.addWidget(self.status)

        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=self.hide)
        QShortcut(QKeySequence("Ctrl+L"), self, activated=self.focus_search)
        # Alt+1 〜 Alt+9: 左の登録フォルダを上から順にエクスプローラーで開く
        for n in range(1, 10):
            QShortcut(QKeySequence(f"Alt+{n}"), self,
                      activated=lambda n=n: self.open_registered_folder(n))

        self.reload_folders()

    # ---- フォルダ管理
    def reload_folders(self, select_row: int = 0):
        self.folder_list.blockSignals(True)
        self.folder_list.clear()
        self.folder_list.addItem(QListWidgetItem(ALL_LABEL))
        for i, p in enumerate(self.cfg["folders"]):
            name = os.path.basename(p) or p
            label = f"[{i + 1}] {name}" if i < 9 else name  # [n] は Alt+n で開く番号
            hk = self.cfg["folder_hotkeys"].get(p)
            it = QListWidgetItem(self.icons.icon(QFileInfo(p)), label)
            tip = p + (f"\nホットキー: {hk}" if hk else "")
            if i < 9:
                tip += f"\nAlt+{i + 1} でエクスプローラーで開く"
            it.setToolTip(tip)
            it.setData(Qt.UserRole, p)
            self.folder_list.addItem(it)
        self.folder_list.setCurrentRow(select_row)
        self.folder_list.blockSignals(False)
        self.refresh()

    def current_folder(self) -> str | None:
        it = self.folder_list.currentItem()
        return it.data(Qt.UserRole) if it else None

    def add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "登録するフォルダを選択")
        if d:
            d = os.path.normpath(d)
            if d not in self.cfg["folders"]:
                self.cfg["folders"].append(d)
                save_config(self.cfg)
            self.reload_folders(self.cfg["folders"].index(d) + 1)

    def remove_folder(self):
        p = self.current_folder()
        if p and p in self.cfg["folders"]:
            self.cfg["folders"].remove(p)
            had_hotkey = self.cfg["folder_hotkeys"].pop(p, None) is not None
            save_config(self.cfg)
            self.reload_folders(0)
            # 登録順の番号が変わるので、ホットキーを登録し直す
            if had_hotkey or self.cfg["folder_hotkeys"]:
                self.hotkeys_changed.emit()

    # ---- 登録フォルダを直接開く
    def open_folder_path(self, path: str):
        err = open_folder(path)
        if err:
            QMessageBox.warning(self, APP_NAME, err)
            return
        self.hide()  # 開いたら自動で閉じる

    def open_folder_item(self, item: QListWidgetItem):
        p = item.data(Qt.UserRole)
        if p:  # 「すべて」の項目は対象外
            self.open_folder_path(p)

    def open_registered_folder(self, n: int):
        """Alt+n: 左の登録フォルダの n 番目(1始まり)をエクスプローラーで開く"""
        if 1 <= n <= len(self.cfg["folders"]):
            self.open_folder_path(self.cfg["folders"][n - 1])
        else:
            self.status.setText(f"{n} 番目の登録フォルダはありません")

    def set_folder_hotkey(self, path: str):
        current = self.cfg["folder_hotkeys"].get(path, "")
        text = ask_hotkey(f"ホットキー: {os.path.basename(path) or path}", current)
        if text is None:  # キャンセル
            return
        if text:
            try:
                parse_hotkey(text)
            except ValueError as e:
                QMessageBox.warning(self, APP_NAME, str(e))
                return
            if same_hotkey(text, self.cfg["hotkey"]):
                QMessageBox.warning(self, APP_NAME, "検索ウィンドウのホットキーと同じです。別の組み合わせにしてください。")
                return
            for other, hk in self.cfg["folder_hotkeys"].items():
                if other != path and same_hotkey(hk, text):
                    QMessageBox.warning(self, APP_NAME, f"すでに別のフォルダで使っています:\n{other}")
                    return
            self.cfg["folder_hotkeys"][path] = text
        else:
            self.cfg["folder_hotkeys"].pop(path, None)
        save_config(self.cfg)
        row = self.folder_list.currentRow()
        self.reload_folders(row)
        self.hotkeys_changed.emit()

    def folder_menu(self, pos):
        item = self.folder_list.itemAt(pos)
        p = item.data(Qt.UserRole) if item else None
        if not p:
            return
        self.folder_list.setCurrentItem(item)
        m = QMenu(self)
        m.addAction("エクスプローラーで開く", lambda: self.open_folder_path(p))
        m.addAction("ホットキーを設定…", lambda: self.set_folder_hotkey(p))
        m.addSeparator()
        m.addAction("登録を解除", self.remove_folder)
        m.exec(self.folder_list.mapToGlobal(pos))

    # ---- 検索
    def refresh(self):
        self.seq += 1
        w = SearchWorker(self.seq, self.search.text().strip(), self.current_folder())
        self.workers.add(w)
        w.done.connect(self.on_done)
        w.finished.connect(lambda w=w: self.workers.discard(w))
        self.status.setText("検索中…")
        w.start()

    def on_done(self, seq: int, rows: list, err: str):
        if seq != self.seq:  # 古い結果は捨てる
            return
        if err:
            self.status.setText(f"エラー: {err}")
            rows = []
        self.rows = rows
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for r, d in enumerate(rows):
            name = SortItem(d["name"])
            name.setIcon(self.icons.icon(QFileInfo(d["path"])))
            name.setData(Qt.UserRole + 1, d["path"])
            name.setData(Qt.UserRole, (not d["isdir"], d["name"].lower()))
            self.table.setItem(r, 0, name)
            self.table.setItem(r, 1, SortItem(d["modified"]))
            self.table.setItem(r, 2, SortItem(d["type"]))
            sz = SortItem(fmt_size(d["size"]))
            sz.setData(Qt.UserRole, d["size"] if d["size"] is not None else -1)
            sz.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(r, 3, sz)
            self.table.setItem(r, 4, SortItem(os.path.dirname(d["path"])))
        self.table.setSortingEnabled(True)
        if rows:
            self.table.selectRow(0)
        if not err:
            cap = f"(上限 {MAX_RESULTS} 件)" if len(rows) >= MAX_RESULTS else ""
            self.status.setText(f"{len(rows)} 件 {cap}")

    # ---- 操作
    def current_path(self) -> str | None:
        r = self.table.currentRow()
        if r < 0:
            return None
        it = self.table.item(r, 0)
        return it.data(Qt.UserRole + 1) if it else None

    def open_current(self):
        p = self.current_path()
        if not p:
            return
        try:
            os.startfile(p)
        except OSError as e:
            QMessageBox.warning(self, APP_NAME, f"開けませんでした:\n{p}\n{e}")
            return
        self.hide()  # 開いたら自動で閉じる

    def open_current_or_first(self):
        if self.table.currentRow() < 0 and self.table.rowCount() > 0:
            self.table.selectRow(0)
        self.open_current()

    def copy_path(self):
        p = self.current_path()
        if p:
            QGuiApplication.clipboard().setText(p)
            self.status.setText(f"パスをコピーしました: {p}")

    def open_containing_folder(self):
        p = self.current_path()
        if p:
            subprocess.Popen(["explorer", f"/select,{p}"])
            self.hide()

    def table_menu(self, pos):
        if not self.current_path():
            return
        m = QMenu(self)
        m.addAction("開く", self.open_current)
        m.addAction("パスをコピー (Ctrl+C)", self.copy_path)
        m.addAction("フォルダを開く", self.open_containing_folder)
        m.exec(self.table.viewport().mapToGlobal(pos))

    # ---- フォーカス / 表示
    def focus_table(self):
        if self.table.rowCount() > 0:
            self.table.setFocus()
            if self.table.currentRow() < 0:
                self.table.selectRow(0)

    def focus_search(self):
        self.search.setFocus()
        self.search.selectAll()

    def _force_visible(self):
        """非表示指定(SW_HIDE)で起動されたプロセスでは、最初のShowWindowが無視されて
        ウィンドウが実際には出ないことがある。Win32 APIで明示的に表示し直す"""
        try:
            user32 = ctypes.windll.user32
            user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.SetForegroundWindow.argtypes = [wintypes.HWND]
            hwnd = wintypes.HWND(int(self.winId()))
            user32.ShowWindow(hwnd, 1)  # SW_SHOWNORMAL (1回目が無視されても2回目で表示される)
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
        except Exception as e:  # noqa: BLE001
            log(f"force_visible failed: {e}")

    def show_and_focus(self):
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized | Qt.WindowActive)
        self._force_visible()
        self.raise_()
        self.activateWindow()
        self.focus_search()

    def closeEvent(self, e):  # ×ボタンは終了ではなく非表示(常駐)
        e.ignore()
        self.hide()


# ---------------------------------------------------------------- 起動
def main():
    if sys.platform != "win32":
        print("このアプリは Windows 専用です。")
        return 1
    sys.excepthook = _excepthook
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # 二重起動の防止: すでに起動中なら、そのウィンドウを前面に出して終了する
    sock = QLocalSocket()
    sock.connectToServer(SINGLE_INSTANCE_NAME)
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.waitForBytesWritten(300)
        sock.disconnectFromServer()
        log("already running -> asked the running instance to show its window, exiting")
        return 0
    QLocalServer.removeServer(SINGLE_INSTANCE_NAME)
    server = QLocalServer()
    server.listen(SINGLE_INSTANCE_NAME)

    cfg = load_config()
    save_config(cfg)
    log(f"start: hotkey={cfg['hotkey']} folders={len(cfg['folders'])}")

    win = MainWindow(cfg)

    def on_second_launch():
        conn = server.nextPendingConnection()
        if conn:
            conn.close()
        log("second launch requested -> show window")
        win.show_and_focus()

    server.newConnection.connect(on_second_launch)

    tray = QSystemTrayIcon(app.style().standardIcon(QStyle.SP_FileDialogContentsView), app)
    menu = QMenu()
    act_show = QAction("検索ウィンドウを表示")
    act_show.triggered.connect(win.show_and_focus)
    act_hotkey = QAction("検索ウィンドウのホットキーを変更…")
    act_quit = QAction("終了")
    act_quit.triggered.connect(app.quit)
    menu.addAction(act_show)
    menu.addAction(act_hotkey)
    menu.addSeparator()
    menu.addAction(act_quit)
    tray.setContextMenu(menu)
    tray.setToolTip(f"{APP_NAME} ({cfg['hotkey']})")
    tray.activated.connect(
        lambda reason: win.show_and_focus() if reason == QSystemTrayIcon.Trigger else None)
    tray.show()

    # ホットキー: 検索ウィンドウ用(1つ) + フォルダごとの個別ホットキー(設定した分だけ)
    state: dict = {"thread": None, "folders": {}}

    def on_hotkey(hid: int):
        if hid == MAIN_HOTKEY_ID:
            log("show window (hotkey)")
            win.show_and_focus()
            return
        path = state["folders"].get(hid)
        if not path:
            return
        err = open_folder(path)
        log(f"open folder (hotkey) {path} -> {'OK' if not err else err}")
        if err:
            tray.showMessage(APP_NAME, err, QSystemTrayIcon.Warning, 4000)

    def start_hotkeys():
        old = state["thread"]
        if old:
            old.stop()
        hotkeys = {MAIN_HOTKEY_ID: cfg["hotkey"]}
        state["folders"] = {}
        for i, p in enumerate(cfg["folders"]):
            hk = cfg["folder_hotkeys"].get(p)
            if hk:
                hid = FOLDER_HOTKEY_BASE + i
                hotkeys[hid] = hk
                state["folders"][hid] = p
        th = HotkeyThread(hotkeys)
        th.pressed.connect(on_hotkey)
        th.failed.connect(lambda msg: QMessageBox.warning(None, APP_NAME, msg))
        th.start()
        state["thread"] = th

    def change_main_hotkey():
        new = ask_hotkey("検索ウィンドウのホットキー", cfg["hotkey"], allow_empty=False)
        if new is None:
            return
        try:
            parse_hotkey(new)
        except ValueError as e:
            QMessageBox.warning(None, APP_NAME, str(e))
            return
        for other, hk in cfg["folder_hotkeys"].items():
            if same_hotkey(hk, new):
                QMessageBox.warning(None, APP_NAME, f"フォルダ用のホットキーと重なっています:\n{other}")
                return
        cfg["hotkey"] = new
        save_config(cfg)
        tray.setToolTip(f"{APP_NAME} ({new})")
        start_hotkeys()

    act_hotkey.triggered.connect(change_main_hotkey)
    win.hotkeys_changed.connect(start_hotkeys)
    start_hotkeys()
    app.aboutToQuit.connect(lambda: state["thread"] and state["thread"].stop())

    win.show_and_focus()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
