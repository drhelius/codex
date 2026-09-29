# Install Codex Gear alongside official Codex

Download the archive for your OS/architecture from
<https://github.com/drhelius/codex/releases> and verify its SHA-256 against
`SHA256SUMS`. Extract the **entire archive** into a new versioned directory.
Keep `bin/`, `codex-resources/`, `codex-path/` and `codex-package.json` together.
Do not move or copy only the main executable.

Run `bin/codex-gear` (Windows: `bin\codex-gear.exe`). Add that package's `bin`
directory to PATH only if you also intend to expose its upstream-compatible
`codex` helper name. To preserve an existing `codex` command, prefer a shell alias
pointing specifically to the absolute `bin/codex-gear` path, or invoke that path.
Nothing in these archives modifies or uninstalls another Codex installation.

`codex-gear --version` retains a single version line with valid `+gear` SemVer
metadata; `--help` identifies the unofficial DrHelius fork. Fork builds
disable the upstream update prompt/check and reject `codex-gear update`, directing
you to this fork's releases without modifying shared configuration. Download a
new version into another directory and update your alias/shortcut manually.
No npm/Homebrew updater or system installer is published by this fork.

macOS ARM64/x86_64 archives are locally ad-hoc signed, without Apple notarization
or an OpenAI signature. Windows binaries are not Authenticode signed. Linux uses
musl and retains Codex's bwrap sandbox. No upstream signing claim is made. Review
your operating system's downloaded-software prompt before launching.

The CLI still uses its ordinary Codex configuration/authentication unless you
choose an isolated `CODEX_HOME`. Conversation-local server selection does not
persist to that configuration. See `fork/SELECTOR.md` in the source repository.

For rollback, keep the previous directory and point your alias/shortcut back to
its `bin/codex-gear`. Release tags and assets are immutable; corrected builds use
an explicitly incremented `-rN` revision. `fork-build.json` inside each archive
and release metadata identify the source commit and exact Actions run.
