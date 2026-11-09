from __future__ import annotations

import ctypes
import os
import re
import subprocess
from ctypes import wintypes
from pathlib import Path


class GateError(RuntimeError):
    pass


def private_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        who = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                             capture_output=True, text=True, check=True)
        sid = re.search(r"S-1-[0-9-]+", who.stdout)
        if not sid:
            raise GateError("Cannot identify Windows user")
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r",
                        f"*{sid.group() }:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"],
                       capture_output=True, check=True)
    else:
        path.chmod(0o700)
    return path


class Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, encrypt: bool) -> bytes:
    if os.name != "nt":
        raise GateError("Credential storage requires Windows DPAPI")
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, out = Blob(len(data), buffer), Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if encrypt:
        ok = crypt.CryptProtectData(ctypes.byref(source), "JARVIS", None, None, None,
                                   1, ctypes.byref(out))
    else:
        ok = crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None,
                                     1, ctypes.byref(out))
    if not ok:
        raise GateError("Windows could not protect/unprotect credential")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel.LocalFree(out.pbData)


class Vault:
    NAMES = {"telegram_api_hash", "gemini_api_key", "google_calendar_tokens"}

    def __init__(self, root: Path):
        self.root = root

    def set(self, name: str, value: str) -> None:
        if name not in self.NAMES or not value.strip():
            raise GateError("Unknown or empty credential")
        root = private_directory(self.root)
        (root / (name + ".dpapi")).write_bytes(_dpapi(value.encode(), True))

    def get(self, name: str) -> str:
        if name not in self.NAMES:
            raise GateError("Unknown credential")
        path = self.root / (name + ".dpapi")
        if not path.is_file():
            raise GateError(f"Missing credential: {name}")
        return _dpapi(path.read_bytes(), False).decode()


def redact(text: str) -> str:
    text = re.sub(r"(?i)(api[_-]?key|api[_-]?hash|token|password|secret)\s*[:=]\s*[^\s,;]+",
                  r"\1=[REDACTED]", text)
    return re.sub(r"\b(?:AIza[\w-]{20,}|sk-[\w-]{12,})\b", "[REDACTED]", text)

