#!/usr/bin/env python3
"""Trusted, serialized release coordination. Never execute candidate code here."""

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

REPO = "drhelius/codex"
UPSTREAM = "openai/codex"
BRANCH = "fork-main"
STATE_BRANCH = "fork-state"
ROOT = Path(__file__).resolve().parents[2]
BOOT = json.loads((ROOT / "fork/bootstrap.json").read_text())
TARGETS = [
    "aarch64-apple-darwin",
    "x86_64-apple-darwin",
    "x86_64-unknown-linux-musl",
    "aarch64-unknown-linux-musl",
    "x86_64-pc-windows-msvc",
]
# These are owned by the fork, restored after merges and checked independently of candidate code.
BOUNDARY = [
    "fork",
    ".github",
    "AGENTS.md",
    "codex-rs/core/tests/suite/mcp_server_selection.rs",
    "codex-rs/tui/src/bottom_pane/mcp_selection_tests.rs",
    "codex-rs/tui/src/bottom_pane/snapshots/codex_tui__bottom_pane__mcp_selection__tests__mcp_selector_appearance.snap",
    "codex-rs/rmcp-client/src/bin/test_stdio_server.rs",
]
SHA = re.compile(r"[0-9a-f]{40}\Z")
TAG = re.compile(r"rust-v(\d+)\.(\d+)\.(\d+)\Z")


def require(value, message):
    if not value:
        raise RuntimeError(message)


def git(*args, data=None, check=True):
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        input=data,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    ).stdout.strip()


class API:
    def request(self, path, method="GET", data=None, binary=False):
        url = path if path.startswith("https://") else "https://api.github.com/" + path
        require(
            urllib.parse.urlparse(url).hostname
            in ("api.github.com", "uploads.github.com"),
            "Unexpected API host",
        )
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "codex-gear-maintenance",
        }
        if os.environ.get("GH_TOKEN"):
            headers["Authorization"] = "Bearer " + os.environ["GH_TOKEN"]
        if isinstance(data, bytes):
            headers["Content-Type"] = "application/octet-stream"
            body = data
        else:
            headers["Content-Type"] = "application/json"
            body = json.dumps(data).encode() if data is not None else None

        class Redirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                result = super().redirect_request(req, fp, code, msg, hdrs, newurl)
                if (
                    result
                    and urllib.parse.urlparse(newurl).hostname
                    != urllib.parse.urlparse(req.full_url).hostname
                ):
                    result.remove_header("Authorization")
                return result

        with urllib.request.build_opener(Redirect()).open(
            urllib.request.Request(url, body, headers, method=method), timeout=120
        ) as response:
            result = response.read()
        return result if binary else (json.loads(result) if result else None)

    def optional(self, path):
        try:
            return self.request(path)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise

    def pages(self, path, key=None):
        result = []
        for page in range(1, 1001):
            data = self.request(
                f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}"
            )
            rows = data[key] if key else data
            result.extend(rows)
            if len(rows) < 100:
                return result
        raise RuntimeError("Pagination limit reached; refusing incomplete discovery")


def eligible(releases):
    baseline = tuple(map(int, TAG.fullmatch(BOOT["upstream_tag"]).groups()))
    return sorted(
        (
            r
            for r in releases
            if not r["draft"]
            and not r["prerelease"]
            and TAG.fullmatch(r["tag_name"])
            and tuple(map(int, TAG.fullmatch(r["tag_name"]).groups())) >= baseline
            and r["published_at"] >= BOOT["published_at"]
        ),
        key=lambda r: (r["published_at"], r["id"]),
    )


def resolve_upstream_tag(api, tag):
    require(TAG.fullmatch(tag), "Unexpected upstream tag")
    obj = api.request(f"repos/{UPSTREAM}/git/ref/tags/{tag}")["object"]
    for _ in range(8):
        require(SHA.fullmatch(obj["sha"]), "Invalid upstream object ID")
        if obj["type"] == "commit":
            return obj["sha"]
        require(obj["type"] == "tag", "Upstream release ref is not a commit/tag")
        obj = api.request(f"repos/{UPSTREAM}/git/tags/{obj['sha']}")["object"]
    raise RuntimeError("Upstream annotated tag nesting exceeded")


def next_pending(state):
    return next(
        (r for r in state["releases"] if r["status"] not in ("published", "skipped")),
        None,
    )


def validate_record(record):
    require(isinstance(record["id"], int) and record["id"] > 0, "Invalid release ID")
    require(
        TAG.fullmatch(record["tag"]) and SHA.fullmatch(record["upstream_sha"]),
        "Invalid upstream identity",
    )
    require(
        record["branch"] == f"fork-candidates/{record['id']}-r{record['revision']}",
        "Invalid candidate branch",
    )
    for name in ("source_sha", "base_sha", "policy_sha"):
        if record.get(name):
            require(SHA.fullmatch(record[name]), f"Invalid {name}")


def normalize_workspace_lock(manifest, lock):
    version = tomllib.loads(manifest)["workspace"]["package"]["version"]
    require(
        re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", version),
        "Invalid workspace version",
    )
    sections = lock.split("[[package]]")
    for index in range(1, len(sections)):
        package = tomllib.loads("[[package]]" + sections[index])["package"][0]
        if "source" not in package:
            sections[index] = re.sub(
                r'(?m)^version = "[^"\n]+"$',
                'version = "' + version + '"',
                sections[index],
                count=1,
            )
    return "[[package]]".join(sections)


def release_tag(record):
    return f"mcp-{record['tag']}-r{record['revision']}"


class Coordinator:
    def __init__(self, api):
        self.api = api
        self.state = None
        self.blob = None

    def ref(self, branch=BRANCH):
        return self.api.request(f"repos/{REPO}/git/ref/heads/{branch}")["object"]["sha"]

    def load(self, create=False):
        response = self.api.optional(
            f"repos/{REPO}/contents/state.json?ref={STATE_BRANCH}"
        )
        if response:
            self.blob = response["sha"]
            self.state = json.loads(base64.b64decode(response["content"]))
        else:
            self.state = {"schema": 1, "repository": REPO, "releases": []}
            if create:
                tree = self.api.request(
                    f"repos/{REPO}/git/trees",
                    "POST",
                    {
                        "tree": [
                            {
                                "path": "state.json",
                                "mode": "100644",
                                "type": "blob",
                                "content": json.dumps(self.state, indent=2) + "\n",
                            }
                        ]
                    },
                )
                commit = self.api.request(
                    f"repos/{REPO}/git/commits",
                    "POST",
                    {
                        "message": "Initialize fork release ledger",
                        "tree": tree["sha"],
                        "parents": [],
                    },
                )
                self.api.request(
                    f"repos/{REPO}/git/refs",
                    "POST",
                    {"ref": "refs/heads/" + STATE_BRANCH, "sha": commit["sha"]},
                )
                self.load()
        require(
            self.state["schema"] == 1 and self.state["repository"] == REPO,
            "Unexpected maintenance state",
        )
        return self.state

    def save(self):
        result = self.api.request(
            f"repos/{REPO}/contents/state.json",
            "PUT",
            {
                "message": "Record fork release progress",
                "branch": STATE_BRANCH,
                "sha": self.blob,
                "content": base64.b64encode(
                    (json.dumps(self.state, indent=2) + "\n").encode()
                ).decode(),
            },
        )
        self.blob = result["content"]["sha"]

    def discover(self):
        known = {r["id"]: r for r in self.state["releases"]}
        for release in eligible(self.api.pages(f"repos/{UPSTREAM}/releases")):
            if release["id"] in known:
                require(
                    known[release["id"]]["tag"] == release["tag_name"],
                    "An upstream release tag changed",
                )
                continue
            sha = resolve_upstream_tag(self.api, release["tag_name"])
            self.state["releases"].append(
                {
                    "id": release["id"],
                    "tag": release["tag_name"],
                    "upstream_sha": sha,
                    "published_at": release["published_at"],
                    "revision": 1,
                    "branch": f"fork-candidates/{release['id']}-r1",
                    "status": "pending",
                    "attempts": 0,
                    "pr": None,
                }
            )
        self.state["releases"].sort(
            key=lambda r: (r["published_at"], r["id"], r["revision"])
        )

    def dispatch(self, workflow, inputs):
        self.api.request(
            f"repos/{REPO}/actions/workflows/{workflow}/dispatches",
            "POST",
            {"ref": BRANCH, "inputs": inputs},
        )

    def push(self, sha, branch):
        # No checkout of candidate code, hooks, force updates, or persistent credentials.
        env = dict(os.environ)
        env.update(
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
            GIT_CONFIG_VALUE_0="AUTHORIZATION: basic "
            + base64.b64encode(
                ("x-access-token:" + os.environ["GH_TOKEN"]).encode()
            ).decode(),
        )
        subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "push",
                "https://github.com/" + REPO + ".git",
                f"{sha}:refs/heads/{branch}",
            ],
            env=env,
            check=True,
        )

    def integrate(self, record):
        validate_record(record)
        release = self.api.request(f"repos/{UPSTREAM}/releases/{record['id']}")
        if (
            release["draft"]
            or release["prerelease"]
            or release["tag_name"] != record["tag"]
            or resolve_upstream_tag(self.api, record["tag"]) != record["upstream_sha"]
        ):
            record["status"] = "blocked"
            self.save()
            self.issue(
                record,
                "Upstream release identity changed after discovery; explicit owner review is required",
            )
            return
        base = self.ref()
        record.update(base_sha=base, policy_sha=base)
        git("fetch", "--no-tags", "origin", base)
        git(
            "fetch",
            "--no-tags",
            "https://github.com/" + UPSTREAM + ".git",
            record["upstream_sha"],
        )
        if record["id"] == BOOT["release_id"] and record["revision"] == 1:
            require(
                record["upstream_sha"] == BOOT["upstream_sha"], "Bootstrap tag moved"
            )
            require(
                subprocess.run(
                    ["git", "merge-base", "--is-ancestor", record["upstream_sha"], base]
                ).returncode
                == 0,
                "Bootstrap lost upstream ancestry",
            )
            source = base
            conflicts = []
        else:
            merged = subprocess.run(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "merge-tree",
                    "--write-tree",
                    base,
                    record["upstream_sha"],
                ],
                text=True,
                capture_output=True,
            )
            require(
                merged.returncode in (0, 1),
                "git merge-tree failed: " + merged.stderr[:1000],
            )
            tree = merged.stdout.splitlines()[0]
            require(SHA.fullmatch(tree), "Merge did not produce a tree")
            # Use a temporary index. Never check out or run upstream/candidate scripts in this job.
            with tempfile.TemporaryDirectory() as directory:
                env = dict(os.environ, GIT_INDEX_FILE=str(Path(directory) / "index"))
                subprocess.run(["git", "read-tree", tree], env=env, check=True)
                all_paths = git("ls-tree", "-r", "--name-only", tree).splitlines()
                owned = [
                    p
                    for p in all_paths
                    if any(p == b or p.startswith(b + "/") for b in BOUNDARY)
                ]
                if owned:
                    subprocess.run(
                        ["git", "update-index", "--force-remove", "--", *owned],
                        env=env,
                        check=True,
                    )
                entries = git("ls-tree", "-r", base, "--", *BOUNDARY)
                subprocess.run(
                    ["git", "update-index", "--index-info"],
                    input=entries + "\n",
                    text=True,
                    env=env,
                    check=True,
                )
                try:
                    manifest = git("show", tree + ":codex-rs/Cargo.toml")
                    lock = git("show", tree + ":codex-rs/Cargo.lock") + "\n"
                    normalized = normalize_workspace_lock(manifest, lock)
                    if normalized != lock:
                        blob = git("hash-object", "-w", "--stdin", data=normalized)
                        subprocess.run(
                            [
                                "git",
                                "update-index",
                                "--add",
                                "--cacheinfo",
                                "100644," + blob + ",codex-rs/Cargo.lock",
                            ],
                            env=env,
                            check=True,
                        )
                except (tomllib.TOMLDecodeError, KeyError):
                    # Real manifest conflicts remain visible and require owner review.
                    pass
                tree = subprocess.check_output(
                    ["git", "write-tree"], env=env, text=True
                ).strip()
            unmerged = [
                line.split("\t", 1)[1]
                for line in merged.stdout.splitlines()
                if re.match(r"[0-7]{6} [0-9a-f]{40} [123]\t", line)
            ]
            source_conflicts = [
                path
                for path in unmerged
                if not any(path == b or path.startswith(b + "/") for b in BOUNDARY)
            ]
            conflicts = (
                merged.stdout.splitlines()[1:]
                if merged.returncode and (source_conflicts or not unmerged)
                else []
            )
            env = dict(
                os.environ,
                GIT_AUTHOR_NAME="github-actions[bot]",
                GIT_AUTHOR_EMAIL="41898282+github-actions[bot]@users.noreply.github.com",
                GIT_COMMITTER_NAME="github-actions[bot]",
                GIT_COMMITTER_EMAIL="41898282+github-actions[bot]@users.noreply.github.com",
            )
            source = subprocess.check_output(
                [
                    "git",
                    "commit-tree",
                    tree,
                    "-p",
                    base,
                    "-p",
                    record["upstream_sha"],
                    "-m",
                    f"Integrate {record['tag']} with fork policy preserved",
                ],
                env=env,
                text=True,
            ).strip()
        existing = self.api.optional(f"repos/{REPO}/git/ref/heads/{record['branch']}")
        if existing:
            # Recovery after a successful push but before state was saved.
            source = existing["object"]["sha"]
            git("fetch", "--no-tags", "origin", source)
            for ancestor in (base, record["upstream_sha"]):
                require(
                    subprocess.run(
                        ["git", "merge-base", "--is-ancestor", ancestor, source]
                    ).returncode
                    == 0,
                    "Existing candidate has unexpected ancestry",
                )
        else:
            self.push(source, record["branch"])
        record.update(
            source_sha=source,
            integration_sha=source,
            conflicts="\n".join(conflicts)[:12000],
            status="integrated",
        )
        self.save()
        if conflicts:
            self.repair(
                record,
                "Mechanical integration has merge conflicts; inspect the candidate conflict markers",
            )
        else:
            self.build(record)

    def status(self, record, state, url=None):
        self.api.request(
            f"repos/{REPO}/statuses/{record['source_sha']}",
            "POST",
            {
                "state": state,
                "context": "fork/release-gates",
                "description": "Five-platform selector and distribution gates: "
                + state,
                "target_url": url
                or f"https://github.com/{REPO}/actions/workflows/fork-build.yml",
            },
        )

    def build(self, record):
        require(record.get("source_sha"), "Missing exact build source")
        record.update(
            status="building", request=uuid.uuid4().hex, policy_sha=self.ref()
        )
        self.save()  # Persist identity before dispatch; a retry can recover this exact request.
        self.status(record, "pending")
        self.dispatch(
            "fork-build.yml",
            {
                "release_id": str(record["id"]),
                "source_sha": record["source_sha"],
                "request": record["request"],
            },
        )

    def issue(self, record, reason):
        body = f"Upstream `{record['tag']}` (`{record['upstream_sha']}`) remains pending.\n\n{reason}\n\nCandidate: `{record.get('source_sha', 'not prepared')}`. Repair attempts: {record['attempts']}/3.\n\nInspect [durable state](https://github.com/{REPO}/blob/{STATE_BRANCH}/state.json) and [Actions](https://github.com/{REPO}/actions). No release was replaced. An owner may retry through Fork Coordinator after resolving the cause."
        if record.get("issue"):
            self.api.request(
                f"repos/{REPO}/issues/{record['issue']}", "PATCH", {"body": body}
            )
        else:
            issue = self.api.request(
                f"repos/{REPO}/issues",
                "POST",
                {
                    "title": f"Pending fork release: {record['tag']} r{record['revision']}",
                    "body": body,
                },
            )
            record["issue"] = issue["number"]
        self.save()

    def repair(self, record, reason):
        record["diagnostic"] = reason
        if record["attempts"] >= BOOT["max_repair_attempts"]:
            record["status"] = "blocked"
            self.save()
            self.issue(record, "The persisted repair budget is exhausted. " + reason)
            return
        record.update(
            status="repairing",
            attempts=record["attempts"] + 1,
            repair_request=uuid.uuid4().hex,
        )
        self.save()
        self.dispatch(
            "upstream-repair.lock.yml",
            {
                "release_id": str(record["id"]),
                "source_sha": record["source_sha"],
                "candidate_branch": record["branch"],
                "request": record["repair_request"],
            },
        )

    def repair_result(self, run):
        match = re.fullmatch(r"Fork repair (\d+) ([0-9a-f]{32})", run["display_title"])
        if not match:
            return
        record = next(
            (
                r
                for r in self.state["releases"]
                if r["id"] == int(match[1]) and r.get("repair_request") == match[2]
            ),
            None,
        )
        if not record or record["status"] != "repairing":
            return
        prs = self.api.pages(f"repos/{REPO}/pulls?state=open&base={record['branch']}")
        prs = [
            p
            for p in prs
            if p["head"]["repo"]["full_name"] == REPO
            and p["title"].startswith(f"MCP repair: {record['id']} ")
            and "fork-repair" in [label["name"] for label in p["labels"]]
        ]
        require(len(prs) <= 1, "Multiple repair PRs need owner resolution")
        if not prs:
            record["status"] = "blocked"
            self.save()
            self.issue(
                record,
                "Repair produced no code PR (or the engine/platform failed). Automatic inference is paused. "
                + run["html_url"],
            )
            return
        pr = prs[0]
        require(
            run["head_sha"] == record["policy_sha"],
            "Repair ran from unexpected workflow policy",
        )
        if pr["head"]["sha"] == record["source_sha"]:
            record["status"] = "blocked"
            self.save()
            self.issue(
                record,
                "Repair did not produce a new code commit; inference is paused. "
                + run["html_url"],
            )
            return
        record.update(pr=pr["number"], source_sha=pr["head"]["sha"], reviewed=False)
        self.save()
        self.build(record)

    def merged(self, pr):
        record = next(
            (
                r
                for r in self.state["releases"]
                if r.get("pr") == pr["number"]
                and r["status"] not in ("published", "skipped")
            ),
            None,
        )
        if not record or not pr["merged"]:
            return
        require(
            pr["base"]["repo"]["full_name"] == REPO
            and pr["head"]["repo"]["full_name"] == REPO
            and pr["base"]["ref"] == record["branch"],
            "Unexpected repair PR identity",
        )
        require(
            self.ref(record["branch"]) == pr["merge_commit_sha"],
            "Repair branch advanced after merge",
        )
        record.update(source_sha=pr["merge_commit_sha"], reviewed=True)
        self.save()
        self.build(record)

    def build_result(self, run):
        match = re.fullmatch(
            r"Fork build (\d+) ([0-9a-f]{40}) ([0-9a-f]{32})", run["display_title"]
        )
        if not match:
            return
        record = next(
            (
                r
                for r in self.state["releases"]
                if r["id"] == int(match[1]) and r.get("request") == match[3]
            ),
            None,
        )
        if not record or record["status"] != "building":
            return  # Old attempts must never promote or consume a repair budget.
        require(
            record["source_sha"] == match[2]
            and run["head_sha"] == record["policy_sha"],
            "Stale or unrelated build",
        )
        record["build_run"] = run["id"]
        self.save()
        self.status(
            record,
            "success" if run["conclusion"] == "success" else "failure",
            run["html_url"],
        )
        if run["conclusion"] != "success":
            jobs = self.api.pages(f"repos/{REPO}/actions/runs/{run['id']}/jobs", "jobs")
            failed = [
                s["name"]
                for j in jobs
                for s in j.get("steps", [])
                if s.get("conclusion") == "failure"
            ]
            if (
                run["conclusion"] != "failure"
                or not failed
                or any(
                    not name.startswith(
                        ("Selector gates", "Build and smoke", "Verify candidate")
                    )
                    for name in failed
                )
            ):
                record["status"] = "blocked"
                self.save()
                self.issue(
                    record,
                    "Infrastructure, dependency setup, cancellation, or policy failure needs review; no repeated model calls. "
                    + run["html_url"],
                )
            else:
                self.repair(
                    record,
                    "Independent validation failed: "
                    + run["html_url"]
                    + "; steps: "
                    + ", ".join(failed),
                )
            return
        if record.get("pr") and not record.get("reviewed"):
            record["status"] = "review"
            self.save()
            return
        self.publish(record, run)

    def publish(self, record, run):
        try:
            publish_verified(self, record, run)
            record.update(
                status="published",
                fork_release=f"https://github.com/{REPO}/releases/tag/{release_tag(record)}",
                fork_sha=record["source_sha"],
            )
            self.save()
            # Process the backlog immediately after success, without a second recurring poll.
            self.dispatch("fork-coordinator.yml", {})
        except Exception:
            record["status"] = "publication_failed"
            self.save()
            raise

    def revise(self, release_id, revision):
        old = next(
            r for r in reversed(self.state["releases"]) if str(r["id"]) == release_id
        )
        pending = next_pending(self.state)
        recovery = old["status"] == "publication_failed" and pending is old
        require(
            revision == old["revision"] + 1
            and ((old["status"] == "published" and pending is None) or recovery),
            "Revision must follow the published version with an empty backlog, or recover the failed pending publication",
        )
        if recovery:
            old.update(
                status="skipped",
                skip_reason=f"Owner requested recovery revision {revision}; existing draft and tag remain unchanged",
            )
        record = {k: old[k] for k in ("id", "tag", "upstream_sha", "published_at")}
        record.update(
            revision=revision,
            branch=f"fork-candidates/{record['id']}-r{revision}",
            status="pending",
            attempts=old["attempts"],
            pr=None,
        )
        self.state["releases"].append(record)
        self.state["releases"].sort(
            key=lambda r: (r["published_at"], r["id"], r["revision"])
        )
        self.save()

    def advance(self, retry=False):
        record = next_pending(self.state)
        if not record:
            print("noop: no pending eligible release")
            return
        validate_record(record)
        if record["status"] == "pending":
            self.integrate(record)
        elif record["status"] == "integrated":
            self.build(record)
        elif retry and record["status"] == "publication_failed":
            run = self.api.request(f"repos/{REPO}/actions/runs/{record['build_run']}")
            require(
                run["conclusion"] == "success"
                and run["head_sha"] == record["policy_sha"],
                "Invalid publication retry",
            )
            self.publish(record, run)
        elif retry and record["status"] in ("blocked", "review"):
            self.build(record)  # Owner retry; budget is retained, never reset.
        elif record["status"] in ("building", "repairing"):
            workflow = (
                "fork-build.yml"
                if record["status"] == "building"
                else "upstream-repair.lock.yml"
            )
            key = (
                record["request"]
                if record["status"] == "building"
                else record["repair_request"]
            )
            runs = self.api.pages(
                f"repos/{REPO}/actions/workflows/{workflow}/runs?event=workflow_dispatch&branch={BRANCH}",
                "workflow_runs",
            )
            matches = [r for r in runs if r["display_title"].endswith(key)]
            if matches:
                run = matches[0]
                if run["status"] == "completed":
                    consume_run(self, str(run["id"]))
            elif retry:
                # Recover a dispatch lost after state was persisted without changing identity.
                inputs = {
                    "release_id": str(record["id"]),
                    "source_sha": record["source_sha"],
                    "request": key,
                }
                if workflow != "fork-build.yml":
                    inputs["candidate_branch"] = record["branch"]
                self.dispatch(workflow, inputs)
        else:
            print(f"noop: {record['tag']} remains {record['status']}")


def verify_boundary(source, policy):
    require(
        SHA.fullmatch(source) and SHA.fullmatch(policy), "Invalid source/policy commit"
    )
    changed = git("diff", "--name-only", policy, source, "--", *BOUNDARY)
    require(not changed, "Fork policy or regression tests changed:\n" + changed)
    modules = git("show", f"{source}:codex-rs/core/tests/suite/mod.rs")
    require(
        "mod mcp_server_selection;" in modules, "Selector regressions were disconnected"
    )
    require(
        "mcp_selection_tests"
        in git("show", f"{source}:codex-rs/tui/src/bottom_pane/mcp_selection.rs"),
        "TUI regressions were disconnected",
    )


def publish_verified(coordinator, record, run):
    api = coordinator.api
    validate_record(record)
    artifacts = api.pages(
        f"repos/{REPO}/actions/runs/{run['id']}/artifacts", "artifacts"
    )
    artifacts = [a for a in artifacts if a["name"].startswith("gear-")]
    require(
        sorted(a["name"] for a in artifacts) == sorted("gear-" + t for t in TARGETS),
        "Incomplete or duplicate platform assets",
    )
    files = {}
    for artifact in artifacts:
        require(
            not artifact["expired"]
            and artifact["workflow_run"]["head_sha"] == record["policy_sha"],
            "Expired or unrelated artifact",
        )
        archive = api.request(
            f"repos/{REPO}/actions/artifacts/{artifact['id']}/zip", binary=True
        )
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            names = bundle.namelist()
            target = artifact["name"][5:]
            extension = ".zip" if "windows" in target else ".tar.gz"
            name = "codex-mcp-" + target + extension
            require(
                sorted(names) == sorted([name, name + ".json"]),
                "Unexpected artifact paths",
            )
            require(
                sum(i.file_size for i in bundle.infolist()) < 4_000_000_000,
                "Oversized artifact",
            )
            payload = bundle.read(name)
            manifest = json.loads(bundle.read(name + ".json"))
            require(
                manifest
                == {
                    "schema": 1,
                    "release_id": record["id"],
                    "upstream_tag": record["tag"],
                    "upstream_sha": record["upstream_sha"],
                    "source_sha": record["source_sha"],
                    "target": target,
                    "run_id": run["id"],
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "asset": name,
                },
                "Artifact identity/hash mismatch",
            )
            files[name] = payload
            files[name + ".json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    # Only trusted policy is executed here. Source commits are fetched for ancestry/path inspection.
    git(
        "fetch",
        "--no-tags",
        "origin",
        record["source_sha"],
        record["base_sha"],
        record["policy_sha"],
    )
    verify_boundary(record["source_sha"], record["policy_sha"])
    for ancestor in (record["base_sha"], record["upstream_sha"]):
        require(
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", ancestor, record["source_sha"]]
            ).returncode
            == 0,
            "Candidate lost upstream/fork ancestry",
        )
    current = coordinator.ref()
    require(
        current in (record["base_sha"], record["source_sha"]),
        "Maintained branch changed; refusing stale promotion",
    )
    if current != record["source_sha"]:
        coordinator.push(
            record["source_sha"], BRANCH
        )  # Exact tested commit, fast-forward only.
    tag = release_tag(record)
    tag_ref = api.optional(f"repos/{REPO}/git/ref/tags/{tag}")
    if tag_ref:
        require(
            tag_ref["object"]["sha"] == record["source_sha"],
            "Published tag identity mismatch",
        )
    else:
        api.request(
            f"repos/{REPO}/git/refs",
            "POST",
            {"ref": "refs/tags/" + tag, "sha": record["source_sha"]},
        )
    files["release.json"] = (
        json.dumps(
            {
                "schema": 1,
                "unofficial": True,
                "upstream_tag": record["tag"],
                "upstream_sha": record["upstream_sha"],
                "fork_sha": record["source_sha"],
                "release_id": record["id"],
                "revision": record["revision"],
                "targets": TARGETS,
                "workflow_run": run["html_url"],
            },
            indent=2,
        )
        + "\n"
    ).encode()
    files["SHA256SUMS"] = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n"
        for name, data in sorted(files.items())
    ).encode()
    release = api.optional(f"repos/{REPO}/releases/tags/{tag}")
    if not release:
        release = api.request(
            f"repos/{REPO}/releases",
            "POST",
            {
                "tag_name": tag,
                "target_commitish": record["source_sha"],
                "name": "Unofficial Codex MCP " + tag,
                "draft": True,
                "prerelease": False,
                "body": f"Unofficial Codex CLI with conversation-local MCP server selection.\n\nUpstream `{record['tag']}`: `{record['upstream_sha']}`\nFork: `{record['source_sha']}`\n\n[Validated build]({run['html_url']}) · [Installation and rollback](https://github.com/{REPO}/blob/{record['source_sha']}/fork/README.md)\n\nAll five targets passed. No upstream signing or notarization is claimed. Install alongside official Codex using `bin/codex-mcp`.",
            },
        )
    existing = {
        a["name"]: a for a in api.pages(f"repos/{REPO}/releases/{release['id']}/assets")
    }
    require(set(existing) <= set(files), "Unexpected assets in existing release")
    for name, data in files.items():
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        if name in existing:
            require(
                existing[name]["size"] == len(data)
                and existing[name].get("digest") == digest,
                "Existing asset mismatch; use a new revision",
            )
        else:
            require(
                release["draft"], "Published release is immutable; use a new revision"
            )
            result = api.request(
                release["upload_url"].split("{")[0]
                + "?name="
                + urllib.parse.quote(name),
                "POST",
                data,
            )
            require(
                result["size"] == len(data) and result.get("digest") == digest,
                "Uploaded asset verification failed",
            )
    assets = api.pages(f"repos/{REPO}/releases/{release['id']}/assets")
    require(set(a["name"] for a in assets) == set(files), "Incomplete draft release")
    if release["draft"]:
        api.request(
            f"repos/{REPO}/releases/{release['id']}",
            "PATCH",
            {"draft": False, "make_latest": "true"},
        )


def consume_run(coordinator, run_id):
    require(re.fullmatch(r"[1-9][0-9]{0,19}", run_id), "Invalid workflow run ID")
    allowed = {
        ".github/workflows/fork-build.yml": coordinator.build_result,
        ".github/workflows/upstream-repair.lock.yml": coordinator.repair_result,
    }
    # The final job explicitly dispatches this workflow before GitHub marks its
    # parent completed. Wait for authoritative completion, never trust an input
    # conclusion or execute an artifact. This is bounded Actions work, not a poller.
    for attempt in range(61):
        run = coordinator.api.request(f"repos/{REPO}/actions/runs/{run_id}")
        require(
            run["repository"]["full_name"] == REPO
            and run["head_repository"]["full_name"] == REPO
            and run["head_branch"] == BRANCH
            and run["event"] == "workflow_dispatch"
            and run["path"] in allowed,
            "Untrusted workflow result",
        )
        pending = next_pending(coordinator.state)
        key = "request" if run["path"].endswith("fork-build.yml") else "repair_request"
        if not pending or not run["display_title"].endswith(
            pending.get(key, "invalid")
        ):
            print("noop: obsolete workflow completion")
            return
        require(run["head_sha"] == pending["policy_sha"], "Unrelated workflow policy")
        if run["status"] == "completed":
            break
        require(
            attempt < 60,
            "Workflow is still running; the daily check can recover completion",
        )
        time.sleep(5)
    workflow = coordinator.api.request(
        f"repos/{REPO}/actions/workflows/{run['workflow_id']}"
    )
    require(workflow["path"] == run["path"], "Workflow identity mismatch")
    allowed[run["path"]](run)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "coordinate"
    coordinator = Coordinator(API())
    if mode == "preflight":
        coordinator.load()
        before = json.dumps(coordinator.state, sort_keys=True)
        coordinator.discover()
        pending = next_pending(coordinator.state)
        actionable = pending and pending["status"] in ("pending", "integrated")
        if pending and pending["status"] in ("building", "repairing"):
            workflow = (
                "fork-build.yml"
                if pending["status"] == "building"
                else "upstream-repair.lock.yml"
            )
            key = (
                pending["request"]
                if pending["status"] == "building"
                else pending["repair_request"]
            )
            runs = coordinator.api.pages(
                f"repos/{REPO}/actions/workflows/{workflow}/runs?event=workflow_dispatch&branch={BRANCH}",
                "workflow_runs",
            )
            actionable = any(
                r["status"] == "completed" and r["display_title"].endswith(key)
                for r in runs
            )
        print(
            json.dumps(
                {
                    "actionable": bool(
                        actionable
                        or json.dumps(coordinator.state, sort_keys=True) != before
                    ),
                    "pending": pending,
                },
                indent=2,
            )
        )
        return
    require(
        os.environ.get("GITHUB_REPOSITORY") == REPO,
        "This workflow belongs only to drhelius/codex",
    )
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    coordinator.load(create=mode == "coordinate")
    if mode in ("build-identity", "repair-identity"):
        inputs = event["inputs"]
        record = next_pending(coordinator.state)
        require(
            record
            and str(record["id"]) == inputs["release_id"]
            and record["source_sha"] == inputs["source_sha"],
            "Unrelated or stale candidate",
        )
        key = "request" if mode == "build-identity" else "repair_request"
        require(record[key] == inputs["request"], "Obsolete request")
        require(
            record["status"]
            == ("building" if mode == "build-identity" else "repairing"),
            "Inactive request",
        )
        validate_record(record)
        if mode == "build-identity":
            require(
                os.environ["GITHUB_SHA"] == record["policy_sha"],
                "Policy changed before build dispatch",
            )
        else:
            require(
                os.environ["GITHUB_SHA"] == record["policy_sha"],
                "Policy changed before repair dispatch",
            )
            require(
                git("rev-parse", "HEAD") == record["source_sha"],
                "Repair checkout does not match the ledger",
            )
            require(
                record["branch"] == inputs["candidate_branch"]
                and record["attempts"] <= 3,
                "Invalid repair branch/budget",
            )
        print(json.dumps(record, indent=2))
        return
    require(
        os.environ.get("GITHUB_REF") == "refs/heads/" + BRANCH
        or os.environ["GITHUB_EVENT_NAME"] == "pull_request",
        "Coordinator must use maintained policy",
    )
    name = os.environ["GITHUB_EVENT_NAME"]
    if name == "workflow_run":
        consume_run(coordinator, str(event["workflow_run"]["id"]))
    elif name == "pull_request":
        coordinator.merged(
            coordinator.api.request(f"repos/{REPO}/pulls/{event['number']}")
        )
    else:
        require(name in ("workflow_dispatch", "push"), "Unsupported event")
        inputs = event.get("inputs") or {}
        if inputs.get("completed_run"):
            consume_run(coordinator, inputs["completed_run"])
            return
        before = json.dumps(coordinator.state, sort_keys=True)
        coordinator.discover()
        if json.dumps(coordinator.state, sort_keys=True) != before:
            coordinator.save()
        if inputs.get("revision"):
            require(
                event["sender"]["login"] == "drhelius",
                "Only the owner may request a new revision",
            )
            coordinator.revise(inputs.get("release_id"), int(inputs["revision"]))
        retry = inputs.get("retry") in (True, "true")
        if retry:
            require(
                event["sender"]["login"] == "drhelius",
                "Only the owner may retry blocked work",
            )
        if inputs.get("source_sha"):
            require(
                event["sender"]["login"] == "drhelius",
                "Only the owner may select a source revision",
            )
            source = inputs["source_sha"]
            require(SHA.fullmatch(source), "Invalid source revision")
            record = next_pending(coordinator.state)
            require(
                record and record.get("base_sha"),
                "Prepare a candidate before selecting a source revision",
            )
            git("fetch", "--no-tags", "origin", source, record["base_sha"])
            verify_boundary(source, coordinator.ref())
            for ancestor in (record["base_sha"], record["upstream_sha"]):
                require(
                    subprocess.run(
                        ["git", "merge-base", "--is-ancestor", ancestor, source]
                    ).returncode
                    == 0,
                    "Manual source lost release ancestry",
                )
            record.update(source_sha=source, reviewed=True)
            coordinator.push(source, record["branch"])
            coordinator.save()
            coordinator.build(record)
        else:
            coordinator.advance(retry)


if __name__ == "__main__":
    main()
