"""Offline lifecycle fixtures. No fixture writes GitHub or a production ledger."""

import copy
from contextlib import chdir
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import maintenance as m
import validate as v

A, B, C = "a" * 40, "b" * 40, "c" * 40


def record(ident=398926954, status="pending"):
    return {
        "id": ident,
        "tag": "rust-v0.159.0",
        "upstream_sha": A,
        "published_at": m.BOOT["published_at"],
        "revision": 1,
        "branch": f"fork-candidates/{ident}-r1",
        "status": status,
        "attempts": 0,
        "pr": None,
        "source_sha": C,
        "integration_sha": C,
        "base_sha": B,
        "policy_sha": B,
        "request": "1" * 32,
    }


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.prs = []
        self.releases = []
        self.assets = []
        self.bundles = {}
        self.release = None
        self.tag = None

    def request(self, path, method="GET", data=None, binary=False):
        self.calls.append((path, method, data))
        if "/dispatches" in path or "/statuses/" in path:
            return None
        if path.endswith("/git/ref/heads/fork-main"):
            return {"object": {"sha": B}}
        if "/commits/" in path:
            return {"sha": A}
        if path.endswith("/issues"):
            return {"number": 7}
        if "/issues/" in path:
            return {}
        if path.endswith("/zip"):
            return self.bundles[path.split("/")[-2]]
        if path.endswith("/git/refs"):
            self.tag = {"object": {"sha": data["sha"]}}
            return self.tag
        if path.endswith("/releases") and method == "POST":
            self.release = dict(
                data,
                id=8,
                upload_url="https://uploads.github.com/repos/drhelius/codex/releases/8/assets{?name,label}",
            )
            return self.release
        if path.endswith("/releases/8"):
            self.release.update(data)
            return self.release
        if "?name=" in path:
            name = path.split("?name=")[1]
            asset = {
                "name": name,
                "size": len(data),
                "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
            }
            self.assets.append(asset)
            return asset
        raise AssertionError((path, method))

    def optional(self, path):
        if "/git/ref/tags/" in path:
            return self.tag
        if "/releases/tags/" in path:
            return self.release
        return None

    def pages(self, path, key=None):
        if path.endswith("/releases"):
            return self.releases
        if "/pulls?" in path:
            return self.prs
        if path.endswith("/artifacts"):
            return [
                {
                    "id": index,
                    "name": "gear-" + target,
                    "expired": False,
                    "workflow_run": {"head_sha": B},
                }
                for index, target in enumerate(m.TARGETS)
            ]
        if path.endswith("/assets"):
            return self.assets
        if path.endswith("/jobs"):
            return [
                {
                    "steps": [
                        {
                            "name": "Selector gates and existing MCP regressions",
                            "conclusion": "failure",
                        }
                    ]
                }
            ]
        raise AssertionError(path)


class Harness(m.Coordinator):
    def __init__(self, records=()):
        super().__init__(FakeAPI())
        self.state = {"schema": 1, "repository": m.REPO, "releases": list(records)}
        self.history = []

    def save(self):
        self.history.append(copy.deepcopy(self.state))

    def integrate(self, row):
        self.history.append(("integrate", row["id"]))
        self.build(row)

    def push(self, sha, branch):
        self.history.append(("push", sha, branch))


def run(row, success=True):
    return {
        "id": 101,
        "display_title": f"Fork build {row['id']} {row['source_sha']} {row['request']}",
        "head_sha": B,
        "conclusion": "success" if success else "failure",
        "html_url": "https://github.com/drhelius/codex/actions/runs/101",
    }


def fill_artifacts(api, row, workflow):
    for index, target in enumerate(m.TARGETS):
        name = "codex-gear-" + target + (".zip" if "windows" in target else ".tar.gz")
        payload = b"fixture bytes; never executed"
        manifest = {
            "schema": 1,
            "release_id": row["id"],
            "upstream_tag": row["tag"],
            "upstream_sha": row["upstream_sha"],
            "source_sha": row["source_sha"],
            "target": target,
            "run_id": workflow["id"],
            "sha256": hashlib.sha256(payload).hexdigest(),
            "asset": name,
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            bundle.writestr(name, payload)
            bundle.writestr(name + ".json", json.dumps(manifest))
        api.bundles[str(index)] = buffer.getvalue()


class MaintenanceTests(unittest.TestCase):
    def test_paginated_discovery_stable_baseline_and_order(self):
        api = m.API()
        pages = [[{"id": i} for i in range(100)], [{"id": 100}]]
        with patch.object(api, "request", side_effect=pages) as query:
            self.assertEqual(len(api.pages("repos/openai/codex/releases")), 101)
            self.assertIn("page=2", query.call_args[0][0])
        fixture = [
            {
                "id": 2,
                "tag_name": "rust-v0.160.0",
                "published_at": "2026-09-30T00:00:00Z",
                "draft": False,
                "prerelease": False,
            },
            {
                "id": 1,
                "tag_name": m.BOOT["upstream_tag"],
                "published_at": m.BOOT["published_at"],
                "draft": False,
                "prerelease": False,
            },
        ]
        excluded = [
            dict(fixture[0], tag_name="rust-v0.161.0-alpha.1", prerelease=True),
            dict(fixture[0], tag_name="desktop-v2.0.0"),
            dict(fixture[0], draft=True),
        ]
        self.assertEqual([r["id"] for r in m.eligible(fixture + excluded)], [1, 2])

    def test_annotated_upstream_tag_resolves_immutable_commit(self):
        api = m.API()
        with patch.object(
            api,
            "request",
            side_effect=[
                {"object": {"type": "tag", "sha": A}},
                {"object": {"type": "commit", "sha": B}},
            ],
        ) as request:
            self.assertEqual(m.resolve_upstream_tag(api, "rust-v0.159.0"), B)
            self.assertIn("/git/ref/tags/", request.call_args_list[0][0][0])
            self.assertIn("/git/tags/", request.call_args_list[1][0][0])

    def test_no_update_has_no_write_or_dispatch(self):
        coordinator = Harness([record(status="published")])
        coordinator.advance()
        self.assertEqual(coordinator.api.calls, [])
        self.assertEqual(coordinator.history, [])

    def test_backlog_is_ordered_and_blocked_release_is_never_skipped(self):
        first, second = record(), record(398926955)
        coordinator = Harness([first, second])
        coordinator.advance()
        self.assertEqual(coordinator.history[0], ("integrate", first["id"]))
        self.assertEqual(second["status"], "pending")
        before = len(coordinator.api.calls)
        coordinator.advance()
        self.assertEqual(len(coordinator.api.calls), before)
        first["status"] = "blocked"
        coordinator.advance()
        self.assertEqual(second["status"], "pending")

    def test_repair_needed_budget_is_persisted_before_dispatch(self):
        row = record()
        coordinator = Harness([row])
        for expected in (1, 2, 3):
            coordinator.repair(row, "fixture compile failure")
            self.assertEqual(row["attempts"], expected)
            self.assertEqual(
                coordinator.history[-1]["releases"][0]["status"], "repairing"
            )
        coordinator.repair(row, "still failing")
        self.assertEqual(row["status"], "blocked")
        dispatches = [c for c in coordinator.api.calls if c[0].endswith("/dispatches")]
        self.assertEqual(len(dispatches), 3)
        self.assertEqual(row["issue"], 7)

    def test_build_failure_dispatches_real_ghaw_repair(self):
        row = record(status="building")
        coordinator = Harness([row])
        coordinator.build_result(run(row, success=False))
        self.assertEqual(row["status"], "repairing")
        self.assertTrue(
            coordinator.api.calls[-1][0].endswith("upstream-repair.lock.yml/dispatches")
        )

    def test_repaired_candidate_waits_for_owner_and_old_runs_are_ignored(self):
        row = record(status="building")
        row["pr"] = 21
        coordinator = Harness([row])
        old = run(row)
        old["display_title"] = old["display_title"].replace(row["request"], "2" * 32)
        coordinator.build_result(old)
        self.assertEqual(row["status"], "building")
        coordinator.build_result(run(row))
        self.assertEqual(row["status"], "review")
        self.assertFalse(any("/releases" in c[0] for c in coordinator.api.calls))

    def test_pr_merge_dispatches_exact_merged_commit(self):
        row = record(status="review")
        row["pr"] = 21
        coordinator = Harness([row])
        pr = {
            "number": 21,
            "merged": True,
            "base": {"repo": {"full_name": m.REPO}, "ref": row["branch"]},
            "head": {"repo": {"full_name": m.REPO}},
            "merge_commit_sha": A,
        }
        with patch.object(coordinator, "ref", return_value=A):
            coordinator.merged(pr)
        self.assertTrue(row["reviewed"])
        self.assertEqual(coordinator.api.calls[-1][2]["inputs"]["source_sha"], A)

    def test_failed_publication_preserves_pending_state(self):
        row = record(status="building")
        coordinator = Harness([row])
        with patch.object(
            m, "publish_verified", side_effect=RuntimeError("fixture upload failure")
        ):
            with self.assertRaises(RuntimeError):
                coordinator.publish(row, run(row))
        self.assertEqual(row["status"], "publication_failed")
        self.assertNotIn("fork_release", row)

    def test_all_assets_are_verified_before_draft_publication_and_retry_is_idempotent(
        self,
    ):
        row = record(status="building")
        coordinator = Harness([row])
        workflow = run(row)
        fill_artifacts(coordinator.api, row, workflow)
        with (
            patch.object(m, "git", return_value=""),
            patch.object(m, "verify_boundary"),
            patch.object(
                m.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
            ),
        ):
            m.publish_verified(coordinator, row, workflow)
            self.assertFalse(coordinator.api.release["draft"])
            self.assertEqual(len(coordinator.api.assets), 12)
            count = len(coordinator.api.assets)
            m.publish_verified(coordinator, row, workflow)
            self.assertEqual(len(coordinator.api.assets), count)
        self.assertIn(("push", C, m.BRANCH), coordinator.history)

    def test_partial_or_tampered_artifacts_cannot_promote(self):
        row = record(status="building")
        coordinator = Harness([row])
        workflow = run(row)
        fill_artifacts(coordinator.api, row, workflow)
        coordinator.api.bundles["0"] = coordinator.api.bundles["1"]
        with self.assertRaises(RuntimeError):
            m.publish_verified(coordinator, row, workflow)
        self.assertFalse(coordinator.history)
        self.assertIsNone(coordinator.api.release)

    def test_lock_normalization_changes_only_workspace_package_versions(self):
        manifest = '[workspace.package]\nversion = "0.160.0"\n'
        lock = 'version = 4\n\n[[package]]\nname = "codex"\nversion = "0.159.0"\n\n[[package]]\nname = "dependency"\nversion = "1.2.3"\nsource = "registry+https://example.invalid"\nchecksum = "abc"\n'
        normalized = m.normalize_workspace_lock(manifest, lock)
        self.assertEqual(
            normalized, lock.replace('version = "0.159.0"', 'version = "0.160.0"')
        )

    def test_shell_and_path_inputs_are_validated(self):
        for field, value in (
            ("branch", "fork-candidates/1;echo unsafe"),
            ("source_sha", "$(command)"),
            ("tag", "../../release"),
        ):
            bad = record()
            bad[field] = value
            with self.assertRaises(RuntimeError):
                m.validate_record(bad)

    def test_expired_failed_publication_can_recover_with_explicit_revision(self):
        old = record(status="publication_failed")
        old["attempts"] = 2
        queued = record(old["id"] + 1)
        coordinator = Harness([old, queued])
        coordinator.revise(str(old["id"]), 2)
        pending = m.next_pending(coordinator.state)
        self.assertEqual((pending["id"], pending["revision"]), (old["id"], 2))
        self.assertEqual(pending["attempts"], 2)
        self.assertEqual(old["status"], "skipped")
        self.assertIn("Owner requested", old["skip_reason"])
        self.assertEqual(queued["status"], "pending")
        self.assertEqual(coordinator.api.calls, [])
        with self.assertRaises(RuntimeError):
            coordinator.revise(str(old["id"]), 3)

    def test_mandatory_test_registration_rejects_missing_or_ignored_tests(self):
        suites = {}
        for package, path in (
            ("codex-core", "core/tests/suite/mcp_server_selection.rs"),
            ("codex-tui", "tui/src/bottom_pane/mcp_selection_tests.rs"),
        ):
            names = v.re.findall(
                r"^(?:async )?fn (mcp_(?:selection|selector)_\w+)\(",
                (v.POLICY / "codex-rs" / path).read_text(),
                v.re.M,
            )
            suites[package] = {
                "package-name": package,
                "testcases": {
                    name: {"ignored": False, "filter-match": {"status": "matches"}}
                    for name in names
                },
            }
        listing = {"rust-suites": suites}
        v.require_selector_tests(listing)
        cases = suites["codex-core"]["testcases"]
        first = next(iter(cases))
        cases[first]["ignored"] = True
        with self.assertRaises(RuntimeError):
            v.require_selector_tests(listing)
        del cases[first]
        with self.assertRaises(RuntimeError):
            v.require_selector_tests(listing)

    def test_actual_coordinator_merges_and_restores_policy_before_dispatch(self):
        with tempfile.TemporaryDirectory() as directory, chdir(directory):

            def git(*args):
                return subprocess.check_output(["git", *args], text=True).strip()

            git("init", "-q")
            git("config", "commit.gpgsign", "false")
            git("config", "user.name", "Fixture")
            git("config", "user.email", "fixture@example.invalid")
            Path("codex-rs").mkdir()
            Path(".github").mkdir()
            Path("codex-rs/Cargo.toml").write_text(
                '[workspace.package]\nversion = "0.159.0"\n'
            )
            Path("codex-rs/Cargo.lock").write_text(
                'version = 4\n[[package]]\nname = "local"\nversion = "0.0.0"\n'
            )
            Path(".github/policy").write_text("original")
            Path("selector.rs").write_text("original")
            git("add", ".")
            git("commit", "-qm", "baseline")
            base = git("rev-parse", "HEAD")
            Path(".github/policy").write_text("fork policy")
            Path("selector.rs").write_text("fork selector")
            git("commit", "-am", "fork")
            fork = git("rev-parse", "HEAD")
            git("checkout", "-q", "--detach", base)
            Path(".github/policy").write_text("upstream policy")
            Path("codex-rs/Cargo.toml").write_text(
                '[workspace.package]\nversion = "0.160.0"\n'
            )
            git("commit", "-am", "upstream")
            real_git = m.git
            for conflict in (False, True):
                if conflict:
                    Path("selector.rs").write_text("conflicting upstream selector")
                    git("commit", "-am", "upstream API change")
                upstream = git("rev-parse", "HEAD")
                row = record(m.BOOT["release_id"] + 1)
                row.update(tag="rust-v0.160.0", upstream_sha=upstream)
                coordinator = Harness([row])
                request = coordinator.api.request

                def api(path, *args, **kwargs):
                    if path == f"repos/openai/codex/releases/{row['id']}":
                        return {
                            "draft": False,
                            "prerelease": False,
                            "tag_name": row["tag"],
                        }
                    if path == f"repos/openai/codex/git/ref/tags/{row['tag']}":
                        return {"object": {"type": "commit", "sha": upstream}}
                    return request(path, *args, **kwargs)

                def local_git(*args, **kwargs):
                    return "" if args[0] == "fetch" else real_git(*args, **kwargs)

                with (
                    patch.object(coordinator, "ref", return_value=fork),
                    patch.object(coordinator.api, "request", side_effect=api),
                    patch.object(m, "git", side_effect=local_git),
                ):
                    m.Coordinator.integrate(coordinator, row)
                source = row["source_sha"]
                self.assertEqual(
                    git("rev-list", "--parents", "-n", "1", source).split()[1:],
                    [fork, upstream],
                )
                self.assertEqual(git("show", source + ":.github/policy"), "fork policy")
                self.assertIn(
                    'version = "0.160.0"', git("show", source + ":codex-rs/Cargo.lock")
                )
                self.assertEqual(row["status"], "repairing" if conflict else "building")
                self.assertEqual(row["attempts"], int(conflict))

    def test_real_merge_tree_preserves_both_parents_and_detects_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:

            def git(*args):
                return subprocess.check_output(
                    [
                        "git",
                        "-c",
                        "commit.gpgsign=false",
                        "-c",
                        "user.name=Fixture",
                        "-c",
                        "user.email=fixture@example.invalid",
                        *args,
                    ],
                    cwd=directory,
                    text=True,
                ).strip()

            git("init", "-q")
            file = Path(directory) / "selector.rs"
            file.write_text("baseline\n")
            git("add", ".")
            git("commit", "-qm", "baseline")
            base = git("rev-parse", "HEAD")
            file.write_text("fork selection\n")
            git("commit", "-am", "fork selector")
            fork = git("rev-parse", "HEAD")
            git("checkout", "-q", "--detach", base)
            (Path(directory) / "other.rs").write_text("upstream addition\n")
            git("add", ".")
            git("commit", "-qm", "clean upstream")
            upstream = git("rev-parse", "HEAD")
            tree = git("merge-tree", "--write-tree", fork, upstream)
            commit = git(
                "commit-tree", tree, "-p", fork, "-p", upstream, "-m", "integration"
            )
            self.assertEqual(git("show", f"{commit}:selector.rs"), "fork selection")
            self.assertEqual(
                git("rev-list", "--parents", "-n", "1", commit).split()[1:],
                [fork, upstream],
            )
            file.write_text("incompatible upstream\n")
            git("commit", "-am", "upstream API change")
            conflict = subprocess.run(
                ["git", "merge-tree", "--write-tree", fork, "HEAD"],
                cwd=directory,
                capture_output=True,
            )
            self.assertEqual(conflict.returncode, 1)


if __name__ == "__main__":
    unittest.main()
