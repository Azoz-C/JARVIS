"""Supported Codex deep links; no simulated keyboard input or automatic Send."""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlencode

from .security import GateError


def chat_link(thread: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", thread):
        raise GateError("Invalid Codex chat identifier")
    return "codex://threads/" + thread


def draft_link(project: Path, prompt: str) -> str:
    project = project.resolve(strict=True)
    if not project.is_dir() or not prompt.strip():
        raise GateError("A project directory and prompt are required")
    return "codex://threads/new?" + urlencode({"path": str(project), "prompt": prompt})


def open_link(link: str):
    if not link.startswith("codex://threads/") or any(c in link for c in "\r\n\0"):
        raise GateError("Invalid Codex frontend link")
    if os.name != "nt":
        raise GateError("Automatic frontend opening is configured for Windows only")
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"codex\shell\open\command"):
            pass
    except OSError:
        raise GateError("Codex deep-link handler is not registered; open the saved prompt in Codex manually") from None
    os.startfile(link)


def prepare_task(root: Path, task: dict) -> tuple[str, Path]:
    folder = root / "frontend"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (task["id"] + ".md")
    prompt = (f"JARVIS owner request {task['id']}\nProject: {task['project']}\n\n"
              + task["prompt"] + "\n\nInspect read-only first. Propose file edits for owner approval before applying them. "
              "Report exactly which checks were actually executed, with commands and exit codes. "
              "Keep static review findings separate from executed tests and unverified recommendations. "
              "Do not install packages, publish, or contact external services without explicit owner instructions.")
    path.write_text(prompt, encoding="utf-8")
    link = draft_link(Path(task["project"]), prompt)
    if len(link) > 24000:
        link = draft_link(Path(task["project"]), "Read the owner request in " + str(path) + " and carry it out subject to its constraints.")
    return link, path
