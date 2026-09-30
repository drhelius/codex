#!/usr/bin/env python3
"""Exercise the real fork TUI beside a socket owned by another local server."""

import json
import os
from pathlib import Path
import re
import select
import socket
import subprocess
import sys
import tempfile
import time


def smoke(binary):
    if os.name == "nt":
        return  # The active release target is macOS ARM64; this harness uses a Unix PTY.
    import fcntl
    import pty
    import struct
    import termios

    binary = Path(binary).resolve()
    # Keep the default Unix socket below macOS's path-length limit.
    with tempfile.TemporaryDirectory(prefix="mcp-tui-", dir="/tmp") as directory:
        root = Path(directory).resolve()
        home = root / "home"
        home.mkdir()
        spawns = root / "spawns.log"
        config = f"""model = "gpt-6-sol"
model_provider = "local_fixture"
check_for_update_on_startup = false
suppress_unstable_features_warning = true
analytics.enabled = false
tui.disable_paste_burst = true
tui.screen_reader_detection_done = true
# Model announcement counters are unrelated to the MCP configuration invariant.
tui.show_tooltips = false
features.daemon_auto_start = true
[model_providers.local_fixture]
name = "Local fixture without inference"
base_url = "http://127.0.0.1:9/v1"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
request_max_retries = 0
[projects.{json.dumps(str(root))}]
trust_level = "trusted"
[mcp_servers.desktop]
command = {json.dumps(sys.executable)}
args = {json.dumps([str(Path(__file__).with_name("mock_mcp.py")), str(spawns)])}
enabled = false
"""
        (home / "config.toml").write_text(config)
        state = home / "app-server-daemon"
        state.mkdir()
        settings = state / "settings.json"
        settings.write_text('{"updater":{"autoUpdateEnabled":false}}\n')
        socket_dir = home / "app-server-control"
        socket_dir.mkdir(mode=0o700)
        socket_path = socket_dir / "app-server-control.sock"
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(root),
            "CODEX_HOME": str(home),
            "CODEX_SQLITE_HOME": str(home),
            "TERM": "xterm-256color",
            "TERM_PROGRAM": "kitty",
            "LANG": "en_US.UTF-8",
        }
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(socket_path))
            listener.listen()
            before = {p.name: p.read_bytes() for p in state.iterdir()}
            # Explicit --remote must still connect to the requested server. Local
            # new/resume/fork launches must neither probe it nor mutate its state.
            for args in (
                [],
                ["resume"],
                ["fork"],
                ["--remote", "unix://" + str(socket_path)],
            ):
                remote = "--remote" in args
                master, slave = pty.openpty()
                fcntl.ioctl(
                    slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 160, 0, 0)
                )
                process = subprocess.Popen(
                    [str(binary), *args, "--no-alt-screen", "-C", str(root)],
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    cwd=root,
                    env=env,
                )
                os.close(slave)
                output = bytearray()
                queries = {
                    b"\x1b[6n": b"\x1b[1;1R",
                    b"\x1b[?u": b"\x1b[?0u\x1b[?1;2c",
                    b"\x1b[c": b"\x1b[?1;2c",
                    b"\x1b]10;?": b"\x1b]10;rgb:ffff/ffff/ffff\x1b\\",
                    b"\x1b]11;?": b"\x1b]11;rgb:0000/0000/0000\x1b\\",
                }
                picker_cancelled = not args
                command_sent = False
                passed = False
                try:
                    deadline = time.monotonic() + 45
                    while time.monotonic() < deadline:
                        ready, _, _ = select.select([master, listener], [], [], 0.1)
                        if listener in ready:
                            connection, _ = listener.accept()
                            connection.close()
                            if not remote:
                                raise AssertionError(
                                    f"Local fork contacted the shared daemon: {args}"
                                )
                            passed = True
                            break
                        if master not in ready:
                            if process.poll() is not None:
                                break
                            continue
                        try:
                            output.extend(os.read(master, 65536))
                        except OSError:
                            break
                        for query, response in list(queries.items()):
                            if query in output:
                                os.write(master, response)
                                del queries[query]
                        text = re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", bytes(output))
                        text = re.sub(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", b"", text)
                        compact = b"".join(text.split())
                        if remote:
                            continue
                        # The picker discards input after its first frame. Wait
                        # for the empty-list response ("No sessions yet"); its
                        # incremental repaint can omit unchanged characters.
                        if (
                            not picker_cancelled
                            and b"yet" in compact
                            and (
                                b"Resumeaprevioussession" in compact
                                or b"Forkaprevioussession" in compact
                            )
                        ):
                            os.write(master, b"\x1b[27u")
                            picker_cancelled = True
                            output.clear()
                        elif (
                            picker_cancelled
                            and not command_sent
                            and b"OpenAICodex" in compact
                        ):
                            os.write(master, b"/mcp select\r")
                            command_sent = True
                            output.clear()
                        elif (
                            command_sent
                            and b"Appliestothenextcompleteturn" in compact
                            and b"desktop" in compact
                        ):
                            os.write(master, b"\x1b[27u")
                            passed = True
                            break
                    if not passed:
                        raise AssertionError(
                            f"Fork TUI isolation failed for {args}: {output.decode(errors='replace')}"
                        )
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    os.close(master)
                assert not spawns.exists(), "The disabled desktop server was started"
                actual_config = (home / "config.toml").read_text()
                assert actual_config == config, (
                    f"TUI changed fixture configuration: {actual_config}"
                )
                assert {p.name: p.read_bytes() for p in state.iterdir()} == before, (
                    "Shared daemon state changed"
                )
                assert not (home / "packages/app-server-daemon").exists(), (
                    "An official daemon package was installed"
                )
    print(
        "Fork TUI startup, resume/fork pickers, MCP selection and explicit remote routing passed"
    )


if __name__ == "__main__":
    smoke(Path(sys.argv[1]))
