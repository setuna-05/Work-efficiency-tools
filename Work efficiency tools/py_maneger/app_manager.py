"""A small Windows desktop manager for folder-based Python applications."""

from __future__ import annotations

import ctypes
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from ctypes import wintypes
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

APP_TITLE = "Pythonアプリ管理"
CONFIG_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PythonAppManager"
CONFIG_FILE = CONFIG_DIR / "apps.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
COLORS = {
    "background": "#17191f",
    "panel": "#22252d",
    "panel_alt": "#292d37",
    "text": "#f0f2f5",
    "muted": "#a6adbb",
    "accent": "#7aa2f7",
    "selected": "#354b70",
    "border": "#3a3f4b",
}


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class STARTUPINFO(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class WindowsProcessGroup:
    """Tracks the launcher and its child processes so Stop can end the whole app."""

    KILL_ON_JOB_CLOSE = 0x2000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
    CREATE_SUSPENDED = 0x00000004
    CREATE_NO_WINDOW = 0x08000000

    def __init__(self, script: Path):
        if os.name != "nt":
            raise OSError("このアプリはWindows専用です。")

        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        self.kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        self.kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CreateProcessW.argtypes = [
            wintypes.LPCWSTR, ctypes.POINTER(wintypes.WCHAR), ctypes.c_void_p,
            ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p,
            wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFO), ctypes.POINTER(PROCESS_INFORMATION),
        ]
        self.kernel32.CreateProcessW.restype = wintypes.BOOL
        self.kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

        self.job = self.kernel32.CreateJobObjectW(None, None)
        if not self.job:
            raise ctypes.WinError(ctypes.get_last_error())

        limits = EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = self.KILL_ON_JOB_CLOSE
        if not self.kernel32.SetInformationJobObject(
            self.job, self.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits), ctypes.sizeof(limits),
        ):
            self._close()
            raise ctypes.WinError(ctypes.get_last_error())

        suffix = script.suffix.lower()
        if suffix == ".bat":
            executable = os.environ.get("COMSPEC", "cmd.exe")
            command_line_text = f'"{executable}" /d /s /c ""{script}""'
        elif suffix == ".vbs":
            executable = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "wscript.exe")
            command_line_text = subprocess.list2cmdline([executable, str(script)])
        else:
            self._close()
            raise ValueError("起動できるのは .bat または .vbs ファイルです。")

        startup = STARTUPINFO()
        startup.cb = ctypes.sizeof(startup)
        startup.dwFlags = 0x00000001  # STARTF_USESHOWWINDOW
        startup.wShowWindow = 0  # SW_HIDE
        process_info = PROCESS_INFORMATION()
        command_line = ctypes.create_unicode_buffer(command_line_text)
        created = self.kernel32.CreateProcessW(
            executable, command_line, None, None, False,
            self.CREATE_SUSPENDED | self.CREATE_NO_WINDOW, None,
            str(script.parent), ctypes.byref(startup), ctypes.byref(process_info),
        )
        if not created:
            self._close()
            raise ctypes.WinError(ctypes.get_last_error())
        self.process_handle = process_info.hProcess
        if not self.kernel32.AssignProcessToJobObject(self.job, self.process_handle):
            error = ctypes.get_last_error()
            self.kernel32.TerminateProcess(self.process_handle, 1)
            self.kernel32.WaitForSingleObject(self.process_handle, 3000)
            self.kernel32.CloseHandle(process_info.hThread)
            self._close()
            raise ctypes.WinError(error)
        if self.kernel32.ResumeThread(process_info.hThread) == 0xFFFFFFFF:
            error = ctypes.get_last_error()
            self.kernel32.TerminateJobObject(self.job, 1)
            self.kernel32.CloseHandle(process_info.hThread)
            self._close()
            raise ctypes.WinError(error)
        self.kernel32.CloseHandle(process_info.hThread)

    def is_running(self) -> bool:
        if not self.job:
            return False
        info = BASIC_ACCOUNTING_INFORMATION()
        if not self.kernel32.QueryInformationJobObject(
            self.job, self.JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(info), ctypes.sizeof(info), None,
        ):
            return self.kernel32.WaitForSingleObject(self.process_handle, 0) == 0x00000102
        return info.ActiveProcesses > 0

    def stop(self) -> None:
        if self.job:
            self.kernel32.TerminateJobObject(self.job, 1)
            self._close()

    def detach(self) -> None:
        """Let apps continue running when the manager window closes."""
        if self.job:
            limits = EXTENDED_LIMIT_INFORMATION()
            self.kernel32.SetInformationJobObject(
                self.job, self.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits), ctypes.sizeof(limits),
            )
            self._close()

    def _close(self) -> None:
        if getattr(self, "process_handle", None):
            self.kernel32.CloseHandle(self.process_handle)
            self.process_handle = None
        if getattr(self, "job", None):
            self.kernel32.CloseHandle(self.job)
            self.job = None


class AppManager:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("850x500")
        self.root.minsize(700, 380)
        self.apps: list[dict] = []
        self.launch_hotkey = ""
        self.running: dict[str, WindowsProcessGroup] = {}
        self.external_running: dict[str, list[int]] = {}
        self.external_active: set[str] = set()
        self.external_lock = threading.Lock()
        self.stop_watch = threading.Event()
        self._build_ui()
        self._load()
        self._refresh()
        self.watch_thread = threading.Thread(target=self._watch_external_apps, daemon=True)
        self.watch_thread.start()
        self.root.after(1000, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        self.root.configure(background=COLORS["background"])
        style.configure("TFrame", background=COLORS["background"])
        style.configure("TLabel", background=COLORS["background"], foreground=COLORS["text"])
        style.configure(
            "TButton", background=COLORS["panel_alt"], foreground=COLORS["text"],
            bordercolor=COLORS["border"], padding=(12, 7),
        )
        style.map(
            "TButton",
            background=[("active", COLORS["selected"]), ("disabled", COLORS["panel"])],
            foreground=[("disabled", COLORS["muted"])],
        )
        style.configure(
            "Treeview", background=COLORS["panel"], fieldbackground=COLORS["panel"],
            foreground=COLORS["text"], rowheight=32, bordercolor=COLORS["border"],
        )
        style.map(
            "Treeview",
            background=[("selected", COLORS["selected"])],
            foreground=[("selected", COLORS["text"])],
        )
        style.configure(
            "Treeview.Heading", background=COLORS["panel_alt"], foreground=COLORS["text"],
            bordercolor=COLORS["border"], padding=(8, 7),
        )
        style.map("Treeview.Heading", background=[("active", COLORS["selected"])])
        self.root.option_add("*Dialog.msg.font", ("Yu Gothic UI", 10))

        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        heading = ttk.Frame(outer)
        heading.pack(fill="x", pady=(0, 12))
        ttk.Label(heading, text="Pythonアプリ管理", font=("Yu Gothic UI", 18, "bold")).pack(side="left")
        ttk.Button(heading, text="起動ホットキー", command=self._configure_launch_hotkey).pack(side="right", padx=(8, 0))
        ttk.Button(heading, text="＋ フォルダーを追加", command=self._add_folder).pack(side="right")

        columns = ("name", "script", "status")
        self.tree = ttk.Treeview(outer, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("name", text="アプリ名")
        self.tree.heading("script", text="起動ファイル")
        self.tree.heading("status", text="状態")
        self.tree.column("name", width=190, minwidth=120)
        self.tree.column("script", width=460, minwidth=220)
        self.tree.column("status", width=90, minwidth=80, anchor="center")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda _event: self._start_selected())

        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=(12, 0))
        ttk.Button(controls, text="起動", command=self._start_selected).pack(side="left")
        ttk.Button(controls, text="停止", command=self._stop_selected).pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="登録を削除", command=self._remove_selected).pack(side="left", padx=(8, 0))
        ttk.Label(
            controls, text="停止はこの画面から起動したアプリ、または検出したVBSアプリが対象です。",
            foreground=COLORS["muted"],
        ).pack(side="right")

    def _load(self) -> None:
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            self.apps = [item for item in data if Path(item.get("script", "")).is_file()]
        except FileNotFoundError:
            self.apps = []
        except (OSError, json.JSONDecodeError, TypeError):
            messagebox.showwarning(APP_TITLE, "登録内容を読み込めませんでした。新しい一覧として開始します。")
            self.apps = []
        try:
            settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            self.launch_hotkey = str(settings.get("launch_hotkey", ""))
        except (OSError, json.JSONDecodeError, TypeError):
            self.launch_hotkey = ""

    def _configure_launch_hotkey(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("管理アプリの起動キー")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)
        ttk.Label(dialog, text="このキーで管理アプリを起動します。\nCtrl / Alt / Shift のいずれかと、英数字または F1～F12 を組み合わせてください。", padding=(18, 16)).pack()
        value = tk.StringVar(value=self.launch_hotkey or "未設定")
        label = ttk.Label(dialog, textvariable=value, font=("Yu Gothic UI", 14, "bold"), anchor="center")
        label.pack(fill="x", padx=18, pady=(0, 12))
        captured = [""]

        def capture(event):
            modifiers = []
            if event.state & 0x0004:
                modifiers.append("Ctrl")
            if event.state & 0x0008 or event.state & 0x20000:
                modifiers.append("Alt")
            if event.state & 0x0001:
                modifiers.append("Shift")
            key = event.keysym.upper()
            if key in ("CONTROL_L", "CONTROL_R", "ALT_L", "ALT_R", "SHIFT_L", "SHIFT_R"):
                return "break"
            if not modifiers or not (len(key) == 1 and key.isalnum() or key.startswith("F") and key[1:].isdigit() and 1 <= int(key[1:]) <= 12):
                value.set("Ctrl / Alt / Shift と英数字または F1～F12 を押してください")
                return "break"
            captured[0] = "+".join(modifiers + [key])
            value.set(captured[0])
            return "break"

        dialog.bind("<KeyPress>", capture)
        dialog.after(100, dialog.focus_force)
        buttons = ttk.Frame(dialog, padding=(18, 4, 18, 16))
        buttons.pack(fill="x")

        def save():
            if not captured[0]:
                messagebox.showinfo(APP_TITLE, "先に設定するキーを押してください。", parent=dialog)
                return
            try:
                self._write_launch_shortcut(captured[0])
                self.launch_hotkey = captured[0]
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                SETTINGS_FILE.write_text(json.dumps({"launch_hotkey": self.launch_hotkey}, ensure_ascii=False, indent=2), encoding="utf-8")
            except (OSError, subprocess.SubprocessError) as exc:
                messagebox.showerror(APP_TITLE, f"起動キーを設定できませんでした。\n\n{exc}", parent=dialog)
                return
            messagebox.showinfo(APP_TITLE, f"起動キーを {captured[0]} に設定しました。\n管理アプリを閉じているときも、このキーで起動できます。", parent=dialog)
            dialog.destroy()

        ttk.Button(buttons, text="設定", command=save).pack(side="right")
        ttk.Button(buttons, text="キャンセル", command=dialog.destroy).pack(side="right", padx=(0, 8))

    @staticmethod
    def _write_launch_shortcut(hotkey: str) -> None:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if not pythonw.exists():
            located = shutil.which("pyw.exe") or shutil.which("pyw")
            if not located:
                raise OSError("pythonw.exe / pyw.exe が見つかりません。Pythonのインストールを確認してください。")
            pythonw = Path(located)
        shortcut = Path(os.environ.get("APPDATA", Path.home())) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / f"{APP_TITLE}.lnk"
        shortcut.parent.mkdir(parents=True, exist_ok=True)

        def ps_quote(value: str) -> str:
            return "'" + value.replace("'", "''") + "'"

        script_path = str(Path(__file__).resolve())
        ps = (
            "$ErrorActionPreference='Stop'; $ws=New-Object -ComObject WScript.Shell; "
            f"$sc=$ws.CreateShortcut({ps_quote(str(shortcut))}); "
            f"$sc.TargetPath={ps_quote(str(pythonw))}; "
            f"$sc.Arguments={ps_quote(subprocess.list2cmdline([script_path]))}; "
            f"$sc.WorkingDirectory={ps_quote(str(Path(script_path).parent))}; "
            f"$sc.Hotkey={ps_quote(hotkey.upper())}; $sc.Save()"
        )
        powershell = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
            creationflags=0x08000000,
        )
        if result.returncode:
            raise OSError(result.stderr.strip() or "Windowsがショートカットを保存できませんでした。")

    def _save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(self.apps, ensure_ascii=False, indent=2), encoding="utf-8")

    def _refresh(self) -> None:
        wanted = {item["script"] for item in self.apps}
        for row in self.tree.get_children():
            if row not in wanted:
                self.tree.delete(row)
        for item in self.apps:
            key = item["script"]
            managed_active = key in self.running and self.running[key].is_running()
            if key in self.running and not managed_active:
                self.running.pop(key).detach()
            with self.external_lock:
                external_active = key in self.external_active
            status = "起動中" if managed_active or external_active else "停止中"
            values = (item["name"], key, status)
            if self.tree.exists(key):
                self.tree.item(key, values=values)
            else:
                self.tree.insert("", "end", iid=key, values=values)

    def _add_folder(self) -> None:
        selected = filedialog.askdirectory(title="Pythonアプリのフォルダーを選択")
        if not selected:
            return
        folder = Path(selected)
        candidates = sorted(
            (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in (".bat", ".vbs")),
            key=lambda p: (p.suffix.lower(), p.name.lower()),
        )
        if not candidates:
            messagebox.showerror(APP_TITLE, "このフォルダーの直下に .bat または .vbs が見つかりませんでした。")
            return
        if len(candidates) == 1:
            script = candidates[0]
        else:
            script = self._choose_script(candidates)
            if not script:
                return
        if any(item["script"].casefold() == str(script).casefold() for item in self.apps):
            messagebox.showinfo(APP_TITLE, "この起動ファイルはすでに登録されています。")
            return
        name = simpledialog.askstring("アプリ名", "一覧に表示する名前:", initialvalue=folder.name, parent=self.root)
        if name is None:
            return
        self.apps.append({"name": name.strip() or folder.name, "script": str(script)})
        self._save()
        self._refresh()

    def _choose_script(self, choices: list[Path]) -> Path | None:
        dialog = tk.Toplevel(self.root)
        dialog.title("起動ファイルを選択")
        dialog.transient(self.root)
        dialog.grab_set()
        ttk.Label(dialog, text="起動に使うファイルを選んでください:", padding=12).pack(anchor="w")
        box = tk.Listbox(dialog, width=58, height=min(8, len(choices)), exportselection=False)
        box.configure(
            background=COLORS["panel"], foreground=COLORS["text"],
            selectbackground=COLORS["selected"], selectforeground=COLORS["text"],
            highlightbackground=COLORS["border"], highlightcolor=COLORS["accent"],
        )
        for choice in choices:
            box.insert("end", choice.name)
        box.selection_set(0)
        box.pack(padx=12, fill="both", expand=True)
        result: list[Path | None] = [None]

        def accept(_event=None):
            selected = box.curselection()
            if selected:
                result[0] = choices[selected[0]]
                dialog.destroy()

        buttons = ttk.Frame(dialog, padding=12)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="選択", command=accept).pack(side="right")
        ttk.Button(buttons, text="キャンセル", command=dialog.destroy).pack(side="right", padx=(0, 8))
        box.bind("<Double-1>", accept)
        self.root.wait_window(dialog)
        return result[0]

    def _selected(self) -> dict | None:
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo(APP_TITLE, "一覧からアプリを選択してください。")
            return None
        return next((item for item in self.apps if item["script"] == selected[0]), None)

    def _start_selected(self) -> None:
        item = self._selected()
        if not item:
            return
        key = item["script"]
        if key in self.running and self.running[key].is_running():
            messagebox.showinfo(APP_TITLE, "このアプリはすでに起動しています。")
            return
        with self.external_lock:
            externally_running = key in self.external_active
        if externally_running:
            messagebox.showinfo(APP_TITLE, "このアプリはすでに起動しています。トレイを確認してください。")
            return
        try:
            self.running[key] = WindowsProcessGroup(Path(key))
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"起動できませんでした。\n\n{exc}")
        self._refresh()

    def _stop_selected(self) -> None:
        item = self._selected()
        if not item:
            return
        group = self.running.pop(item["script"], None)
        if group and group.is_running():
            group.stop()
        else:
            if group:
                group.detach()
            with self.external_lock:
                pids = self.external_running.get(item["script"], [])[:]
                external_active = item["script"] in self.external_active
            stopped = self._stop_external_processes(pids)
            if stopped:
                with self.external_lock:
                    self.external_running.pop(item["script"], None)
                    self.external_active.discard(item["script"])
            else:
                if external_active:
                    messagebox.showwarning(
                        APP_TITLE,
                        "アプリは起動中ですが、終了対象のプロセスを特定できませんでした。\n"
                        "起動ファイル内のPythonスクリプト指定を確認してください。",
                    )
                else:
                    messagebox.showinfo(APP_TITLE, "このアプリの実行中プロセスを見つけられませんでした。")
        self._refresh()

    def _remove_selected(self) -> None:
        item = self._selected()
        if not item:
            return
        with self.external_lock:
            external_active = item["script"] in self.external_active
        if (item["script"] in self.running and self.running[item["script"]].is_running()) or external_active:
            messagebox.showwarning(APP_TITLE, "起動中のアプリを停止してから登録を削除してください。")
            return
        self.apps.remove(item)
        finished = self.running.pop(item["script"], None)
        if finished:
            finished.detach()
        self._save()
        self._refresh()

    def _poll(self) -> None:
        self._refresh()
        self.root.after(1000, self._poll)

    def _on_close(self) -> None:
        # Closing the manager should not unexpectedly stop the apps it launched.
        self.stop_watch.set()
        for group in self.running.values():
            group.detach()
        self.root.destroy()

    def _watch_external_apps(self) -> None:
        """Detect Python apps started outside this manager, including named mutexes."""
        command = (
            "$ErrorActionPreference='Stop'; "
            "$p = try { @(Get-CimInstance Win32_Process -ErrorAction Stop | "
            "Select-Object ProcessId,Name,CommandLine) } "
            "catch { @(Get-Process -Name 'python*' -ErrorAction SilentlyContinue | "
            "ForEach-Object { [pscustomobject]@{ ProcessId=$_.Id; Name=($_.ProcessName + '.exe'); CommandLine=$null } }) }; "
            "ConvertTo-Json -InputObject $p -Compress"
        )
        powershell = os.path.join(
            os.environ.get("WINDIR", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
        )
        while not self.stop_watch.is_set():
            if not self.apps:
                self.stop_watch.wait(3)
                continue
            try:
                result = subprocess.run(
                    [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=6, creationflags=0x08000000,
                )
                processes = json.loads(result.stdout) if result.stdout.strip() else []
                if processes is None:
                    processes = []
                elif isinstance(processes, dict):
                    processes = [processes]
                current: dict[str, list[int]] = {}
                active_keys: set[str] = set()
                for app in self.apps:
                    script = Path(app["script"])
                    launcher_text = self._read_text(script)
                    python_paths = set(re.findall(r"(?i)[^\s\"']+\.py\b", launcher_text))
                    python_paths.update(str(path) for path in script.parent.glob("*.py"))
                    python_names = {Path(path).name.casefold() for path in python_paths}
                    mutex_names = self._find_mutex_names(script, python_paths)
                    mutex_active = any(self._named_mutex_exists(name) for name in mutex_names)
                    folder_marker = str(script.parent).replace("/", "\\").casefold()
                    matched = []
                    for proc in processes:
                        name = str(proc.get("Name") or "").casefold()
                        args = str(proc.get("CommandLine") or "").replace("/", "\\").casefold()
                        if not name.startswith("python"):
                            continue
                        references_folder = folder_marker in args
                        references_script = any(
                            re.search(rf"(?<![\w.-]){re.escape(py_name)}(?![\w.-])", args)
                            for py_name in python_names
                        )
                        if references_folder or references_script:
                            try:
                                matched.append(int(proc["ProcessId"]))
                            except (KeyError, TypeError, ValueError):
                                continue
                    if matched:
                        current[app["script"]] = matched
                    if matched or mutex_active:
                        active_keys.add(app["script"])
                with self.external_lock:
                    self.external_running = current
                    self.external_active = active_keys
            except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
                pass
            self.stop_watch.wait(3)

    @staticmethod
    def _read_text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return ""

    @classmethod
    def _find_mutex_names(cls, launcher: Path, python_paths: set[str]) -> set[str]:
        """Read static CreateMutexW names from launcher-referenced Python sources."""
        sources = {Path(path) for path in python_paths}
        sources.update(launcher.parent.glob("*.py"))
        names: set[str] = set()
        for source_path in sources:
            source = cls._read_text(source_path)
            constants = {}
            for variable, _quote, literal in re.findall(
                r"(?m)^\s*([A-Za-z_]\w*)\s*=\s*(['\"])(.*?)\2", source
            ):
                constants[variable] = literal
            for match in re.finditer(
                r"(?is)\bCreateMutexW\s*\(\s*[^,]+,\s*[^,]+,\s*([^\)]+)\)", source
            ):
                value_parts = re.split(r"\s*\+\s*", match.group(1).strip())
                resolved = []
                for part in value_parts:
                    part = part.strip()
                    if part in constants:
                        resolved.append(constants[part])
                        continue
                    try:
                        literal = ast.literal_eval(part)
                    except (ValueError, SyntaxError):
                        resolved = []
                        break
                    if not isinstance(literal, str):
                        resolved = []
                        break
                    resolved.append(literal)
                if resolved:
                    names.add("".join(resolved))
        return names

    @staticmethod
    def _named_mutex_exists(name: str) -> bool:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenMutexW.restype = wintypes.HANDLE
        kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenMutexW(0x00100000, False, name)  # SYNCHRONIZE
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True

    @staticmethod
    def _stop_external_processes(pids: list[int]) -> bool:
        taskkill = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "taskkill.exe")
        success = False
        for pid in pids:
            try:
                result = subprocess.run(
                    [taskkill, "/PID", str(pid), "/T", "/F"],
                    capture_output=True, text=True, timeout=5, creationflags=0x08000000,
                )
                success = success or result.returncode == 0
            except (OSError, subprocess.SubprocessError):
                continue
        return success


def main() -> None:
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        ctypes.set_last_error(0)
        mutex = kernel32.CreateMutexW(None, False, "Local\\PythonAppManager_SingleInstance")
        if mutex and ctypes.get_last_error() == 183:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.FindWindowW.restype = wintypes.HWND
            hwnd = user32.FindWindowW(None, APP_TITLE)
            if hwnd:
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
            return
    root = tk.Tk()
    AppManager(root)
    root.mainloop()


if __name__ == "__main__":
    main()
