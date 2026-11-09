"""Owner-local setup/control windows. Secrets never travel through chat/stdout."""
from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from dataclasses import asdict, replace
from pathlib import Path

from .approvals import ApprovalBroker
from .config import Config
from .security import GateError, Vault, _dpapi, private_directory
from .store import Store


def save_config(path: Path, config: Config):
    lines = []
    for name, value in asdict(config).items():
        lines.append(name + " = " + (str(value).lower() if isinstance(value, bool) else json.dumps(value)))
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    Config.load(temporary)
    os.replace(temporary, path)


def launch(config_path: Path, root: Path, action="serve"):
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    executable = pythonw if pythonw.is_file() else python
    args = [str(executable), "-B", "-m", "jarvis", "--config", str(config_path.resolve()),
            "--state", str(root.resolve()), action]
    if action == "serve":
        args.append("--tray")
    return subprocess.Popen(args, cwd=Path(__file__).resolve().parent.parent,
                            creationflags=0x08000000 if os.name == "nt" else 0)


class Window:
    def __init__(self, title: str, config_path: Path, root: Path):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.path, self.root = config_path.resolve(), private_directory(root.resolve())
        self.config = Config.load(self.path if self.path.exists() else None)
        self.window = tk.Tk()
        self.window.title(title)
        self.window.geometry("820x650")
        self.window.minsize(650, 480)
        self.events = queue.Queue()
        self.status = tk.StringVar(value="Ready")
        self.frame = ttk.Frame(self.window, padding=16)
        self.frame.pack(fill="both", expand=True)
        ttk.Label(self.window, textvariable=self.status, padding=8).pack(fill="x")
        self.window.after(100, self.pump)

    def pump(self):
        try:
            while True:
                callback = self.events.get_nowait()
                callback()
        except queue.Empty:
            pass
        self.window.after(100, self.pump)

    def background(self, function, success=None):
        self.status.set("Working…")
        def run():
            try:
                result = function()
                self.events.put(lambda: (self.status.set("Done"), success(result) if success else None))
            except Exception as error:
                # Some libraries include phone numbers/keys in exception strings.
                kind = type(error).__name__
                messages = {
                    "ApiIdInvalidError": "Telegram rejected the API ID/hash. Copy both from your API development tools page.",
                    "PhoneCodeInvalidError": "That Telegram login code was not accepted. Try the latest code.",
                    "PhoneCodeExpiredError": "The Telegram code expired. Start login again for a new code.",
                    "PhoneNumberInvalidError": "Use the spare account's full phone number, including country code.",
                    "PasswordHashInvalidError": "The Telegram 2FA password was not accepted.",
                    "UsernameNotOccupiedError": "That personal Telegram username was not found. Check its spelling.",
                }
                message = str(error) if isinstance(error, GateError) else messages.get(kind, "Could not complete the operation (" + kind + "). Check the entries and try again.")
                self.events.put(lambda: self.status.set(message))
        threading.Thread(target=run, daemon=True).start()

    def prompt(self, title: str, message: str, secret=False) -> str:
        from tkinter import simpledialog
        done, result = threading.Event(), []
        def ask():
            result.append(simpledialog.askstring(title, message, show="*" if secret else None, parent=self.window))
            done.set()
        self.events.put(ask)
        done.wait()
        if not result[0]:
            raise GateError("Cancelled")
        return result[0]

    def run(self):
        self.window.mainloop()


class SetupWindow(Window):
    def __init__(self, config_path: Path, root: Path):
        super().__init__("JARVIS — secure setup", config_path, root)
        t, tk = self.ttk, self.tk
        self.frame.columnconfigure(1, weight=1)
        t.Label(self.frame, text="Connect your accounts", font=("Segoe UI", 18, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        t.Label(self.frame, text="Enter credentials here, on this computer. Do not paste them into the chat.", wraplength=740).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 16))
        self.values = {}
        fields = [("owner", "Your personal Telegram @username or numeric ID", str(self.config.owner_id or ""), False),
                  ("api_id", "Telegram app API ID", str(self.config.telegram_api_id or ""), False),
                  ("api_hash", "Telegram app API hash", "", True),
                  ("gemini", "Gemini API key (optional with local speech)", "", True)]
        for row, (name, label, initial, hidden) in enumerate(fields, 2):
            t.Label(self.frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=8)
            variable = tk.StringVar(value=initial)
            self.values[name] = variable
            t.Entry(self.frame, textvariable=variable, show="*" if hidden else "").grid(row=row, column=1, sticky="ew")
        self.free = tk.BooleanVar(value=self.config.free_cloud_confirmed)
        t.Checkbutton(self.frame, text="My Gemini API project has no billing attached (free tier only)", variable=self.free).grid(row=6, column=0, columnspan=2, sticky="w", pady=12)
        links = t.Frame(self.frame)
        links.grid(row=7, column=0, columnspan=2, sticky="w")
        t.Button(links, text="Get Telegram app credentials", command=lambda: webbrowser.open("https://my.telegram.org/apps")).pack(side="left", padx=(0, 8))
        t.Button(links, text="Get a Gemini API key", command=lambda: webbrowser.open("https://aistudio.google.com/apikey")).pack(side="left")
        t.Label(self.frame, text="Telegram login uses the SPARE account. You will enter its phone, login code and 2FA password in local prompts. Your personal account is the call recipient.", wraplength=740).grid(row=8, column=0, columnspan=2, sticky="w", pady=20)
        buttons = t.Frame(self.frame)
        buttons.grid(row=9, column=0, columnspan=2, sticky="w")
        t.Button(buttons, text="Save settings", command=self.save).pack(side="left", padx=(0, 8))
        self.login_button = t.Button(buttons, text="Log in spare Telegram account", command=self.login)
        self.login_button.pack(side="left", padx=(0, 8))
        t.Button(buttons, text="Open controls", command=lambda: launch(self.path, self.root, "control")).pack(side="left")
        t.Label(self.frame, text="Calls stay disabled until the connection test. Saving or logging in will not call anyone.", wraplength=740).grid(row=10, column=0, columnspan=2, sticky="w", pady=20)

    def save(self):
        try:
            self.config = Config.load(self.path if self.path.exists() else None)
            api_id = int(self.values["api_id"].get().strip() or "0")
            owner = self.values["owner"].get().strip()
            owner_id = int(owner) if owner.isdecimal() else self.config.owner_id
            if api_id < 0 or owner_id < 0:
                raise ValueError()
            vault = Vault(self.root / "secrets")
            for field, key in (("api_hash", "telegram_api_hash"), ("gemini", "gemini_api_key")):
                value = self.values[field].get().strip()
                if value:
                    vault.set(key, value)
                    self.values[field].set("")
            self.config = replace(self.config, telegram_api_id=api_id, owner_id=owner_id,
                                  free_cloud_confirmed=self.free.get())
            save_config(self.path, self.config)
            self.status.set("Settings saved locally. Log in the spare account to resolve a personal @username.")
            return True
        except Exception:
            self.status.set("Check the numeric API ID and account entries. Nothing was sent to chat.")
            return False

    def login(self):
        if not self.save():
            return
        owner = self.values["owner"].get().strip()
        self.login_button.configure(state="disabled")
        def work():
            try:
                asyncio.run(self.login_flow(owner))
            finally:
                self.events.put(lambda: self.login_button.configure(state="normal"))
        self.background(work, lambda _: self.status.set("Telegram connected. Open controls and restart JARVIS. Calls remain disabled."))

    async def login_flow(self, owner: str):
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        if not self.config.telegram_api_id or not owner:
            raise GateError("API ID and owner identity required")
        vault = Vault(self.root / "secrets")
        client = TelegramClient(StringSession(), self.config.telegram_api_id, vault.get("telegram_api_hash"))
        try:
            await client.start(
                phone=lambda: self.prompt("Spare Telegram account", "Enter the spare account's phone number, including country code:"),
                code_callback=lambda: self.prompt("Telegram login code", "Enter the code sent by Telegram:", secret=True),
                password=lambda: self.prompt("Telegram 2FA", "Enter the spare account's 2FA password:", secret=True))
            me = await client.get_me()
            if owner.isdecimal():
                owner_id = int(owner)
            else:
                entity = await client.get_entity(owner)
                if getattr(entity, "bot", True) or getattr(entity, "deleted", False):
                    raise GateError("Owner must be a real user")
                owner_id = entity.id
            if owner_id <= 0 or owner_id == me.id:
                raise GateError("Use different owner and spare accounts")
            peer = await client.get_input_entity(owner_id)
            (self.root / "telegram-owner.dpapi").write_bytes(_dpapi(
                json.dumps({"id": peer.user_id, "access_hash": peer.access_hash}).encode(), True))
            (self.root / "telegram.dpapi").write_bytes(_dpapi(client.session.save().encode(), True))
            # Reload to preserve speech paths written by installation while the UI was open.
            current = Config.load(self.path)
            self.config = replace(current, owner_id=owner_id)
            save_config(self.path, self.config)
        finally:
            await client.disconnect()


class ControlWindow(Window):
    def __init__(self, config_path: Path, root: Path):
        super().__init__("JARVIS — controls", config_path, root)
        self.store = Store(self.root / "state.sqlite3")
        self.broker = ApprovalBroker(self.store)
        t, tk = self.ttk, self.tk
        toolbar = t.Frame(self.frame)
        toolbar.pack(fill="x")
        for title, command in (("Refresh", self.refresh), ("Account setup", lambda: launch(self.path, self.root, "setup")),
                               ("Start / restart", self.restart), ("Call me", lambda: self.send("/call")),
                               ("Hang up", lambda: self.send("/hangup")), ("Stop", lambda: self.send("/stop"))):
            t.Button(toolbar, text=title, command=command).pack(side="left", padx=3)
        personal = t.Frame(self.frame)
        personal.pack(fill="x", pady=4)
        for title, command in (("Personal help", lambda: self.send("/help-personal")),
                               ("Daily briefing", lambda: self.send("/briefing")),
                               ("Activity history", lambda: self.send("/activity")),
                               ("Connect Google Calendar", self.connect_calendar)):
            t.Button(personal, text=title, command=command).pack(side="left", padx=3)
        self.output = tk.Text(self.frame, wrap="word", font=("Consolas", 10), state="disabled")
        self.output.pack(fill="both", expand=True, pady=12)
        self.command = tk.StringVar(value="/status")
        entry = t.Entry(self.frame, textvariable=self.command)
        entry.pack(fill="x")
        entry.bind("<Return>", lambda _: self.send(self.command.get()))
        t.Button(self.frame, text="Run owner command", command=lambda: self.send(self.command.get())).pack(anchor="e", pady=6)
        t.Label(self.frame, text="Use /help for tasks, chat monitoring and exact proposal approvals. Credentials belong in Account setup.").pack(anchor="w")
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()

    def show(self, text):
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("1.0", text)
        self.output.configure(state="disabled")

    def connect_calendar(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(parent=self.window, title="Select Google Desktop app OAuth client JSON", filetypes=[("Google OAuth client", "*.json")])
        if path:
            self.send("/calendar-connect " + path)

    def refresh(self):
        heartbeat = float(self.store.setting("heartbeat", "0"))
        data = {"service": self.store.setting("health", "not started"), "heartbeat_recent": time.time() - heartbeat < 15,
                "voice": self.store.setting("voice_mode", "none"),
                "tasks": self.store.rows("SELECT id,state,summary FROM tasks ORDER BY updated DESC LIMIT 15"),
                "pending_approvals": self.store.rows("SELECT id,task,digest,state FROM proposals WHERE state IN ('pending','uncertain')")}
        self.show(json.dumps(data, indent=2))

    def send(self, command):
        if time.time() - float(self.store.setting("heartbeat", "0")) > 15:
            self.status.set("JARVIS is not running. Use Start / restart.")
            return
        identifier = uuid.uuid4().hex
        with self.store.lock:
            self.store.db.execute("INSERT INTO inbox(id,command,created) VALUES(?,?,?)", (identifier, command, time.time()))
        def wait():
            deadline = time.monotonic() + 150
            while time.monotonic() < deadline:
                row = self.store.one("SELECT * FROM inbox WHERE id=?", (identifier,))
                if row["state"] in {"done", "uncertain"}:
                    return row["response"]
                time.sleep(.2)
            return "Still pending. Do not resubmit blindly. Command ID: " + identifier
        self.background(wait, self.show)

    def restart(self):
        def work():
            if time.time() - float(self.store.setting("heartbeat", "0")) < 15:
                identifier = uuid.uuid4().hex
                with self.store.lock:
                    self.store.db.execute("INSERT INTO inbox(id,command,created) VALUES(?,?,?)", (identifier, "/stop", time.time()))
                deadline = time.monotonic() + 15
                while self.store.setting("health") != "stopped" and time.monotonic() < deadline:
                    time.sleep(.25)
                if self.store.setting("health") != "stopped":
                    raise GateError("Service has not stopped")
                time.sleep(1)
            launch(self.path, self.root)
            return "JARVIS launched. Refresh status in a few seconds."
        self.background(work, self.show)

    def close(self):
        # Worker threads may still be waiting for a command; leave the process-owned
        # SQLite connection for interpreter teardown rather than closing under them.
        self.window.destroy()
