#!/usr/bin/env python3
"""Deterministic workspace-version handling; never resolve external dependencies."""

import json
from pathlib import Path
import re
import sys
import tomllib


def normalize_workspace_lock(manifest, lock):
    version = tomllib.loads(manifest)["workspace"]["package"]["version"]
    if not isinstance(version, str) or not re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", version
    ):
        raise ValueError("Invalid workspace version")
    sections = lock.split("[[package]]")
    for index in range(1, len(sections)):
        package = tomllib.loads("[[package]]" + sections[index])["package"][0]
        if "source" not in package and package["version"] != version:
            sections[index], replaced = re.subn(
                r'(?m)^version = "[^"\n]+"$',
                'version = "' + version + '"',
                sections[index],
                count=1,
            )
            if replaced != 1:
                raise ValueError("Cannot safely normalize the workspace lockfile entry")
    return "[[package]]".join(sections)


def normalize_release_lock(manifest, lock, tag):
    if not re.fullmatch(r"rust-v\d+\.\d+\.\d+", tag):
        raise ValueError("Expected an exact stable CLI release tag")
    if tomllib.loads(manifest)["workspace"]["package"]["version"] != tag[6:]:
        raise ValueError(
            "Workspace version does not match the recorded upstream release"
        )
    return normalize_workspace_lock(manifest, lock)


def resolve_workspace_version_conflict(manifest, *, current_version, upstream_version):
    section = re.search(
        r"(?ms)^\[workspace\.package\][ \t]*\n(.*?)(?=^\[|\Z)", manifest
    )
    if not section:
        return None
    conflicts = list(
        re.finditer(
            r"(?ms)^<<<<<<< [^\n]*\n(.*?)^=======\n(.*?)^>>>>>>> [^\n]*(?:\n|\Z)",
            section[1],
        )
    )
    if len(conflicts) != 1:
        return None
    conflict = conflicts[0]
    ours = re.split(r"(?m)^\|{7} [^\n]*\n", conflict[1])
    try:
        if tomllib.loads(ours[0]) != {"version": current_version}:
            return None
        if len(ours) > 1 and set(tomllib.loads(ours[1])) != {"version"}:
            return None
        if tomllib.loads(conflict[2]) != {"version": upstream_version}:
            return None
        start, end = (
            section.start(1) + conflict.start(),
            section.start(1) + conflict.end(),
        )
        resolved = manifest[:start] + conflict[2] + manifest[end:]
        parsed = tomllib.loads(resolved)
        if parsed["workspace"]["package"]["version"] != upstream_version:
            return None
        return resolved
    except (ValueError, KeyError):
        return None


if __name__ == "__main__":
    root = Path(sys.argv[1])
    identity = json.loads((root / "identity.json").read_text())
    manifest = (root / "codex-rs/Cargo.toml").read_text()
    lock = (root / "codex-rs/Cargo.lock").read_text()
    if normalize_release_lock(manifest, lock, identity["tag"]) != lock:
        raise SystemExit(
            "Workspace lockfile versions are stale; refuse the build before tool setup"
        )
    print("Workspace lockfile versions match", identity["tag"])
