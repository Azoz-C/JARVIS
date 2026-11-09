"""Install hash-locked local speech assets inside this checkout only."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import stat
import sys
import urllib.request
import zipfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from jarvis.config import Config
from jarvis.desktop import save_config


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inside(relative):
    result = (ROOT / relative).resolve()
    if not result.is_relative_to(ROOT / ".runtime"):
        raise ValueError("Asset destination must stay inside .runtime")
    return result


def install():
    for asset in json.loads((ROOT / "runtime-assets.lock.json").read_text())["assets"]:
        path = inside(asset["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file() or digest(path) != asset["sha256"]:
            temporary = path.with_name(path.name + ".part")
            print("Downloading", path.name, flush=True)
            with urllib.request.urlopen(asset["url"], timeout=60) as response, temporary.open("wb") as output:
                shutil.copyfileobj(response, output, 1024 * 1024)
            if digest(temporary) != asset["sha256"]:
                temporary.unlink()
                raise ValueError("Asset hash mismatch: " + path.name)
            temporary.replace(path)
        if asset.get("extract_to"):
            destination = inside(asset["extract_to"])
            destination.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(path) as archive:
                for member in archive.infolist():
                    target = (destination / member.filename).resolve()
                    if not target.is_relative_to(destination) or stat.S_ISLNK(member.external_attr >> 16):
                        raise ValueError("Unsafe archive member")
                archive.extractall(destination)
        print("Verified", path.name, flush=True)
    binaries = list((ROOT / ".runtime" / "whisper").rglob("whisper-cli.exe"))
    if len(binaries) != 1:
        raise ValueError("Expected exactly one whisper-cli.exe")
    config_path = ROOT / "config.local.toml"
    config = Config.load(config_path if config_path.exists() else None)
    piper = ROOT / ".venv" / "Scripts" / "piper.exe"
    config = replace(config, whisper_bin=str(binaries[0]),
                     whisper_model=str(ROOT / ".runtime" / "models" / "ggml-base.en-q5_1.bin"),
                     piper_bin=str(piper), piper_model=str(ROOT / ".runtime" / "models" / "en_US-lessac-medium.onnx"))
    save_config(config_path, config)
    print("Configured local speech. Accounts and calling remain unchanged.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--authorize-downloads", action="store_true")
    if not parser.parse_args().authorize_downloads:
        parser.error("Use --authorize-downloads")
    install()
