# Install and update codex-mcp

`codex-mcp` is the unofficial DrHelius fork with conversation-local MCP server
selection. It installs alongside official `codex`. Both commands keep their own
executables, helpers and update paths. The installers never uninstall another
Codex, change its files, or edit your Codex configuration.

New builds target macOS ARM64 only. Instructions for other platforms remain
available for earlier releases that contain their matching archive.

## macOS and Linux

```sh
curl -fsSL https://raw.githubusercontent.com/drhelius/codex/fork-main/fork/install/install.sh | sh
codex-mcp
```

The installer detects ARM64 or x86_64, downloads the latest published stable
**fork** release, verifies GitHub's SHA-256 asset digests and `SHA256SUMS`, and
installs the entire package. If no complete release has been published yet, it
exits without replacing an existing installation.

Packages live in `~/.local/share/codex-mcp/releases/`; an atomic `current`
symlink selects the version. Only `~/.local/bin/codex-mcp` is exposed, so the
internal `codex` and helper names cannot shadow your official installation.
The installer adds that bin directory to your shell profile when necessary.
Open a new terminal afterward, or run `~/.local/bin/codex-mcp` immediately.

Local sessions use the fork's embedded server so an already-running official
daemon cannot take over `/mcp select`. The initial `-r1` release requires
`codex-mcp --no-daemon` for this isolation; later corrected builds apply it by default.

## Windows x86_64

Run in Windows PowerShell 5.1 or PowerShell 7:

```powershell
& ([scriptblock]::Create((Invoke-RestMethod 'https://raw.githubusercontent.com/drhelius/codex/fork-main/fork/install/install.ps1')))
codex-mcp
```

Packages live under `%LOCALAPPDATA%\codex-mcp\releases`. The installer adds
`%LOCALAPPDATA%\codex-mcp\bin` to your user PATH and creates only the
`codex-mcp.cmd` launcher there. It switches an ASCII version pointer atomically; older
running executables remain in their original directories. No administrator
rights or Windows symbolic-link privileges are needed. Open a new terminal to
refresh PATH in other shells.

## Updates, pinned versions and rollback

```sh
codex-mcp update
```

This runs the installed package's bundled fork installer, using its recorded
install location, and selects the latest stable fork release. Rerunning either
installation command also updates the fork. Official upstream update checks are
disabled in fork builds; the fork never switches to OpenAI's release channel.
There is no local background updater. Running sessions keep their current binary;
start a new session to use the update.

For a specific release or rollback, download the same script and run
`sh install.sh --release mcp-rust-v0.159.0-r1`, or on Windows
`./install.ps1 -Release mcp-rust-v0.159.0-r1`, replacing the example with a
published tag from [the fork's releases](https://github.com/drhelius/codex/releases).
Updates are explicit: `codex-mcp update` returns to the latest stable version even
after a rollback. Previous package directories are retained and can be removed
manually once no sessions use them. Published assets are immutable; corrected
builds use a new `-rN` revision.

Optional environment settings, separate from all official `CODEX_*` install settings:

- `CODEX_MCP_INSTALL_ROOT`: absolute package/state directory.
- `CODEX_MCP_BIN_DIR`: absolute directory for the single visible command on macOS/Linux.
  Windows always uses `CODEX_MCP_INSTALL_ROOT\bin`.
- `CODEX_MCP_MODIFY_PATH=0`: leave shell profiles/user PATH unchanged.
- `CODEX_MCP_RELEASE`: release tag, or `latest` (also selectable with `--release`/`-Release`).

An unrelated existing `codex-mcp` command is never overwritten. Concurrent
installers are locked out. An interrupted POSIX installer normally removes its
lock; after an uncatchable termination, remove `install.lock` inside the install
root only after confirming that no installer is running.

## Manual archives and provenance

Verify an archive against `SHA256SUMS`, extract it completely, and run
`bin/codex-mcp` (`bin\codex-mcp.exe` on Windows). Keep `bin/`, `codex-resources/`,
`codex-path/`, `codex-package.json` and `install/` together. Do not put the entire
package's `bin` on PATH: it includes the internal upstream-compatible `codex`
name. Use the installer to obtain a managed, updatable command.

`--version` retains the upstream version format so package/daemon version checks
remain compatible; `--help` identifies `codex-mcp` as unofficial.
`fork-build.json` and release metadata identify the
exact upstream/fork commits and Actions run. macOS binaries are ad-hoc signed,
without Apple notarization or OpenAI signatures; Windows binaries are unsigned.
Linux retains the musl CLI and bwrap sandbox (the ARM64 ripgrep helper requires a
glibc-compatible system).

The two CLIs use ordinary Codex configuration/authentication unless you choose a
separate `CODEX_HOME`. Installation state is always separate. Conversation-local
server selection does not persist to configuration; see [SELECTOR.md](SELECTOR.md).
