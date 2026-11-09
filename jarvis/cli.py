from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib.metadata
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

from .approvals import ApprovalBroker
from .codex import CodexClient, resolve_executable
from .config import Config, state_home
from .platform import instance_lock, install_startup, startup_xml
from .security import GateError, Vault, _dpapi, private_directory
from .store import Store


def parser():
    p = argparse.ArgumentParser(description="JARVIS: Telegram calls and approval-gated Codex work")
    p.add_argument("--config", type=Path, default=Path("config.local.toml"))
    p.add_argument("--state", type=Path, default=state_home())
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("doctor", help="Offline dependency and configuration check")
    sub.add_parser("setup", help="Local secure credential and Telegram login window")
    sub.add_parser("control", help="Local status and owner-command window")
    serve = sub.add_parser("serve"); serve.add_argument("--tray", action="store_true")
    send = sub.add_parser("send", help="Send an owner command to the running local service")
    send.add_argument("command")
    send.add_argument("--wait", type=int, default=30)
    secret = sub.add_parser("secret"); secret.add_argument("operation", choices=["set"]); secret.add_argument("name", choices=sorted(Vault.NAMES))
    login = sub.add_parser("login-telegram"); login.add_argument("--authorize-login", action="store_true")
    probe = sub.add_parser("probe-codex", help="Read-only protocol probe; no model turn")
    probe.add_argument("--authorize-local-server", action="store_true")
    sub.add_parser("tasks")
    task = sub.add_parser("task"); task.add_argument("project", type=Path); task.add_argument("prompt")
    propose = sub.add_parser("propose"); propose.add_argument("task"); propose.add_argument("changes", type=Path)
    preview = sub.add_parser("preview"); preview.add_argument("proposal")
    approve = sub.add_parser("approve"); approve.add_argument("proposal"); approve.add_argument("digest")
    reject = sub.add_parser("reject"); reject.add_argument("proposal")
    startup = sub.add_parser("startup"); startup.add_argument("operation", choices=["preview", "install", "remove"])
    startup.add_argument("--authorize-startup-change", action="store_true")
    purge = sub.add_parser("purge"); purge.add_argument("--all-history", action="store_true")
    return p


def doctor(config: Config, root: Path):
    packages = {}
    for name in ("Telethon", "py-tgcalls", "ntgcalls", "websockets", "pystray", "Pillow"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {"python": sys.version.split()[0], "windows": os.name == "nt",
            "dependencies": packages, "codex": resolve_executable(config.codex_bin),
            "local_speech": {"whisper": shutil.which(config.whisper_bin), "piper": shutil.which(config.piper_bin),
                             "whisper_model": bool(config.whisper_model and Path(config.whisper_model).is_file()),
                             "piper_model": bool(config.piper_model and Path(config.piper_model).is_file())},
            "configured_owner": bool(config.owner_id), "calls_enabled": config.calls_enabled,
            "automatic_calls": config.automatic_calls, "free_cloud_confirmed": config.free_cloud_confirmed,
            "telegram_session_present": (root / "telegram.dpapi").is_file(),
            "state": str(root), "live_call_verified": False,
            "note": "Offline inspection only. Installed packages do not prove a working call or free API access."}


async def login(config: Config, root: Path):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    if not config.owner_id or not config.telegram_api_id:
        raise GateError("Configure numeric owner_id and telegram_api_id first")
    api_hash = Vault(root / "secrets").get("telegram_api_hash")
    client = TelegramClient(StringSession(), config.telegram_api_id, api_hash)
    try:
        await client.start(phone=lambda: input("Spare Telegram phone number: "),
                           password=lambda: getpass.getpass("Telegram 2FA password: "),
                           code_callback=lambda: getpass.getpass("Telegram login code: "))
        me = await client.get_me()
        if me.id == config.owner_id:
            raise GateError("This is the owner account; authenticate the spare account instead")
        (root / "telegram.dpapi").write_bytes(_dpapi(client.session.save().encode(), True))
        print("Spare Telegram session stored with Windows DPAPI. No call placed.")
    finally:
        await client.disconnect()


async def probe(config: Config):
    client = CodexClient(config.codex_bin)
    try:
        await client.start()
        account = await client.request("account/read", {"refreshToken": False})
        threads = await client.list_threads()
        print(json.dumps({"protocol": "connected", "account_type": (account.get("account") or {}).get("type"),
                          "threads_visible": len(threads.get("data", [])),
                          "has_more": bool(threads.get("nextCursor")), "model_turns_started": 0}))
    finally:
        await client.close()


async def serve(config: Config, args, store: Store):
    from .service import Service
    service = Service(config, args.config, args.state, store)
    tray = None
    with instance_lock(args.state):
        try:
            if args.tray:
                from .platform import Tray
                tray = Tray(asyncio.get_running_loop(), service)
                tray.start()
            await service.start()
            await service.stop.wait()
        finally:
            await service.close()
            if tray:
                tray.stop()


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        config = Config.load(args.config if args.config.is_file() else None)
        if args.action == "doctor":
            print(json.dumps(doctor(config, args.state), indent=2))
            return 0
        if args.action in {"setup", "control"}:
            from .desktop import SetupWindow, ControlWindow
            (SetupWindow if args.action == "setup" else ControlWindow)(args.config, args.state).run()
            return 0
        if args.action == "probe-codex":
            if not args.authorize_local_server:
                raise GateError("Use --authorize-local-server for the read-only protocol probe")
            asyncio.run(probe(config))
            return 0
        if args.action == "startup" and args.operation == "preview":
            print(startup_xml(args.config, args.state))
            return 0
        if not args.config.is_file() and args.action in {"serve", "login-telegram", "startup"}:
            raise GateError("Create and review config.local.toml first")
        private_directory(args.state)
        if args.action == "secret":
            Vault(args.state / "secrets").set(args.name, getpass.getpass("Secret (hidden): "))
            print("Stored using Windows DPAPI.")
            return 0
        if args.action == "login-telegram":
            if not args.authorize_login:
                raise GateError("Use --authorize-login to contact Telegram and authenticate")
            asyncio.run(login(config, args.state))
            return 0
        if args.action == "startup":
            if not args.authorize_startup_change:
                raise GateError("Use --authorize-startup-change after reviewing startup preview")
            if args.operation == "install":
                install_startup(args.config, args.state)
            else:
                import subprocess
                subprocess.run(["schtasks", "/Delete", "/TN", "JARVIS", "/F"], check=True, capture_output=True)
            print("Startup registration updated.")
            return 0
        store = Store(args.state / "state.sqlite3")
        broker = ApprovalBroker(store)
        try:
            if args.action == "serve":
                asyncio.run(serve(config, args, store))
            elif args.action == "task":
                print(store.create_task(args.project, args.prompt))
            elif args.action == "tasks":
                print(json.dumps(store.rows("SELECT id,project,state,summary FROM tasks ORDER BY updated DESC"), indent=2))
            elif args.action == "propose":
                print(broker.propose(args.task, json.loads(args.changes.read_text("utf-8"))))
            elif args.action == "preview":
                print(broker.preview(args.proposal))
            elif args.action == "approve":
                broker.approve_and_apply(args.proposal, args.digest)
                print("Approved changes applied and read back. Tests not run.")
            elif args.action == "reject":
                broker.reject(args.proposal)
                print("Rejected.")
            elif args.action == "send":
                heartbeat = float(store.setting("heartbeat", "0"))
                if time.time() - heartbeat > 15:
                    raise GateError("Service heartbeat is stale; start JARVIS first")
                identifier = uuid.uuid4().hex
                with store.lock:
                    store.db.execute("INSERT INTO inbox(id,command,created) VALUES(?,?,?)", (identifier, args.command, time.time()))
                deadline = time.monotonic() + args.wait
                while time.monotonic() < deadline:
                    row = store.one("SELECT * FROM inbox WHERE id=?", (identifier,))
                    if row["state"] in {"done", "uncertain"}:
                        print(row["response"])
                        return 0 if row["state"] == "done" else 1
                    time.sleep(.2)
                print("Command submitted; do not resubmit blindly. ID: " + identifier)
                return 1
            elif args.action == "purge":
                if args.all_history:
                    if store.rows("SELECT id FROM tasks WHERE state IN ('running','verifying')"):
                        raise GateError("Stop active tasks before purging history")
                    with store.transaction() as db:
                        for table in ("events", "proposals", "command_proposals", "blockers", "tasks", "calls", "commands", "inbox"):
                            db.execute("DELETE FROM " + table)
                else:
                    store.purge()
                print("History purged. Credentials preserved.")
        finally:
            store.close()
        return 0
    except KeyboardInterrupt:
        return 130
    except (GateError, ValueError, OSError, ImportError) as error:
        print(f"JARVIS: {error}", file=sys.stderr)
        return 1
