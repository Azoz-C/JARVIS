"""Conservatively resolve Win64/Python 3.12 artifacts using PyPI metadata only.

Never installs, imports, builds, or executes downloaded packages. PyAES 1.6.1 is
the sole source-distribution exception. Conflicts fail rather than guess.
"""
from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path

from pip._vendor.packaging.markers import default_environment
from pip._vendor.packaging.requirements import Requirement
from pip._vendor.packaging.specifiers import SpecifierSet
from pip._vendor.packaging.tags import compatible_tags, cpython_tags
from pip._vendor.packaging.utils import canonicalize_name, parse_wheel_filename
from pip._vendor.packaging.version import Version, InvalidVersion

TARGET = default_environment() | {
    "python_version": "3.12", "python_full_version": "3.12.0", "implementation_version": "3.12.0",
    "os_name": "nt", "sys_platform": "win32", "platform_system": "Windows",
    "platform_machine": "AMD64", "implementation_name": "cpython", "extra": "",
}
TAGS = list(cpython_tags((3, 12), ["cp312"], ["win_amd64"])) + list(compatible_tags((3, 12), "cp312", ["win_amd64"]))
TAG_RANK = {tag: i for i, tag in reversed(list(enumerate(TAGS)))}
CACHE = {}


def metadata(name, version=None):
    key = (name, version)
    if key not in CACHE:
        suffix = "/" + version if version else ""
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}{suffix}/json", timeout=30) as response:
            CACHE[key] = json.load(response)
    return CACHE[key]


def choose_artifact(name, version, files):
    candidates = []
    for file in files:
        if file.get("yanked") or not SpecifierSet(file.get("requires_python") or "").contains("3.12.0"):
            continue
        if file["filename"].endswith(".whl"):
            _, _, _, tags = parse_wheel_filename(file["filename"])
            ranks = [TAG_RANK[tag] for tag in tags if tag in TAG_RANK]
            if ranks:
                candidates.append((min(ranks), file["filename"], file))
        elif name == "pyaes" and version == "1.6.1" and file["filename"].endswith(".tar.gz"):
            candidates.append((100000, file["filename"], file))
    return min(candidates, key=lambda row: row[:2])[2] if candidates else None


def resolve(roots):
    selected = {}
    for _ in range(40):
        constraints, extras, visited = {}, {}, set()
        pending = list(roots)
        while pending:
            requirement = pending.pop()
            if requirement.url:
                raise RuntimeError("Direct URL requirements are not allowed")
            name = canonicalize_name(requirement.name)
            constraints.setdefault(name, set()).add(str(requirement.specifier))
            extras.setdefault(name, set()).update(requirement.extras)
            if name in selected:
                info, artifact = selected[name]
                marker_extras = extras[name] | {""}
                key = (name, tuple(sorted(marker_extras)))
                if key in visited:
                    continue
                visited.add(key)
                for value in info.get("requires_dist") or []:
                    child = Requirement(value)
                    if not child.marker or any(child.marker.evaluate(TARGET | {"extra": extra}) for extra in marker_extras):
                        pending.append(child)
        next_selected = {}
        for name, specs in sorted(constraints.items()):
            combined = SpecifierSet(",".join(s for s in sorted(specs) if s))
            package = metadata(name)
            versions = []
            for version in package["releases"]:
                try:
                    parsed = Version(version)
                except InvalidVersion:
                    continue
                if not parsed.is_prerelease and not parsed.is_devrelease and combined.contains(parsed):
                    versions.append(parsed)
            for version in sorted(versions, reverse=True):
                artifact = choose_artifact(name, str(version), package["releases"][str(version)])
                if artifact:
                    info = metadata(name, str(version))["info"]
                    if SpecifierSet(info.get("requires_python") or "").contains("3.12.0"):
                        next_selected[name] = (info, artifact)
                        break
            if name not in next_selected:
                raise RuntimeError(f"No approved Win64/Python 3.12 artifact satisfies {name}{combined}")
        fingerprint = lambda selection: {name: info["version"] for name, (info, _) in selection.items()}
        if fingerprint(next_selected) == fingerprint(selected):
            return next_selected
        selected = next_selected
    raise RuntimeError("Resolution did not converge; review dependency constraints")


def write_lock(path, roots):
    selected = resolve(roots)
    lines = ["# Windows x64 / CPython 3.12. Generated from PyPI metadata.",
             "# No packages were installed or executed during resolution."]
    for name, (info, artifact) in sorted(selected.items()):
        lines.extend(["# " + artifact["filename"],
                      f"{name}=={info['version']} --hash=sha256:{artifact['digests']['sha256']}"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("Wrote", path.name, "with", len(selected), "distributions")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--authorize-downloads", action="store_true", help="Allow public package-metadata requests")
    args = parser.parse_args()
    if not args.authorize_downloads:
        parser.error("Use --authorize-downloads to fetch public dependency metadata")
    root = Path(__file__).resolve().parent.parent
    roots = [Requirement(line) for line in (root / "requirements-live.txt").read_text().splitlines()
             if line.strip() and not line.startswith("#")]
    write_lock(root / "requirements-win-py312.lock", roots)
    write_lock(root / "requirements-build-win-py312.lock", [Requirement("setuptools"), Requirement("wheel")])


if __name__ == "__main__":
    main()
