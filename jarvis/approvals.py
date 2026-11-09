from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath

from .security import GateError
from .store import Store


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(data) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def safe_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise GateError("Use a nonempty relative path with forward slashes")
    p = PurePosixPath(relative)
    if p.is_absolute() or any(part in ("", ".", "..") for part in relative.split("/")):
        raise GateError("Path traversal is forbidden")
    for part in p.parts:
        if (re.search(r'[<>:"|?*\x00-\x1f]', part) or part.endswith((".", " "))
                or part.lower() in {".git", ".hg", ".svn", ".env"}
                or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?", part)):
            raise GateError("Reserved or sensitive path")
    root = root.resolve(strict=True)
    path = root.joinpath(*p.parts)
    for ancestor in [path, *path.parents]:
        if ancestor == root:
            break
        if ancestor.is_symlink() or (hasattr(ancestor, "is_junction") and ancestor.is_junction()):
            raise GateError("Links and junctions are forbidden in approval paths")
    if not path.resolve().is_relative_to(root):
        raise GateError("Path leaves project")
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise GateError("Target must be a regular, non-hardlinked file")
    return path


class ApprovalBroker:
    """Only the trusted owner controls this broker; voice models never receive it."""
    def __init__(self, store: Store):
        self.store = store

    def propose(self, task: str, changes: list[dict], ttl: int = 3600) -> str:
        root = Path(self.store.task(task)["project"])
        if not changes or len(changes) > 100 or not 1 <= ttl <= 86400:
            raise GateError("Invalid proposal size or lifetime")
        result, seen = [], set()
        for change in changes:
            if set(change) != {"path", "before_sha256", "content"}:
                raise GateError("Each change needs path, before_sha256, content")
            path = safe_path(root, change["path"])
            key = str(path).casefold()
            if key in seen:
                raise GateError("Duplicate target")
            seen.add(key)
            if not isinstance(change["content"], str) or len(change["content"].encode()) > 1024 * 1024:
                raise GateError("Changes must be UTF-8 text of at most 1 MiB")
            actual = sha(path.read_bytes()) if path.exists() else None
            if actual != change["before_sha256"]:
                raise GateError("Proposal is based on stale file contents")
            result.append(dict(change))
        payload = canonical({"project": str(root.resolve()), "changes": result})
        identifier = uuid.uuid4().hex[:16]
        with self.store.lock:
            self.store.db.execute("INSERT INTO proposals VALUES(?,?,?,?,?,?,?)",
                                  (identifier, task, payload, sha(payload.encode()), "pending", time.time() + ttl, time.time()))
        self.store.transition(task, "awaiting_approval", "A change proposal is ready for review")
        return identifier

    def get(self, identifier: str) -> dict:
        return self.store.one("SELECT * FROM proposals WHERE id=?", (identifier,))

    def preview(self, identifier: str) -> str:
        proposal = self.get(identifier)
        payload = json.loads(proposal["payload"])
        parts = [f"Proposal {identifier}\nSHA256: {proposal['digest']}\nState: {proposal['state']}\n"]
        for change in payload["changes"]:
            path = safe_path(Path(payload["project"]), change["path"])
            before = path.read_text("utf-8").splitlines(keepends=True) if path.exists() else []
            parts.extend(difflib.unified_diff(before, change["content"].splitlines(keepends=True),
                                            fromfile="a/" + change["path"], tofile="b/" + change["path"]))
        return "\n".join(parts)

    def reject(self, identifier: str):
        with self.store.lock:
            changed = self.store.db.execute("UPDATE proposals SET state='rejected' WHERE id=? AND state='pending'", (identifier,)).rowcount
        if changed != 1:
            raise GateError("Proposal is no longer pending")
        self.store.event(self.get(identifier)["task"], "proposal_rejected", {"id": identifier})
        task = self.get(identifier)["task"]
        with self.store.lock:
            self.store.db.execute("UPDATE blockers SET resolved=1 WHERE task=?", (task,))
        self.store.transition(task, "needs_input", "Owner rejected the proposal; provide corrections before resuming")

    def approve_and_apply(self, identifier: str, digest: str):
        with self.store.transaction() as db:
            proposal = dict(db.execute("SELECT * FROM proposals WHERE id=?", (identifier,)).fetchone() or {})
            if not proposal or proposal["state"] != "pending":
                raise GateError("Proposal is missing or no longer pending")
            if self.store.task(proposal["task"])["state"] == "cancelled":
                raise GateError("Task was cancelled; proposal cannot be applied")
            if proposal["expires"] <= time.time():
                raise GateError("Proposal expired")
            if digest != proposal["digest"] or sha(proposal["payload"].encode()) != digest:
                raise GateError("Approval digest does not match proposal")
            payload = json.loads(proposal["payload"])
            if payload["project"] != self.store.task(proposal["task"])["project"]:
                raise GateError("Proposal project mismatch")
            for change in payload["changes"]:
                self._validate(payload, change)
            db.execute("UPDATE proposals SET state='applying' WHERE id=?", (identifier,))
        applied = []
        try:
            for change in payload["changes"]:
                path = self._validate(payload, change)
                path.parent.mkdir(parents=True, exist_ok=True)
                # Check parents again after creation; refuse redirected paths.
                path = self._validate(payload, change)
                fd, tmp = tempfile.mkstemp(prefix=".jarvis-", dir=path.parent)
                try:
                    with os.fdopen(fd, "wb") as f:
                        f.write(change["content"].encode("utf-8"))
                        f.flush()
                        os.fsync(f.fileno())
                    self._validate(payload, change)
                    os.replace(tmp, path)
                finally:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
                if sha(path.read_bytes()) != sha(change["content"].encode()):
                    raise GateError("Readback failed")
                applied.append(change["path"])
        except BaseException:
            with self.store.lock:
                self.store.db.execute("UPDATE proposals SET state='uncertain' WHERE id=?", (identifier,))
            self.store.event(proposal["task"], "apply_uncertain", {"proposal": identifier, "applied": applied})
            raise
        with self.store.lock:
            self.store.db.execute("UPDATE proposals SET state='applied' WHERE id=?", (identifier,))
            self.store.db.execute("UPDATE blockers SET resolved=1 WHERE task=?", (proposal["task"],))
        self.store.event(proposal["task"], "patch_applied", {"proposal": identifier, "paths": applied})
        self.store.transition(proposal["task"], "needs_input", "Approved changes applied and read back; verification still required")

    @staticmethod
    def _validate(payload: dict, change: dict) -> Path:
        path = safe_path(Path(payload["project"]), change["path"])
        current = sha(path.read_bytes()) if path.exists() else None
        if current != change["before_sha256"]:
            raise GateError("File changed since proposal; create a new proposal")
        return path

