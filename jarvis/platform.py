from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from xml.sax.saxutils import escape

from .security import GateError


@contextmanager
def instance_lock(root: Path):
    path = root / "service.lock"
    file = path.open("a+b")
    try:
        file.seek(0)
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                file.write(b"0"); file.flush(); file.seek(0)
            try:
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise GateError("JARVIS is already running") from None
        else:
            import fcntl
            try:
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise GateError("JARVIS is already running") from None
        yield
    finally:
        file.close()


def startup_xml(config: Path, root: Path) -> str:
    executable = Path(sys.executable)
    pythonw = executable.with_name("pythonw.exe")
    if pythonw.is_file():
        executable = pythonw
    args = subprocess.list2cmdline(["-B", "-m", "jarvis", "--config", str(config.resolve()),
                                    "--state", str(root.resolve()), "serve", "--tray"])
    cwd = Path(__file__).resolve().parent.parent
    identity = ""
    if os.name == "nt":
        result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True)
        match = re.search(r"S-1-[0-9-]+", result.stdout)
        if not match:
            raise GateError("Cannot identify startup task owner")
        identity = match.group()
    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
 <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(identity)}</UserId></LogonTrigger></Triggers>
 <Principals><Principal id="Author"><UserId>{escape(identity)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
 <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><WakeToRun>false</WakeToRun></Settings>
 <Actions Context="Author"><Exec><Command>{escape(str(executable))}</Command><Arguments>{escape(args)}</Arguments><WorkingDirectory>{escape(str(cwd))}</WorkingDirectory></Exec></Actions>
</Task>'''


def install_startup(config: Path, root: Path):
    if os.name != "nt":
        raise GateError("Startup registration requires Windows")
    with tempfile.TemporaryDirectory(dir=root) as directory:
        path = Path(directory) / "startup.xml"
        path.write_text(startup_xml(config, root), encoding="utf-16")
        result = subprocess.run(["schtasks", "/Create", "/TN", "JARVIS", "/XML", str(path), "/F"],
                                capture_output=True, text=True, creationflags=0x08000000)
        if result.returncode:
            raise GateError("Startup task registration failed: " + result.stderr.strip())


class Tray:
    def __init__(self, loop, service):
        import pystray
        from PIL import Image, ImageDraw
        image = Image.new("RGB", (64, 64), "#101c30")
        draw = ImageDraw.Draw(image)
        draw.ellipse((12, 12, 52, 52), outline="#44d4ff", width=5)
        self.loop, self.service = loop, service

        def submit(command):
            def handler(*_):
                asyncio.run_coroutine_threadsafe(service.owner_command(command, "tray:" + str(__import__('uuid').uuid4())), loop)
            return handler

        import asyncio
        self.icon = pystray.Icon("jarvis", image, "JARVIS — edits require approval", menu=pystray.Menu(
            pystray.MenuItem("Open controls", lambda *_: __import__("jarvis.desktop", fromlist=["launch"]).launch(service.config_path, service.root, "control")),
            pystray.MenuItem("Account setup", lambda *_: __import__("jarvis.desktop", fromlist=["launch"]).launch(service.config_path, service.root, "setup")),
            pystray.MenuItem("Send status to Telegram", submit("/status")),
            pystray.MenuItem("Call me", submit("/call")),
            pystray.MenuItem("Hang up", submit("/hangup")),
            pystray.MenuItem("Quiet mode on", submit("/quiet on")),
            pystray.MenuItem("Quiet mode off", submit("/quiet off")),
            pystray.MenuItem("Stop JARVIS", submit("/stop")),
        ))
        service.desktop_notify = lambda message: self.icon.notify(message[:1500], "JARVIS")

    def start(self):
        self.icon.run_detached()

    def stop(self):
        self.icon.stop()

