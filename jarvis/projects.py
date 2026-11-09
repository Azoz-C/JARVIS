"""Read-only project metadata adapter, independent of chat history pagination.

The installed local app-server has no project/list method. Prefer the desktop's
registered local-project metadata; fall back to configured roots if unavailable.
Never modify desktop state, trust configuration, authentication or Codex databases.
"""
from __future__ import annotations

import hashlib
import json
import os
import tomllib
from pathlib import Path


def project_key(path: Path, project_id: str | None = None) -> str:
    identity = str(path.resolve()).casefold()
    if project_id:
        identity = project_id + "\0" + identity
    return hashlib.sha256(identity.encode()).hexdigest()[:12]


class ProjectRegistry:
    def __init__(self, home: Path | None = None):
        self.home = home or Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        self._last_good = None

    def revision(self):
        result = []
        for filename in (".codex-global-state.json", "config.toml"):
            try:
                info = (self.home / filename).stat()
                result.append((filename, info.st_mtime_ns, info.st_size))
            except OSError:
                result.append((filename, None, None))
        return tuple(result)

    @staticmethod
    def entries(records):
        projects, seen = [], set()
        for record in records:
            roots = record.get("rootPaths", [])
            if not isinstance(roots, list):
                continue
            roots = [p for p in roots if isinstance(p, str) and Path(p).is_absolute()]
            for root in roots:
                path = Path(root).resolve()
                key = project_key(path, record.get("id") if isinstance(record.get("id"), str) else None)
                if key in seen:
                    continue
                seen.add(key)
                label = record.get("name")
                label = label.strip()[:200] if isinstance(label, str) and label.strip() else path.name
                name = label if len(roots) == 1 else label + " / " + path.name
                projects.append({"id": key, "name": name, "project_name": label, "path": str(path),
                                 "roots": roots, "available": path.is_dir(), "source": record["source"],
                                 "codex_project_id": record.get("id")})
        return projects

    def read(self):
        warnings, records, assignments = [], [], {}
        state_path = self.home / ".codex-global-state.json"
        try:
            if state_path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError("Project metadata exceeds its size limit")
            state = json.loads(state_path.read_text("utf-8"))
            local = state.get("local-projects")
            if not isinstance(local, dict):
                raise ValueError("Unsupported local project metadata shape")
            order = state.get("project-order", [])
            order = order if isinstance(order, list) else []
            keys = [k for k in order if isinstance(k, str) and k in local]
            keys += [k for k in local if k not in keys]
            for key in keys:
                record = local[key]
                if isinstance(record, dict):
                    records.append({**record, "id": key, "source": "codex_desktop"})
            mapping = state.get("thread-project-assignments", {})
            if isinstance(mapping, dict):
                assignments = {key: value["projectId"] for key, value in mapping.items()
                               if isinstance(key, str) and isinstance(value, dict)
                               and value.get("projectKind") == "local" and isinstance(value.get("projectId"), str)}
            result = {"projects": self.entries(records), "assignments": assignments,
                      "source": "codex_desktop", "registered_projects_available": True, "warnings": warnings}
            self._last_good = result
            return result
        except (OSError, ValueError, TypeError, AttributeError):
            warnings.append("Desktop project metadata is unavailable or has an unsupported format")
        if self._last_good:
            return {**self._last_good, "warnings": warnings + ["Using the last valid registered-project snapshot"], "stale": True}
        try:
            config_path = self.home / "config.toml"
            if config_path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("Configuration exceeds its size limit")
            config = tomllib.loads(config_path.read_text("utf-8"))
            records = [{"name": Path(path).name, "rootPaths": [path], "source": "codex_config"}
                       for path in config.get("projects", {}) if isinstance(path, str) and Path(path).is_absolute()]
        except (OSError, ValueError, TypeError):
            records = []
        return {"projects": self.entries(records), "assignments": {}, "source": "codex_config" if records else "chat_history",
                "registered_projects_available": False, "warnings": warnings}
