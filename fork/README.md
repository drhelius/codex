# Codex MCP: unofficial CLI fork

Public fork: <https://github.com/drhelius/codex>. Maintained branch: `fork-main`.
The initial baseline is the stable CLI release `rust-v0.159.0`, upstream commit
`687a119f0fcaace47e1f1abcc77cec6c813fd6da` (release ID `398926954`).
Upstream history and license notices are retained. This fork adds conversation-local
MCP server selection. It is not an OpenAI release.

Install it as **`codex-mcp`** alongside official `codex`, using the checked-in
[macOS/Linux installer](install/install.sh) or [Windows installer](install/install.ps1).
Run `codex-mcp update` to update from this fork's stable releases. Complete helper
packages and installer state stay separate from the official installation.

See [selector behavior and regression contract](SELECTOR.md) and
[installation and rollback](INSTALL.md). The bootstrap checklist is [PROGRESS.md](PROGRESS.md).

## Automation and ownership

`upstream-maintainer.md` is a real gh-aw daily schedule, with optional manual
dispatch. Its read-only deterministic preflight paginates published
`openai/codex` releases, accepts stable `rust-vMAJOR.MINOR.PATCH` CLI tags from the
baseline onward, and resolves each new tag to a commit. Prereleases, desktop and
other release streams are excluded (`include_prereleases: false`). The agent
uses quiet `noop` for an empty queue or already-running, blocked or review work.
There is no hourly check, companion repository, external scheduler or local daemon.

The small JSON ledger on [`fork-state`](https://github.com/drhelius/codex/blob/fork-state/state.json)
records release IDs/tags, upstream/base/integration/source SHAs, revision, status,
repair attempt count, PR, workflow run and publication. Its commits survive
artifact expiration. Releases are queued by publication time and ID, processed
one at a time. A blocked release blocks later releases. Only the owner may
explicitly mark an entry `skipped`, with a reason, through an explicit ledger edit.

The event chain is:

```text
bootstrap push/manual coordinator dispatch
    -> exact bootstrap candidate -> Fork Build -> trusted publish
Daily Codex Upstream Maintainer (gh-aw)
    -> safe dispatch Fork Coordinator -> next release merge
        -> clean candidate -> Fork Build -> fast-forward fork-main -> publish
        -> conflict/failure -> Fork Repair (gh-aw) -> one repair PR
            -> independent Fork Build -> await owner review
            -> owner merges into candidate -> Fork Build -> promote/publish
```

Isolated final jobs explicitly dispatch completion from both the build and repair
workflows. The coordinator verifies the exact run and waits up to five minutes
for GitHub's authoritative completion status before processing its result.
The existing `workflow_run` bridge is also idempotent, and the daily preflight
recovers completed requests if a notification was missed. Explicit
same-repository `workflow_dispatch` connects jobs written with `GITHUB_TOKEN`;
the pipeline does not depend on token-generated push/PR/tag events. See
[GitHub's documented event behavior](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).
Only intended owned branches, workflow paths/IDs, ledger request IDs and exact
source/policy SHAs are accepted. Stale results are ignored or rejected.

`fork-candidates/<release-id>-r<revision>` contains the mechanical upstream merge
with **both parents**. `fork-repair/<release-id>-r<revision>` adds the small repair.
The PR targets that candidate in this repository. No history is flattened or
force-pushed. The default is `auto_merge_repairs: false`; no automatic merging of
repairs is implemented. Owner merge resumes validation/publication automatically.
The trusted coordinator reports `fork/release-gates` on the exact source commit,
so repair PRs show the independently dispatched validation result.
The repair agent creates code through native `create-pull-request` and updates
existing code through `push-to-pull-request-branch`; a PR metadata update alone
is insufficient. The three-attempt budget persists across reruns. A no-change,
platform/permission failure or exhausted budget leaves the same release pending
and a diagnostic issue, avoiding repeated inference.

The trusted policy boundary consists of `fork/`, `.github/`, root `AGENTS.md` and
the dedicated selector regression files, their snapshot and the mock stdio server. Integration restores these paths from
the maintained base, then independently checks them at build and publication.
All inherited executable workflows are archived as `fork/upstream-workflows/*.disabled`.
New upstream workflows cannot silently become active. The agent cannot edit
policy, tests, dependencies, credentials or release gates through its allowed
Rust-source safe outputs. Necessary policy/test/dependency adaptations require
owner review. Large mechanical merges never pass through the agent's patch
transport or its bounded 60-file/1-MiB create-PR allowance.

Upstream release tags currently leave path-package versions at `0.0.0` in
`Cargo.lock`. Bootstrap and the deterministic integrator synchronize only those
workspace package versions with `workspace.package.version`; external package
versions and checksums are preserved. No dependency resolver runs with write
credentials. The repository's Bazel lock update is also checked at bootstrap.

## gh-aw compilation

Compiler **v0.87.4**, Copilot CLI **1.0.80**, matching the live Gearboy watcher and
Build Doctor inspected at bootstrap. The Copilot engine uses its supported
default model selection, as in those reference workflows. Both sources specify
`strict: true`, bounded outputs and narrow network allowlists, with GitHub tools
in `gh-proxy` mode. Permissions are `contents: read`, `pull-requests: read`, and
`copilot-requests: write`; the repair workflow additionally reads Actions.
The generated engine receives `${{ github.token }}` through the framework's
permission-based Copilot mechanism. No new inference secret or copied local
credentials are configured. Optional framework token references in generated
YAML do not create a separate authentication prerequisite.

```sh
gh extension install github/gh-aw --pin v0.87.4
gh aw compile upstream-maintainer upstream-repair --strict --no-check-update --schedule-seed drhelius/codex
python3 fork/scripts/tools.py /tmp/codex-gear-tools
python3 fork/scripts/check_policy.py /tmp/codex-gear-tools
```

Commit Markdown, generated `.lock.yml` files and `.github/aw/actions-lock.json`.
Never edit generated locks by hand. Pinned-tool SHA-256 verification, compilation
consistency, ordinary workflow lint and offline lifecycle fixtures are mandatory
release gates. Scheduled/dispatch agent conditions were inspected for this fork;
no generated security or fork guard was removed. Fork Repair uses dispatch rather
than the compiler's fork-restricted `workflow_run` agent trigger.
The explicit schedule seed keeps the daily cron identical in local checkouts
with an `upstream` remote and Actions checkouts with only `origin`.

## Validation, releases and recovery

`fork-build.yml` runs without repository-write/publication credentials. Fast policy
and native macOS/Linux/Windows installer checks must pass before the Rust matrix
starts. It installs the release's exact Rust toolchain, uses locked Cargo
dependencies and builds on standard hosted runners for macOS ARM64/x86_64,
Linux ARM64/x86_64 (musl) and Windows x86_64. A failed target cancels the remaining
matrix work; publication still requires all five targets to succeed. Each target has
two parallel jobs: deterministic selector/MCP tests and optimized distribution
builds with extracted-archive smoke tests. Both phases must succeed on all targets;
an uploaded package cannot bypass a failed test job. Archive checks use a local
MCP server and local model endpoint.
Native installer fixtures cover updates, rollback, failed integrity checks and
official-Codex coexistence; an extracted CLI also exercises its real update command
against local release fixtures. Keep these gates and bundled installers across integrations.
The CLI and package retain matching upstream versions for daemon compatibility;
fork identity lives in help output and release provenance. Native Unix gates also
verify that a daemon copied from the fork cannot enable official automatic updates.
No desktop application, real model or paid inference is needed by those tests.
Pinned official `just` and `cargo-nextest` binaries are SHA-256 verified instead
of compiled from source on every runner. The pinned Rust cache action retains
compiled external dependencies and Cargo downloads, including after failures,
with separate keys for each platform, phase, compiler, build environment and lockfile.
Workspace crates and test binaries are rebuilt, and Cargo still checks dependency
fingerprints. The release retains upstream optimization and thin LTO. Test and
release profiles compile concurrently on separate standard runners; four compiler
jobs are allowed on the 14/16-GB runners and two on the 7-GB ARM Mac.
The focused core selector harness reuses the original ten integration cases;
the complete upstream suite keeps its original registration. The gate selects
the same 416 MCP/TUI cases without building unrelated integration binaries.
Cargo timing reports are retained as diagnostic artifacts. Caches are used
only by unprivileged build jobs. Privileged jobs
restore no build cache and execute no candidate binaries/scripts.

The package uses upstream's supported resource layout and checksum-verified
V8/ripgrep/zsh downloads, including the code-mode host, Linux bwrap digest, and
Windows sandbox helpers. Linux uses upstream's musl/OpenSSL/libcap setup. The upstream ARM64 ripgrep
manifest currently supplies a GNU-linked helper, so that archive still needs a
glibc-compatible system for ripgrep. The
optional separately distributed voice runtime and desktop applications are not
part of this CLI package. The CLI's regular features and sandboxing remain enabled.

Publication requires success of the entire workflow and exactly five verified
platform artifacts from that run. A trusted coordinator checks manifests,
SHA-256 digests, ancestry and the expected maintained-branch tip, promotes the
exact tested commit and uses immutable `mcp-<upstream-tag>-rN` tags. It creates a
draft, verifies every uploaded asset, then publishes. `release.json`, per-target
manifests and `SHA256SUMS` identify upstream/fork SHAs and the source workflow.
Published assets/tags are never silently overwritten. Failed publication leaves
the last good release intact and the ledger pending.

Use **Fork Coordinator → Run workflow → retry=true** after resolving an external
failure or to recover a missed dispatch/publication. The same request, release
identity and budget are reused as appropriate. Artifacts remain for 30 days;
expired artifacts require a new validated build/revision. For a corrected build
of a published release, explicitly supply its `release_id` and the next
`revision` with an empty backlog. The same explicit revision operation can recover
the first pending `publication_failed` entry after artifact expiration or a partial
draft mismatch: it records the old attempt as superseded by the owner's choice
and preserves that draft/tag and repair budget, then validates a new revision before later releases.
The owner can also provide an exact
`source_sha` for an already-prepared pending release; ancestry and fork policy
are checked before the full validation/build pipeline runs. Review automation changes on `fork-main` before
retrying; policy mismatches stop promotion. Rolling back an installation means
selecting an older immutable archive, not rewriting Git history.

Actions owns the initial build, publication and all subsequent maintenance after
bootstrap. A pending repair or failed check is visible unfinished work, never a
successful release claim. Copilot may require owner help for future changes.
