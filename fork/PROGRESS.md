# Bootstrap progress

## Build performance follow-up

- [x] Inspect live build steps and distinguish compilation from test execution.
- [x] Run tests and optimized packaging in parallel for all five platforms.
- [x] Cache compiled dependencies with the pinned release toolchain and isolated phase keys.
- [x] Use four compiler jobs where runner memory permits; retain two on ARM macOS.
- [x] Reuse selector cases in a focused harness and preserve the original full-suite registration.
- [x] Confirm the exact existing test set, run release gates and check workflow compilation.
- [ ] Sign, push and hand the optimized build to Actions.

Local validation: the old and focused gates select the same 416 cases, and all
416 pass with retries disabled. The fork daemon bootstrap regression also passes.
Thirty offline script fixtures pass; the two Windows-only fixtures remain mandatory
in Actions. Both gh-aw definitions compile without warnings or generated changes,
and workflow lint passes. The expanded matrix has exactly two phases per target.
Full hosted build timings and the new dependency caches will be measured by Actions.

## Build reliability follow-up

- [x] Diagnose the native Windows update failure and cold tool-setup delay from Actions logs.
- [x] Preserve atomic installer updates with a valid PowerShell backup path.
- [x] Gate the Rust matrix on fast native installer and policy checks.
- [x] Verify pinned prebuilt test tools for every supported runner; preserve dependency downloads after failed tests.
- [x] Keep cancelled and installer-policy failures outside the Copilot repair budget.
- [x] Run local script, PowerShell, workflow and compiler-consistency checks.
- [x] Sign and push the installer/build fixes (`f79c45f688`).
- [x] Diagnose the handoff's transient GitHub API 504 and add bounded read-only retries.
- [x] Fix incompatible PowerShell module inheritance exposed by the fast native Windows gate.
- [x] Supersede the obsolete build and hand the replacement run to Actions (36595825015).

Local checks: 28 offline fixtures pass; the two native Windows fixtures remain
required in Actions. Both gh-aw definitions compile with no warnings and unchanged
locks. All nine pinned tool archives pass checksum/extraction checks for the five
runner architectures; native macOS tools start successfully. PowerShell 7 parses
the installer and passes atomic replacement, pending the native PowerShell 5.1 gate.

## Installer follow-up

- [x] Inspect upstream installers and the fork's complete distribution layout.
- [x] Add isolated install/update scripts and the `codex-mcp` command.
- [x] Verify installation, updates, rollback and official-Codex coexistence with fixtures.
- [x] Include installers in release gates and document usage.
- [x] Fix observed Actions failures: build the remote-test CLI prerequisite,
      stabilize gh-aw schedule compilation, and explicitly dispatch completion.
- [x] Preserve CLI/package version compatibility and prevent copied fork daemons
      from enabling the official automatic updater.

Follow-up validation: 18 CLI help/update tests and the three previously failing
remote-MCP tests pass locally with retries disabled. The native macOS package
passes installer/update and existing mock-MCP smoke checks. Twenty-five offline
installer/maintenance fixtures pass; the two Windows-only fixtures remain native
Windows release gates. PowerShell syntax was parsed locally. Both gh-aw locks
match byte-for-byte in an origin-only Actions-style checkout with the explicit
schedule seed. A broader CLI run initially passed 492/498 checks; all six failures
or timeouts passed after the package compatibility fixes or reduced-concurrency
reruns. The 70 upstream daemon tests pass with normal build settings, and the
fork-specific daemon bootstrap check passes with official automatic updates disabled.
Signing, push and the replacement build run are reported after
this source checkpoint; the full platform build/publication belongs to Actions.

Implementation checkpoint recorded in the bootstrap commit. Repository setup and
the initial Actions run are reported by the bootstrap session after this commit is pushed.

- [x] Verify `drhelius`, create the public `openai/codex` fork, and disable inherited Actions during setup.
- [x] Read the four live Gearboy workflows; pin gh-aw v0.87.4 and Copilot CLI 1.0.80.
- [x] Resolve stable CLI baseline: release 398926954, `rust-v0.159.0`, commit `687a119f0fcaace47e1f1abcc77cec6c813fd6da`; create `fork-main`.
- [x] Implement conversation-local server selection through the TUI, app-server, and incremental MCP runtime.
- [x] Add deterministic selector, process-lifetime, and mocked-model regressions.
- [x] Implement durable release state, ordered integration, bounded gh-aw repair PRs, and explicit workflow dispatch wiring.
- [x] Implement unprivileged five-platform builds, complete packaging/smoke checks, and trusted atomic publication.
- [x] Compile gh-aw, validate workflow/script fixtures, and run available Rust checks.
- [x] Document fork behavior, installation, review policy, recovery and the finite Actions handoff.

## Implementation plan

Keep server registration, per-conversation availability, and connection lifetime separate. Reuse the existing MCP runtime reconciliation and TUI multi-select picker. Apply selection at complete-turn boundaries; retain inactive connections, reject stale dispatch, and inherit parent restrictions in subagents. Fresh processes use configuration defaults.

Keep fork policy under `fork/` and fork-owned workflows. Use a daily gh-aw entry point and a separate dispatch-only gh-aw repair workflow. The pinned compiler's `workflow_run` fork restriction does not affect scheduled/dispatch agent execution; an ordinary trusted workflow bridges build results to dispatch. Keep mechanical upstream merges separate from bounded agent repair patches. Persist release identity and attempts, serialize promotion, and publish only verified artifacts from an exact successful five-platform run.

GitHub Actions owns all execution after the final push/dispatch. No local recurring process is part of this project.

## Validation checkpoint

- Native macOS ARM64 CLI and code-mode/proxy helpers built with Rust 1.95.0.
- Final extracted package smoke passed: helper startup, parseable fork version, updater protection, local mock MCP/model, delayed activation, retention, stale dispatch and cold resume defaults.
- Ten core selector regressions, three picker regressions and command routing are covered by the expanded MCP/TUI suite: 416/416 passed with retries disabled.
- Touched-crate Clippy completed successfully; only three pre-existing unused-import warnings remain. Relevant help snapshots were updated and passed.
- App-server JSON/TypeScript/Python artifacts regenerated; Bazel lock check finished without a lock diff. Full repository formatting passed.
- gh-aw v0.87.4 compilation: two workflows, no warnings. Engine 1.0.80 and permission/fork conditions inspected.
- Workflow lint, compile consistency and seventeen offline maintenance fixtures passed, including actual Git integration and failed-publication recovery.
- Next, outside this immutable source checkpoint: sign/push, set the default branch and Actions permissions, dispatch the initial coordinator, and report its actual run URL/status. Subsequent platform builds and publication belong to Actions.
