#!/usr/bin/env python3
"""Check compiled gh-aw consistency and the fork's trusted workflow boundary."""

from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def main():
    tools = Path(sys.argv[1]).resolve()
    paths = [
        ROOT / ".github/workflows/upstream-maintainer.lock.yml",
        ROOT / ".github/workflows/upstream-repair.lock.yml",
        ROOT / ".github/aw/actions-lock.json",
    ]
    before = {p: p.read_bytes() for p in paths}
    subprocess.run(
        [
            str(tools / "gh-aw"),
            "compile",
            "upstream-maintainer",
            "upstream-repair",
            "--strict",
            "--no-check-update",
            "--schedule-seed",
            "drhelius/codex",
        ],
        cwd=ROOT,
        check=True,
    )
    for path, content in before.items():
        if path.read_bytes() != content:
            raise RuntimeError("Regenerate and commit " + str(path.relative_to(ROOT)))
    ordinary = [
        ROOT / ".github/workflows/fork-build.yml",
        ROOT / ".github/workflows/fork-coordinator.yml",
    ]
    subprocess.run(
        [str(tools / "actionlint"), "-shellcheck=", *map(str, ordinary)],
        cwd=ROOT,
        check=True,
    )
    for path in paths[:2]:
        text = path.read_text()
        for pattern in (
            '"compiler_version":"v0.87.4"',
            '"copilot":"1.0.80"',
            "copilot-requests: write",
            "COPILOT_GITHUB_TOKEN: ${{ github.token }}",
        ):
            if pattern not in text:
                raise RuntimeError(
                    "Compiled engine/permission contract missing: " + pattern
                )
        if (
            "!github.event.repository.fork" in text
            or "!github.event.workflow_run.repository.fork" in text
        ):
            raise RuntimeError("Unexpected fork guard in scheduled/dispatch workflow")
    allowed = {p.name for p in paths[:2]} | {p.name for p in ordinary}
    active = {
        p.name
        for p in (ROOT / ".github/workflows").iterdir()
        if p.suffix in (".yml", ".yaml")
    }
    if active != allowed:
        raise RuntimeError(
            "Inherited or missing active workflow: " + str(active ^ allowed)
        )
    for path in ordinary:
        text = path.read_text()
        if "pull_request_target:" in text:
            raise RuntimeError("Untrusted pull_request_target execution is forbidden")
        for ref in re.findall(r"(?m)^\s*(?:-\s+)?uses:\s+(\S+)", text):
            if not re.fullmatch(r"[\w/-]+@[0-9a-f]{40}", ref):
                raise RuntimeError("Unpinned action: " + ref)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            "fork/tests",
            "-p",
            "test_*.py",
            "-v",
        ],
        cwd=ROOT,
        check=True,
    )
    print("Compiled workflows, action references, fork boundary and fixtures passed")


if __name__ == "__main__":
    main()
