"""Bounded local document discovery and extraction in owner-selected roots."""
from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from .security import GateError

EXTENSIONS = {".txt", ".md", ".pdf", ".docx", ".csv"}
SKIP = {".git", ".venv", "node_modules", ".runtime", ".codex", "AppData", "secrets"}


class Documents:
    def __init__(self, store):
        self.store = store

    def roots(self):
        value = self.store.setting("document_roots")
        return [Path(p) for p in json.loads(value)] if value else [Path.home() / "Documents"]

    def add_root(self, path):
        root = Path(path).expanduser().resolve(strict=True)
        if not root.is_dir() or root == Path(root.anchor) or root == Path.home():
            raise GateError("Choose a specific document folder")
        roots = list(dict.fromkeys([str(root)] + [str(p) for p in self.roots()]))
        self.store.set_setting("document_roots", json.dumps(roots))
        return {"document_roots": roots}

    def check(self, value):
        original = Path(value).expanduser()
        if not original.is_absolute():
            raise GateError("Use an absolute document path")
        path = original.resolve(strict=True)
        matching = next((root.resolve() for root in self.roots() if root.is_dir() and path.is_relative_to(root.resolve())), None)
        if matching is None:
            raise GateError("The document is outside your selected document folders")
        if any(p.is_symlink() or p.is_junction() for p in [original, *original.parents]):
            raise GateError("Document links and junctions are not supported")
        if path.suffix.lower() not in EXTENSIONS or any(p.startswith(".") or p.casefold() in {x.casefold() for x in SKIP} for p in path.relative_to(matching).parts):
            raise GateError("Unsupported or protected document")
        if any(word in path.name.lower() for word in ("credential", "secret", "password", "token")):
            raise GateError("Credential files are excluded from document tools")
        if not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
            raise GateError("Document must be a file of at most 10 MiB")
        return path

    def find(self, query):
        query = query.strip().casefold()
        if len(query) < 2:
            raise GateError("Use at least two characters to find documents")
        matches, scanned = [], 0
        for root in self.roots():
            for folder, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = [d for d in dirs if d not in SKIP and not d.startswith(".") and not (Path(folder) / d).is_junction()]
                for name in files:
                    scanned += 1
                    if query in name.casefold() and Path(name).suffix.lower() in EXTENSIONS:
                        try:
                            path = self.check(str(Path(folder) / name))
                            matches.append({"path": str(path), "bytes": path.stat().st_size})
                        except (GateError, OSError):
                            pass
                    if scanned >= 10000 or len(matches) >= 30:
                        return {"documents": matches, "limited": True}
        return {"documents": matches, "limited": False}

    def read(self, value):
        path = self.check(value)
        if path.suffix.lower() == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(path)
            if reader.is_encrypted:
                raise GateError("Unlock the PDF locally before summarizing it")
            pieces, length = [], 0
            for index, page in enumerate(reader.pages[:100]):
                content = page.extract_text() or ""
                pieces.append(f"[Page {index + 1}]\n" + content)
                length += len(content)
                if length > 60000:
                    break
            body = "\n\n".join(pieces)
        elif path.suffix.lower() == ".docx":
            with zipfile.ZipFile(path) as archive:
                entry = archive.getinfo("word/document.xml")
                if entry.file_size > 4 * 1024 * 1024:
                    raise GateError("Word document content is too large")
                xml = ElementTree.fromstring(archive.read(entry))
                body = "\n".join("".join(p.itertext()) for p in xml.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"))
        else:
            body = path.read_text("utf-8-sig", errors="replace")
        return {"source": str(path), "text": body[:60000], "truncated": len(body) > 60000,
                "note": "Extracted text only; scanned pages require OCR and are not silently treated as empty summaries"}
