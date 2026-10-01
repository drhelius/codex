"""Real Git fixtures for automatic version repair before expensive release gates."""

import copy
import json
from contextlib import chdir
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import maintenance as m
import release_versions as versions
from test_maintenance import Harness, record


class ReleaseVersionTests(unittest.TestCase):
    def test_only_the_known_workspace_version_conflict_is_resolved(self):
        for ancestor in ("", '||||||| base\nversion = "0.0.0"\n'):
            conflicted = (
                '[workspace.package]\nlicense = "Apache-2.0"\n'
                '<<<<<<< fork\nversion = "0.159.1"\n'
                + ancestor
                + '=======\nversion = "0.159.2"\n>>>>>>> upstream\n'
                '[profile.release]\nlto = "thin"\n'
            )
            expected = '[workspace.package]\nlicense = "Apache-2.0"\nversion = "0.159.2"\n[profile.release]\nlto = "thin"\n'
            self.assertEqual(
                versions.resolve_workspace_version_conflict(
                    conflicted, current_version="0.159.1", upstream_version="0.159.2"
                ),
                expected,
            )
            for ambiguous in (
                conflicted.replace('version = "0.159.1"', 'version = "0.158.0"'),
                conflicted.replace("[workspace.package]", "[dependencies.example]"),
                conflicted.replace(
                    'version = "0.159.2"', 'version = "0.159.2"\nedition = "2024"'
                ),
                conflicted + "<<<<<<< fork\nx = 1\n=======\nx = 2\n>>>>>>> upstream\n",
            ):
                self.assertIsNone(
                    versions.resolve_workspace_version_conflict(
                        ambiguous, current_version="0.159.1", upstream_version="0.159.2"
                    )
                )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.enterContext(chdir(self.temporary.name))
        self.real_git = m.git
        self.real_git("init", "-q")
        self.real_git("config", "commit.gpgsign", "false")
        self.real_git("config", "user.name", "Fixture")
        self.real_git("config", "user.email", "fixture@example.invalid")
        files = {
            "codex-rs/Cargo.toml": '[workspace.package]\nversion = "0.159.2"\n',
            "codex-rs/Cargo.lock": 'version = 4\n\n[[package]]\nname = "codex"\nversion = "0.159.1"\n\n[[package]]\nname = "codex-utils-process"\nversion = "0.0.0"\n\n[[package]]\nname = "external"\nversion = "3.4.5"\nsource = "registry+https://example.invalid"\nchecksum = "abc"\n',
            "codex-rs/core/tests/suite/mod.rs": "mod mcp_server_selection;\n",
            "codex-rs/tui/src/bottom_pane/mcp_selection.rs": "mod mcp_selection_tests;\n",
            "fork/policy": "trusted policy\n",
            "selector.rs": "preserved selector\n",
        }
        for name, text in files.items():
            path = Path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.real_git("add", ".")
        self.real_git("commit", "-qm", "fixture repaired manifest with stale lockfile")
        self.source = self.real_git("rev-parse", "HEAD")
        self.row = record()
        self.row.update(
            tag="rust-v0.159.2",
            source_sha=self.source,
            policy_sha=self.source,
            base_sha=self.source,
            upstream_sha=self.source,
            attempts=2,
        )
        self.coordinator = Harness([self.row])
        self.heads = {m.BRANCH: self.source, self.row["branch"]: self.source}
        self.enterContext(
            patch.object(
                self.coordinator,
                "ref",
                side_effect=lambda branch=m.BRANCH: self.heads[branch],
            )
        )
        self.pushes = []

        def push(sha, branch):
            self.pushes.append((sha, branch))
            self.heads[branch] = sha

        self.enterContext(patch.object(self.coordinator, "push", side_effect=push))
        self.enterContext(
            patch.object(
                m,
                "git",
                side_effect=lambda *args, **kwargs: (
                    "" if args[0] == "fetch" else self.real_git(*args, **kwargs)
                ),
            )
        )
        self.enterContext(
            patch.object(
                self.coordinator,
                "normalize_build_source",
                side_effect=lambda row: m.Coordinator.normalize_build_source(
                    self.coordinator, row
                ),
            )
        )

    def test_normalizes_all_packages_and_dispatches_exact_child_commit(self):
        original = self.real_git("show", self.source + ":codex-rs/Cargo.lock") + "\n"
        expected = versions.normalize_release_lock(
            Path("codex-rs/Cargo.toml").read_text(), original, self.row["tag"]
        )
        self.coordinator.build(self.row)
        fixed = self.row["source_sha"]
        self.assertEqual(
            self.real_git("show", fixed + ":codex-rs/Cargo.lock") + "\n", expected
        )
        self.assertEqual(
            self.real_git("rev-list", "--parents", "-n", "1", fixed).split()[1:],
            [self.source],
        )
        self.assertEqual(
            self.real_git("diff", "--name-only", self.source, fixed),
            "codex-rs/Cargo.lock",
        )
        self.assertEqual(
            self.coordinator.api.calls[-1][2]["inputs"]["source_sha"], fixed
        )
        self.assertEqual(self.pushes, [(fixed, self.row["branch"])])
        self.assertEqual(self.row["attempts"], 2)
        self.coordinator.normalize_build_source(self.row)
        self.assertEqual(self.pushes, [(fixed, self.row["branch"])])
        self.assertEqual(
            tomllib.loads(expected)["package"][-1],
            tomllib.loads(original)["package"][-1],
        )
        self.assertEqual(self.real_git("status", "--porcelain"), "")

    def test_normalizes_the_existing_open_pr_without_creating_another(self):
        branch = f"fork-repair/{self.row['id']}-r1"
        self.heads[branch] = self.source
        self.row.update(pr=4, reviewed=False)
        self.coordinator.api.prs = [
            {
                "number": 4,
                "state": "open",
                "base": {"repo": {"full_name": m.REPO}, "ref": self.row["branch"]},
                "head": {
                    "repo": {"full_name": m.REPO},
                    "ref": branch,
                    "sha": self.source,
                },
            }
        ]
        self.coordinator.build(self.row)
        self.assertEqual(self.pushes, [(self.row["source_sha"], branch)])
        self.assertFalse(self.row["reviewed"])
        self.assertEqual(self.heads[self.row["branch"]], self.source)
        self.assertFalse(
            any(path.endswith("/pulls") for path, _, _ in self.coordinator.api.calls)
        )

    def test_push_before_save_interruption_reuses_the_exact_commit(self):
        checkpoint = copy.deepcopy(self.row)
        with patch.object(
            self.coordinator, "save", side_effect=RuntimeError("interrupted")
        ):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self.coordinator.normalize_build_source(self.row)
        fixed = self.pushes[0][0]
        self.row.clear()
        self.row.update(checkpoint)
        self.coordinator.normalize_build_source(self.row)
        self.assertEqual(self.row["source_sha"], fixed)
        self.assertEqual(self.pushes, [(fixed, self.row["branch"])])

    def test_merged_pr_is_normalized_without_spending_another_repair_attempt(self):
        self.row.update(pr=4, reviewed=True)
        self.coordinator.build(self.row)
        self.assertEqual(
            (self.row["status"], self.row["reviewed"], self.row["attempts"]),
            ("building", True, 2),
        )
        self.assertEqual(self.pushes, [(self.row["source_sha"], self.row["branch"])])
        self.assertFalse(
            any("/pulls" in path for path, _, _ in self.coordinator.api.calls)
        )

    def test_concurrent_owner_changes_are_preserved(self):
        Path("selector.rs").write_text("owner changes\n")
        self.real_git("commit", "-am", "owner work")
        advanced = self.real_git("rev-parse", "HEAD")
        self.heads[self.row["branch"]] = advanced
        with self.assertRaisesRegex(RuntimeError, "refusing to overwrite"):
            self.coordinator.normalize_build_source(self.row)
        self.assertEqual(self.pushes, [])
        self.assertEqual(self.heads[self.row["branch"]], advanced)

    def test_incorrect_manifest_version_requests_repair_before_build_dispatch(self):
        self.row["tag"] = "rust-v0.159.3"
        self.coordinator.build(self.row)
        self.assertEqual(
            (self.row["status"], self.row["source_sha"]), ("repairing", self.source)
        )
        self.assertTrue(
            self.coordinator.api.calls[-1][0].endswith(
                "upstream-repair.lock.yml/dispatches"
            )
        )
        self.assertFalse(
            any(
                "fork-build.yml/dispatches" in path
                for path, _, _ in self.coordinator.api.calls
            )
        )
        self.assertEqual(self.pushes, [])

    def test_read_only_ci_preflight_rejects_stale_versions(self):
        Path("identity.json").write_text(json.dumps({"tag": self.row["tag"]}))
        command = [sys.executable, "-I", versions.__file__, "."]
        stale = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("before tool setup", stale.stderr)
        lock = Path("codex-rs/Cargo.lock")
        fixed = versions.normalize_release_lock(
            Path("codex-rs/Cargo.toml").read_text(), lock.read_text(), self.row["tag"]
        )
        lock.write_text(fixed)
        checked = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(lock.read_text(), fixed)

    def policy_update(self):
        lock = Path("codex-rs/Cargo.lock")
        lock.write_text(
            versions.normalize_release_lock(
                Path("codex-rs/Cargo.toml").read_text(),
                lock.read_text(),
                self.row["tag"],
            )
        )
        Path("selector.rs").write_text("candidate selector\n")
        self.real_git("commit", "-am", "candidate product changes")
        candidate = self.real_git("rev-parse", "HEAD")
        self.real_git("checkout", "-q", "--detach", self.source)
        Path("fork/policy").unlink()
        Path("fork/new-policy").write_text("reviewed policy update\n")
        self.real_git("add", "-A")
        self.real_git("commit", "-qm", "trusted policy changes")
        policy = self.real_git("rev-parse", "HEAD")
        self.heads.update({m.BRANCH: policy, self.row["branch"]: candidate})
        self.row.update(source_sha=candidate, pr=4, reviewed=True)
        self.enterContext(
            patch.object(
                self.coordinator,
                "refresh_build_policy",
                side_effect=lambda row: m.Coordinator.refresh_build_policy(
                    self.coordinator, row
                ),
            )
        )
        return candidate, policy

    def test_retry_refreshes_only_policy_and_dispatches_the_exact_merge(self):
        candidate, policy = self.policy_update()
        self.coordinator.build(self.row)
        refreshed = self.row["source_sha"]
        self.assertEqual(
            self.real_git("rev-list", "--parents", "-n", "1", refreshed).split()[1:],
            [candidate, policy],
        )
        self.assertEqual(
            self.real_git("diff", "--name-only", candidate, refreshed).splitlines(),
            ["fork/new-policy", "fork/policy"],
        )
        self.assertEqual(
            self.real_git("show", refreshed + ":selector.rs"), "candidate selector"
        )
        m.verify_boundary(refreshed, policy)
        self.assertEqual(
            (
                self.row["base_sha"],
                self.row["policy_sha"],
                self.row["attempts"],
                self.row["reviewed"],
            ),
            (policy, policy, 2, True),
        )
        self.assertEqual(
            self.coordinator.api.calls[-1][2]["inputs"]["source_sha"], refreshed
        )
        self.coordinator.refresh_build_policy(self.row)
        self.assertEqual(self.pushes, [(refreshed, self.row["branch"])])

    def test_policy_refresh_recovers_a_push_before_its_atomic_state_save(self):
        _, policy = self.policy_update()
        checkpoint = copy.deepcopy(self.row)
        with patch.object(
            self.coordinator, "save", side_effect=RuntimeError("interrupted")
        ):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self.coordinator.refresh_build_policy(self.row)
        refreshed = self.pushes[0][0]
        self.row.clear()
        self.row.update(checkpoint)
        self.coordinator.refresh_build_policy(self.row)
        self.assertEqual(
            (self.row["source_sha"], self.row["base_sha"], self.row["policy_sha"]),
            (refreshed, policy, policy),
        )
        self.assertEqual(self.pushes, [(refreshed, self.row["branch"])])

    def test_policy_refresh_reuses_the_existing_open_pr_branch(self):
        candidate, policy = self.policy_update()
        branch = f"fork-repair/{self.row['id']}-r1"
        self.heads[branch] = candidate
        self.row["reviewed"] = False
        self.coordinator.api.prs = [
            {
                "number": 4,
                "state": "open",
                "base": {"repo": {"full_name": m.REPO}, "ref": self.row["branch"]},
                "head": {
                    "repo": {"full_name": m.REPO},
                    "ref": branch,
                    "sha": candidate,
                },
            }
        ]
        self.coordinator.build(self.row)
        self.assertEqual(self.pushes, [(self.row["source_sha"], branch)])
        self.assertEqual(self.heads[self.row["branch"]], candidate)
        self.assertEqual(
            (self.row["policy_sha"], self.row["reviewed"]), (policy, False)
        )

    def test_policy_refresh_refuses_to_discard_new_product_changes_on_main(self):
        candidate, _ = self.policy_update()
        Path("selector.rs").write_text("new product code on main\n")
        self.real_git("commit", "-am", "owner product change")
        self.heads[m.BRANCH] = self.real_git("rev-parse", "HEAD")
        with self.assertRaisesRegex(RuntimeError, "Maintained product code changed"):
            self.coordinator.build(self.row)
        self.assertEqual(
            (self.row["source_sha"], self.pushes, self.coordinator.api.calls),
            (candidate, [], []),
        )

    def test_policy_refresh_preserves_concurrent_candidate_work(self):
        candidate, _ = self.policy_update()
        self.real_git("checkout", "-q", "--detach", candidate)
        Path("selector.rs").write_text("concurrent candidate work\n")
        self.real_git("commit", "-am", "owner candidate change")
        advanced = self.real_git("rev-parse", "HEAD")
        self.heads[self.row["branch"]] = advanced
        with self.assertRaisesRegex(RuntimeError, "refusing to overwrite"):
            self.coordinator.build(self.row)
        self.assertEqual((self.heads[self.row["branch"]], self.pushes), (advanced, []))


if __name__ == "__main__":
    unittest.main()
