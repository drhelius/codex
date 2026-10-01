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
from unittest.mock import MagicMock, patch
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
        self.runs = []

    def request(self, path, method="GET", data=None, binary=False):
        self.calls.append((path, method, data))
        if "/dispatches" in path or "/statuses/" in path:
            return None
        if path.endswith("/actions/runs/101"):
            return self.runs[0]
        if path.endswith("/actions/workflows/17"):
            return {"path": ".github/workflows/fork-build.yml"}
        if path.endswith("/git/ref/heads/fork-main"):
            return {"object": {"sha": B}}
        if "/commits/" in path:
            return {"sha": A}
        if "/pulls/" in path:
            return next(pr for pr in self.prs if path.endswith("/" + str(pr["number"])))
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

    def pages(self, path, key=None, *, per_page=100):
        if "/runs?event=workflow_dispatch" in path:
            return self.runs
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

    def normalize_build_source(self, row):
        # Lifecycle fixtures use symbolic SHAs. Native Git cases cover normalization.
        pass


def run(row, success=True):
    return {
        "id": 101,
        "display_title": f"Fork build {row['id']} {row['source_sha']} {row['request']}",
        "head_sha": B,
        "conclusion": "success" if success else "failure",
        "html_url": "https://github.com/drhelius/codex/actions/runs/101",
        "repository": {"full_name": m.REPO},
        "head_repository": {"full_name": m.REPO},
        "head_branch": m.BRANCH,
        "event": "workflow_dispatch",
        "workflow_id": 17,
        "path": ".github/workflows/fork-build.yml",
        "status": "completed",
    }


def fill_artifacts(api, row, workflow):
    for index, target in enumerate(m.TARGETS):
        name = "codex-mcp-" + target + (".zip" if "windows" in target else ".tar.gz")
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
    def test_transient_github_read_failure_recovers(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"id": 42}'
        error = m.urllib.error.HTTPError(
            "https://api.github.com/fixture", 504, "Gateway Timeout", {}, None
        )
        with (
            patch.object(m.urllib.request, "build_opener") as opener,
            patch.object(m.time, "sleep"),
        ):
            opener.return_value.open.side_effect = [error, response]
            self.assertEqual(m.API().request("fixture"), {"id": 42})
            self.assertEqual(opener.return_value.open.call_count, 2)

    def test_github_read_retries_are_bounded_and_writes_are_never_repeated(self):
        for method, attempts in (("GET", 3), ("POST", 1)):
            with (
                self.subTest(method=method),
                patch.object(m.urllib.request, "build_opener") as opener,
                patch.object(m.time, "sleep"),
            ):
                opener.return_value.open.side_effect = m.urllib.error.HTTPError(
                    "https://api.github.com/fixture", 504, "Gateway Timeout", {}, None
                )
                with self.assertRaises(m.urllib.error.HTTPError):
                    m.API().request("fixture", method=method)
                self.assertEqual(opener.return_value.open.call_count, attempts)

    def test_interrupted_github_response_is_replaced_by_a_complete_read(self):
        for error in (
            m.http.client.IncompleteRead(b'{"id":', 26_984_539),
            ConnectionResetError("Connection reset by peer"),
            TimeoutError("Read timed out"),
        ):
            with (
                self.subTest(error=type(error).__name__),
                patch.object(m.urllib.request, "build_opener") as opener,
                patch.object(m.time, "sleep") as sleep,
            ):
                response = opener.return_value.open.return_value.__enter__.return_value
                response.read.side_effect = [error, b'{"id": 42}']
                self.assertEqual(m.API().request("fixture"), {"id": 42})
                self.assertEqual(opener.return_value.open.call_count, 2)
                sleep.assert_called_once_with(1)

    def test_interrupted_reads_are_bounded_and_uncertain_writes_are_not_repeated(self):
        for method, attempts in (("GET", 3), ("POST", 1), ("PATCH", 1), ("PUT", 1)):
            with (
                self.subTest(method=method),
                patch.object(m.urllib.request, "build_opener") as opener,
                patch.object(m.time, "sleep") as sleep,
            ):
                response = opener.return_value.open.return_value.__enter__.return_value
                response.read.side_effect = m.http.client.IncompleteRead(b"", 100)
                with self.assertRaises(m.http.client.IncompleteRead):
                    m.API().request("fixture", method=method)
                self.assertEqual(opener.return_value.open.call_count, attempts)
                self.assertEqual(sleep.call_count, attempts - 1)

    def test_permission_failure_is_not_retried(self):
        with (
            patch.object(m.urllib.request, "build_opener") as opener,
            patch.object(m.time, "sleep") as sleep,
        ):
            opener.return_value.open.side_effect = m.urllib.error.HTTPError(
                "https://api.github.com/fixture", 403, "Forbidden", {}, None
            )
            with self.assertRaises(m.urllib.error.HTTPError):
                m.API().request("fixture")
            self.assertEqual(opener.return_value.open.call_count, 1)
            sleep.assert_not_called()

    def test_completion_waits_for_authoritative_result_and_verifies_workflow(self):
        row = record(status="building")
        coordinator = Harness([row])
        completed = run(row)
        running = dict(completed, status="in_progress", conclusion=None)
        with (
            patch.object(
                coordinator.api,
                "request",
                side_effect=[running, completed, {"path": completed["path"]}],
            ),
            patch.object(coordinator, "build_result") as consume,
            patch.object(m.time, "sleep") as sleep,
        ):
            m.consume_run(coordinator, "101")
            sleep.assert_called_once_with(5)
            consume.assert_called_once_with(completed)
        for field, value in (
            ("head_repository", {"full_name": "openai/codex"}),
            ("head_sha", A),
            ("path", ".github/workflows/unrelated.yml"),
        ):
            with patch.object(
                coordinator.api,
                "request",
                return_value=dict(completed, **{field: value}),
            ):
                with self.assertRaises(RuntimeError):
                    m.consume_run(coordinator, "101")
        self.assertEqual(coordinator.history, [])

    def test_missed_completion_is_recovered_without_owner_retry_and_stale_is_noop(self):
        row = record(status="building")
        coordinator = Harness([row])
        coordinator.api.runs = [run(row)]
        with patch.object(coordinator, "build_result") as consume:
            coordinator.advance()
            consume.assert_called_once_with(coordinator.api.runs[0])
        row["request"] = "2" * 32
        with patch.object(coordinator, "build_result") as consume:
            m.consume_run(coordinator, "101")
            consume.assert_not_called()
        self.assertEqual(coordinator.history, [])

    def test_completion_wait_is_bounded(self):
        row = record(status="building")
        coordinator = Harness([row])
        coordinator.api.runs = [dict(run(row), status="in_progress", conclusion=None)]
        with patch.object(m.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "still running"):
                m.consume_run(coordinator, "101")
            self.assertEqual(sleep.call_count, 60)
        self.assertEqual(coordinator.history, [])

    def test_paginated_discovery_stable_baseline_and_order(self):
        api = m.API()
        for page_size in (20, 100):
            rows = [{"id": i} for i in range(page_size + 1)]
            pages = [rows[:page_size], rows[page_size:]]
            with (
                self.subTest(page_size=page_size),
                patch.object(api, "request", side_effect=pages) as query,
            ):
                self.assertEqual(
                    api.pages("repos/openai/codex/releases", per_page=page_size), rows
                )
                self.assertEqual(
                    [call.args[0] for call in query.call_args_list],
                    [
                        f"repos/openai/codex/releases?per_page={page_size}&page=1",
                        f"repos/openai/codex/releases?per_page={page_size}&page=2",
                    ],
                )
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
            dict(fixture[0], tag_name="rust-v0.161.0-rc.1", prerelease=False),
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
        # Package jobs may already have uploaded all assets when a parallel test fails.
        with patch.object(coordinator, "publish") as publish:
            coordinator.build_result(run(row, success=False))
            publish.assert_not_called()
        self.assertEqual(row["status"], "repairing")
        self.assertTrue(
            coordinator.api.calls[-1][0].endswith("upstream-repair.lock.yml/dispatches")
        )

    def test_repair_retry_targets_existing_open_pr_and_stops_after_merge(self):
        row = record(status="building")
        row.update(pr=4, attempts=1)
        coordinator = Harness([row])
        pr = {
            "number": 4,
            "state": "open",
            "base": {"repo": {"full_name": m.REPO}, "ref": row["branch"]},
            "head": {"repo": {"full_name": m.REPO}},
        }
        coordinator.api.prs = [pr]
        coordinator.build_result(run(row, success=False))
        self.assertEqual((row["status"], row["attempts"]), ("repairing", 2))
        self.assertEqual(
            coordinator.api.calls[-1][2]["inputs"]["pull_request_number"], "4"
        )
        before = len(coordinator.api.calls)
        pr["state"] = "closed"
        row.update(status="building", reviewed=True)
        coordinator.build_result(run(row, success=False))
        self.assertEqual(
            (row["status"], row["attempts"], row["issue"]), ("blocked", 2, 7)
        )
        self.assertFalse(
            any(
                path.endswith("/dispatches")
                for path, _, _ in coordinator.api.calls[before:]
            )
        )

    def test_owner_retry_refreshes_conflicts_without_building_them(self):
        row = record(status="blocked")
        row.update(conflicts="codex-rs/Cargo.toml", attempts=1)
        coordinator = Harness([row])
        with (
            patch.object(coordinator, "integrate") as integrate,
            patch.object(coordinator, "build") as build,
        ):
            coordinator.advance()
            integrate.assert_not_called()
            coordinator.advance(retry=True)
            integrate.assert_called_once_with(row, retry_conflicts=True)
            build.assert_not_called()
        self.assertEqual(row["attempts"], 1)
        row["status"] = "integrated"
        coordinator.advance()
        self.assertEqual((row["status"], row["attempts"]), ("repairing", 2))
        self.assertTrue(
            coordinator.api.calls[-1][0].endswith("upstream-repair.lock.yml/dispatches")
        )

    def test_conflict_refresh_preserves_existing_work_and_repair_budget(self):
        original = record(status="blocked")
        original.update(conflicts="codex-rs/Cargo.toml", attempts=1)
        for changes in (
            {"pr": 42},
            {"source_sha": A},
            {"attempts": 3},
            {"status": "repairing"},
            {"conflicts": ""},
        ):
            row = dict(original, **changes)
            coordinator = Harness([row])
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                m.Coordinator.integrate(coordinator, row, retry_conflicts=True)
            self.assertEqual(coordinator.history, [])
        coordinator = Harness([original])
        coordinator.api.prs = [{"number": 42}]
        with self.assertRaisesRegex(RuntimeError, "existing candidate PR"):
            m.Coordinator.integrate(coordinator, original, retry_conflicts=True)
        self.assertEqual(coordinator.history, [])

    def test_owner_source_uses_current_promotion_base_without_resetting_budget(self):
        source, current = "d" * 40, "e" * 40
        for includes_current in (False, True):
            with (
                self.subTest(includes_current=includes_current),
                tempfile.TemporaryDirectory() as directory,
            ):
                row = record(status="blocked")
                row.update(attempts=3, pr=3, reviewed=True)
                original = copy.deepcopy(row)
                coordinator = Harness([row])
                event = Path(directory) / "event.json"
                event.write_text(
                    json.dumps(
                        {
                            "sender": {"login": "drhelius"},
                            "inputs": {"source_sha": source},
                        }
                    )
                )

                def ancestry(args):
                    self.assertEqual(args[:3], ["git", "merge-base", "--is-ancestor"])
                    self.assertEqual(args[-1], source)
                    return subprocess.CompletedProcess(
                        args, int(args[-2] == current and not includes_current)
                    )

                with (
                    patch.object(m, "Coordinator", return_value=coordinator),
                    patch.object(coordinator, "load"),
                    patch.object(coordinator, "discover"),
                    patch.object(coordinator, "ref", return_value=current),
                    patch.object(m, "git"),
                    patch.object(m, "verify_boundary") as boundary,
                    patch.object(m.subprocess, "run", side_effect=ancestry) as checked,
                    patch.object(m.sys, "argv", ["maintenance.py", "coordinate"]),
                    patch.dict(
                        m.os.environ,
                        {
                            "GITHUB_REPOSITORY": m.REPO,
                            "GITHUB_EVENT_PATH": str(event),
                            "GITHUB_EVENT_NAME": "workflow_dispatch",
                            "GITHUB_REF": "refs/heads/" + m.BRANCH,
                        },
                    ),
                ):
                    if includes_current:
                        m.main()
                    else:
                        with self.assertRaisesRegex(
                            RuntimeError, "lost release ancestry"
                        ):
                            m.main()
                boundary.assert_called_once_with(source, current)
                self.assertEqual(
                    [call.args[0][-2] for call in checked.call_args_list],
                    [B, A, current],
                )
                if includes_current:
                    expected = dict(
                        original,
                        source_sha=source,
                        base_sha=current,
                        policy_sha=current,
                        status="building",
                        request=row["request"],
                    )
                    self.assertEqual(row, expected)
                    self.assertIn(("push", source, row["branch"]), coordinator.history)
                    self.assertTrue(
                        coordinator.api.calls[-1][0].endswith(
                            "fork-build.yml/dispatches"
                        )
                    )
                else:
                    self.assertEqual(row, original)
                    self.assertEqual(coordinator.history, [])

    def test_cancelled_and_installer_failures_do_not_consume_repair_budget(self):
        for conclusion, step in (
            ("cancelled", "Selector gates and existing MCP regressions"),
            ("failure", "Native installer lifecycle and integrity fixtures"),
        ):
            with self.subTest(conclusion=conclusion, step=step):
                row = record(status="building")
                coordinator = Harness([row])
                workflow = run(row, success=False)
                workflow["conclusion"] = conclusion
                jobs = [{"steps": [{"name": step, "conclusion": "failure"}]}]
                with patch.object(coordinator.api, "pages", return_value=jobs):
                    coordinator.build_result(workflow)
                self.assertEqual((row["status"], row["attempts"]), ("blocked", 0))
                self.assertFalse(
                    any(
                        path.endswith("/dispatches")
                        for path, _, _ in coordinator.api.calls
                    )
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
            self.assertEqual(len(coordinator.api.assets), 2 * len(m.TARGETS) + 2)
            count = len(coordinator.api.assets)
            m.publish_verified(coordinator, row, workflow)
            self.assertEqual(len(coordinator.api.assets), count)
        self.assertIn(("push", C, m.BRANCH), coordinator.history)

    def test_partial_or_tampered_artifacts_cannot_promote(self):
        row = record(status="building")
        coordinator = Harness([row])
        workflow = run(row)
        fill_artifacts(coordinator.api, row, workflow)
        with patch.object(m, "TARGETS", ["fixture-other-target"]):
            fill_artifacts(coordinator.api, row, workflow)
        with self.assertRaises(RuntimeError):
            m.publish_verified(coordinator, row, workflow)
        self.assertFalse(coordinator.history)
        self.assertIsNone(coordinator.api.release)

    def test_lock_normalization_changes_only_workspace_package_versions(self):
        manifest = '[workspace.package]\nversion = "0.160.0"\n'
        lock = 'version = 4\n\n[[package]]\nname = "codex"\nversion = "0.159.0"\n\n[[package]]\nname = "codex-utils-process"\nversion = "0.0.0"\n\n[[package]]\nname = "dependency"\nversion = "1.2.3"\nsource = "registry+https://example.invalid"\nchecksum = "abc"\n'
        normalized = m.normalize_workspace_lock(manifest, lock)
        self.assertEqual(
            normalized,
            lock.replace('version = "0.159.0"', 'version = "0.160.0"').replace(
                'version = "0.0.0"', 'version = "0.160.0"'
            ),
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
                '[workspace.package]\nversion = "0.0.0"\n'
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
            Path("codex-rs/Cargo.toml").write_text(
                '[workspace.package]\nversion = "0.159.0"\n'
            )
            git("commit", "-am", "fork")
            fork = git("rev-parse", "HEAD")
            git("checkout", "-q", "--detach", base)
            Path(".github/policy").write_text("upstream policy")
            Path("codex-rs/Cargo.toml").write_text(
                '[workspace.package]\nversion = "0.160.0"\n'
            )
            with Path("codex-rs/Cargo.lock").open("a") as lock:
                lock.write('[[package]]\nname = "new-package"\nversion = "0.0.0"\n')
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

    def test_manifest_conflict_retry_uses_current_policy_and_recovers_interrupted_push(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory, chdir(directory):
            real_git = m.git
            real_git("init", "-q")
            real_git("config", "commit.gpgsign", "false")
            real_git("config", "user.name", "Fixture")
            real_git("config", "user.email", "fixture@example.invalid")
            Path("codex-rs").mkdir()
            Path(".github").mkdir()
            manifest = Path("codex-rs/Cargo.toml")
            manifest.write_text(
                '[workspace.package]\nversion = "0.0.0"\ndescription = "base"\n'
            )
            Path("codex-rs/Cargo.lock").write_text(
                'version = 4\n[[package]]\nname = "local"\nversion = "0.0.0"\n'
            )
            Path(".github/policy").write_text("upstream")
            real_git("add", ".")
            real_git("commit", "-qm", "baseline")
            baseline = real_git("rev-parse", "HEAD")
            manifest.write_text(
                '[workspace.package]\nversion = "0.159.0"\ndescription = "fork"\n'
            )
            Path(".github/policy").write_text("old fork policy")
            real_git("commit", "-am", "fork")
            fork = real_git("rev-parse", "HEAD")
            real_git("checkout", "-q", "--detach", baseline)
            manifest.write_text(
                '[workspace.package]\nversion = "0.159.1"\ndescription = "upstream"\n'
            )
            real_git("commit", "-am", "stable upstream release")
            upstream = real_git("rev-parse", "HEAD")
            row = record(m.BOOT["release_id"] + 1)
            row.update(tag="rust-v0.159.1", upstream_sha=upstream)
            coordinator = Harness([row])
            request = coordinator.api.request

            def api(path, *args, **kwargs):
                if path == f"repos/openai/codex/releases/{row['id']}":
                    return {"draft": False, "prerelease": False, "tag_name": row["tag"]}
                if path == f"repos/openai/codex/git/ref/tags/{row['tag']}":
                    return {"object": {"type": "commit", "sha": upstream}}
                return request(path, *args, **kwargs)

            def local_git(*args, **kwargs):
                return "" if args[0] == "fetch" else real_git(*args, **kwargs)

            with (
                patch.object(coordinator, "ref", return_value=fork) as ref,
                patch.object(coordinator.api, "request", side_effect=api),
                patch.object(m, "git", side_effect=local_git),
            ):
                m.Coordinator.integrate(coordinator, row)
                previous = row["source_sha"]
                self.assertIn("codex-rs/Cargo.toml", row["conflicts"])
                self.assertEqual((row["status"], row["attempts"]), ("repairing", 1))
                row["status"] = "blocked"
                checkpoint = copy.deepcopy(row)
                real_git("checkout", "-q", "--detach", fork)
                Path(".github/policy").write_text("reviewed manifest repair policy")
                real_git("commit", "-am", "allow manifest repair PRs")
                updated = real_git("rev-parse", "HEAD")
                ref.return_value = updated
                branch = {"object": {"sha": previous}}
                changed = real_git(
                    "commit-tree",
                    previous + "^{tree}",
                    "-p",
                    previous,
                    "-m",
                    "owner work",
                )
                with patch.object(
                    coordinator.api,
                    "optional",
                    return_value={"object": {"sha": changed}},
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "refusing to discard work"
                    ):
                        m.Coordinator.integrate(coordinator, row, retry_conflicts=True)
                self.assertEqual(row, checkpoint)

                def push(sha, name):
                    self.assertEqual(name, row["branch"])
                    branch["object"]["sha"] = sha

                with (
                    patch.object(coordinator.api, "optional", return_value=branch),
                    patch.object(coordinator, "push", side_effect=push) as pushed,
                ):
                    with patch.object(
                        coordinator,
                        "save",
                        side_effect=RuntimeError("interrupted save"),
                    ):
                        with self.assertRaisesRegex(RuntimeError, "interrupted save"):
                            m.Coordinator.integrate(
                                coordinator, row, retry_conflicts=True
                            )
                    source = branch["object"]["sha"]
                    row.clear()
                    row.update(checkpoint)
                    m.Coordinator.integrate(coordinator, row, retry_conflicts=True)
                    pushed.assert_called_once_with(source, row["branch"])
                self.assertEqual(row["source_sha"], source)
                self.assertEqual(row["integration_sha"], source)
                self.assertEqual(
                    (row["base_sha"], row["policy_sha"]), (updated, updated)
                )
                self.assertEqual((row["status"], row["attempts"]), ("repairing", 2))
                self.assertEqual(
                    real_git("rev-list", "--parents", "-n", "1", source).split()[1:],
                    [updated, upstream, previous],
                )
                self.assertEqual(
                    real_git("show", source + ":.github/policy"),
                    "reviewed manifest repair policy",
                )
                self.assertIn(
                    "<<<<<<<", real_git("show", source + ":codex-rs/Cargo.toml")
                )
                self.assertEqual(
                    coordinator.api.calls[-1],
                    (
                        f"repos/{m.REPO}/actions/workflows/upstream-repair.lock.yml/dispatches",
                        "POST",
                        {
                            "ref": m.BRANCH,
                            "inputs": {
                                "release_id": str(row["id"]),
                                "source_sha": source,
                                "candidate_branch": row["branch"],
                                "pull_request_number": "0",
                                "request": row["repair_request"],
                            },
                        },
                    ),
                )

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
