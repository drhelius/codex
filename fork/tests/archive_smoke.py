#!/usr/bin/env python3
"""Exercise the shipped CLI and selector through real JSON-RPC and local mocks."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time

POLICY = Path(__file__).resolve().parent


def check(value, message):
    if not value:
        raise AssertionError(message)


class App:
    def __init__(self, binary, directory):
        env = dict(os.environ, CODEX_HOME=str(directory / "home"))
        for name in (
            "OPENAI_API_KEY",
            "CODEX_API_KEY",
            "OPENAI_BASE_URL",
            "CODEX_REMOTE",
            "CODEX_REMOTE_AUTH_TOKEN",
        ):
            env.pop(name, None)
        self.log = (directory / "app.stderr").open("a")
        self.process = subprocess.Popen(
            [str(binary), "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.log,
            text=True,
            env=env,
            cwd=directory,
        )
        self.messages = queue.Queue()
        self.pending = []
        self.sequence = 0

        def read():
            for line in self.process.stdout:
                self.messages.put(json.loads(line))

        threading.Thread(target=read, daemon=True).start()
        self.call(
            "initialize",
            {
                "clientInfo": {"name": "codex_gear_smoke", "version": "1.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        self.send({"method": "initialized", "params": {}})

    def send(self, message):
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def receive(self, predicate):
        for index, message in enumerate(self.pending):
            if predicate(message):
                return self.pending.pop(index)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            message = self.messages.get(timeout=max(0.1, deadline - time.monotonic()))
            if predicate(message):
                return message
            self.pending.append(message)
        raise AssertionError("App-server response timed out")

    def call(self, method, params, error=False):
        self.sequence += 1
        ident = self.sequence
        self.send({"id": ident, "method": method, "params": params})
        response = self.receive(lambda m: m.get("id") == ident)
        check(
            ("error" in response) == error, f"Unexpected {method} response: {response}"
        )
        return response.get("result", response.get("error"))

    def turn(self, thread):
        result = self.call(
            "turn/start",
            {
                "threadId": thread,
                "input": [{"type": "text", "text": "Reply done", "text_elements": []}],
            },
        )
        turn = result["turn"]["id"]
        completed = self.receive(
            lambda m: (
                m.get("method") == "turn/completed"
                and m["params"]["turn"]["id"] == turn
            )
        )
        check(
            completed["params"]["turn"]["status"] == "completed",
            f"Mock turn failed: {completed}",
        )

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=20)
        finally:
            if self.process.poll() is None:
                self.process.kill()
            self.log.close()
        check(self.process.returncode == 0, "App-server did not shut down normally")


def smoke(directory):
    suffix = ".exe" if os.name == "nt" else ""
    binary = directory / "bin" / ("codex-mcp" + suffix)
    output = subprocess.check_output([str(binary), "--version"], text=True)
    package = json.loads((directory / "codex-package.json").read_text())
    check(
        output.strip() == "codex-cli " + package["version"],
        "CLI version does not match its package metadata",
    )
    help_text = subprocess.check_output([str(binary), "--help"], text=True)
    check("Unofficial DrHelius" in help_text, "Fork identity missing")
    check("Usage: codex-mcp" in help_text, "Fork command name missing")
    from daemon_isolation_smoke import smoke as smoke_daemon_isolation

    smoke_daemon_isolation(binary)
    from installer_smoke import smoke as smoke_installer

    smoke_installer(binary)
    for command in (
        [directory / "bin" / ("codex-code-mode-host" + suffix), "--help"],
        [directory / "codex-path" / ("rg" + suffix), "--version"],
    ):
        subprocess.run(list(map(str, command)), check=True, stdout=subprocess.DEVNULL)
    if sys.platform.startswith("linux"):
        subprocess.run(
            [str(directory / "codex-resources/bwrap"), "--version"], check=True
        )
    if os.name != "nt":
        subprocess.run(
            [str(directory / "codex-resources/zsh/bin/zsh"), "-c", "exit 0"], check=True
        )
    else:
        for name in (
            "codex-command-runner.exe",
            "codex-windows-sandbox-setup.exe",
            "codex-windows-sandbox-service.exe",
        ):
            check(
                (directory / "codex-resources" / name).is_file(),
                "Missing Windows sandbox helper",
            )
    with tempfile.TemporaryDirectory(prefix="gear-mock-") as temporary:
        temporary = Path(temporary)
        home = temporary / "home"
        home.mkdir()
        update = subprocess.run(
            [str(binary), "update"],
            env=dict(os.environ, CODEX_HOME=str(home)),
            cwd=temporary,
            capture_output=True,
            text=True,
        )
        check(
            update.returncode != 0
            and "Install codex-mcp using fork/install/install.sh" in update.stderr,
            "An unmanaged archive must direct updates to the fork installer",
        )
        spawns = temporary / "spawns.log"
        requests = []

        class Model(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = b'{"models":[]}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                pids = spawns.read_text().splitlines() if spawns.exists() else []
                requests.append((body, len(pids)))
                events = [
                    {"type": "response.created", "response": {"id": "gear-mock"}},
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "gear-mock",
                            "usage": {
                                "input_tokens": 0,
                                "output_tokens": 0,
                                "total_tokens": 0,
                            },
                        },
                    },
                ]
                payload = "".join(
                    "data: " + json.dumps(event) + "\n\n" for event in events
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        config = f"""model = "mock-model"
model_provider = "gear_mock"
check_for_update_on_startup = false
[model_providers.gear_mock]
name = "Local deterministic mock"
base_url = "http://127.0.0.1:{server.server_port}/v1"
wire_api = "responses"
requires_openai_auth = false
request_max_retries = 0
stream_max_retries = 0
[features]
apps = false
plugins = false
memories = false
[analytics]
enabled = false
[mcp_servers.desktop]
command = {json.dumps(sys.executable)}
args = {json.dumps([str(POLICY / "mock_mcp.py"), str(spawns)])}
enabled = false
startup_timeout_sec = 10
"""
        (home / "config.toml").write_text(config)
        app = App(binary, temporary)
        try:
            thread = app.call(
                "thread/start", {"cwd": str(temporary), "model": "mock-model"}
            )["thread"]["id"]

            def select(names=None):
                params = {"threadId": thread}
                if names is not None:
                    params["selectedServers"] = names
                return app.call("thread/mcp/selection", params)["servers"]

            check(
                not select()[0]["selected"], "Disabled configured server was selected"
            )
            app.call("mcpServerStatus/list", {"threadId": thread})
            check(not spawns.exists(), "Passive display started a process")
            app.turn(thread)
            check(requests[-1][1] == 0, "Unrelated turn started disabled MCP")
            select(["desktop"])
            check(
                not spawns.exists(), "Selection started a process before the next turn"
            )
            app.turn(thread)
            check(requests[-1][1] == 1, "MCP was not initialized before model request")
            check(spawns.read_text().count("\n") == 1, "Duplicate process")
            tool = {
                "threadId": thread,
                "server": "desktop",
                "tool": "echo",
                "arguments": {"message": "ok"},
            }
            check(
                "gear-smoke:ok" in json.dumps(app.call("mcpServer/tool/call", tool)),
                "Packaged MCP call failed",
            )
            select([])
            app.turn(thread)
            app.call("mcpServer/tool/call", tool, error=True)
            select(["desktop"])
            app.turn(thread)
            check(
                spawns.read_text().count("\n") == 1,
                "Reselection restarted the live process",
            )
        finally:
            app.close()
        # A new CLI process resuming the same conversation must restore configured defaults.
        resumed = App(binary, temporary)
        try:
            resumed.call("thread/resume", {"threadId": thread, "cwd": str(temporary)})
            rows = resumed.call("thread/mcp/selection", {"threadId": thread})["servers"]
            check(
                not rows[0]["selected"],
                "Conversation selection leaked into cold resume",
            )
            check(
                spawns.read_text().count("\n") == 1,
                "Cold resume launched the disabled app",
            )
        finally:
            resumed.close()
            server.shutdown()
        check(
            (home / "config.toml").read_text() == config,
            "Selector changed configuration",
        )
    print(
        "Extracted archive: helpers, startup, mock MCP, retention and cold resume passed"
    )


if __name__ == "__main__":
    smoke(Path(sys.argv[1]).resolve())
