# Conversation-local MCP server selection

In the interactive CLI, run `/mcp select`. Arrow keys navigate, Space toggles,
Enter saves for the next complete user turn, and Escape cancels the draft.
`/mcp` and `/mcp verbose` continue to show status. The picker includes configured
servers that are disabled, with selection/connection status and locked policy
reasons. Internal/plugin-owned servers are not exposed as configuration toggles.

Example: keep a desktop application registered but inactive by default:

```toml
[mcp_servers.desktop_app]
command = "/absolute/path/to/desktop-app-mcp"
args = ["--stdio"]
enabled = false
startup_timeout_sec = 20
```

Select `desktop_app` with `/mcp select`, press Enter, then send a user message.
Initialization and tool discovery finish before that turn's model requests.
Reading config, opening/canceling the picker, status and unrelated turns do not
launch the disabled application. No persistent tool-schema cache is involved.
This example does not change any existing personal configuration.

The choice persists for the current live conversation. All model requests and
tool calls resulting from one user message use the same choice. Edits are rejected
while a turn runs. A new CLI process, including a cold resume of an old thread,
uses configuration defaults. A live thread retains its choice when viewed again.
Choices are never written to `config.toml` or conversation rollout settings;
other conversations are independent. Children inherit the parent turn's limits
and cannot expand them with the selector.

Ordinary local fork sessions, including resume and fork, use their embedded
app-server instead of attaching to the official shared daemon. Explicit `--remote`
connections and the daemon-wide `agents` overview retain their selected server;
that server must also contain the fork's MCP API to support `/mcp select`.

Deselecting removes tools, resource access and other callable surfaces for the
next turn; stale names also fail at dispatch. The existing owned process stays
alive internally, preserving desktop state. Reselecting reuses a healthy
connection. Adding B does not restart A. Failed/crashed connections can be retried
by applying the selection again. Startup timeout/cancellation remains bounded;
failed servers show unavailable status and cannot advertise usable tools.
Normal conversation cleanup closes owned stdio processes; remote/external
servers are never terminated by selection.

Configured tool filters, approvals, sandbox restrictions, authentication and
administrator requirements still apply. `enabled=false` can be overridden
transiently; a policy-disabled server cannot. Required servers are locked to
their configured availability. Transport/configuration changes use the existing
runtime identity checks and replace only affected connections.

## Patch and regression contract

- `codex-mcp/src/runtime/selection.rs`: pending/active conversation choice,
  validation and passive selector rows.
- `codex-mcp/src/connection_manager.rs`: retained inactive connections and
  serialized incremental publication, with explicit activation readiness.
- `core/src/session/`: apply before turn preparation; reject active-turn edits;
  carry the choice into child configuration. Dispatch uses the filtered runtime.
- App-server `thread/mcp/selection` and TUI `mcp_selection` modules: the existing
  multi-select picker, no config writes. Thread-bound status is passive.
- `core/tests/suite/mcp_server_selection.rs`: real mock-process spawn/PID logs,
  mocked model requests, retention/isolation/shutdown, complete-turn edits,
  startup timeout/cancel, required/policy blocks, remote MCP, crash retry and
  child restrictions.
- `tui/src/bottom_pane/mcp_selection_tests.rs`: navigation, locked rows,
  apply/cancel and rendering snapshot.
- `fork/tests/archive_smoke.py`: extracted native CLI, helper discovery,
  app-server API, local MCP/model, process reuse and fresh-process resume defaults.
- `fork/tests/daemon_isolation_smoke.py`: real TUI startup and selector display
  beside an existing daemon socket, resume/fork pickers, and explicit remote routing.

Run `python3 fork/scripts/validate.py setup` once if the declared toolchain,
`just` or nextest is missing, then `python3 fork/scripts/validate.py tests` from
the repository root. It uses checksum-verified upstream V8 inputs. Cargo dependencies
remain locked. No test may be disconnected, weakened or skipped by a repair agent.
Keep this behavior and independent validation of every owner-enabled release target
across integrations. The current release target is macOS ARM64.
