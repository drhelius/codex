"""Offline installer fixtures; never access GitHub or the user's installation."""

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

INSTALL = Path(__file__).resolve().parents[1] / "install"


@unittest.skipIf(os.name == "nt", "POSIX installer; Windows fixtures run separately")
class InstallTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mcp-install-")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "home with spaces and ' quotes"
        self.home.mkdir()
        self.mocks = self.home / "mocks"
        self.mocks.mkdir()
        self.remote = self.home / "remote"
        self.remote.mkdir()
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self.root = self.home / "packages"
        self.official = self.bin / "codex"
        self.executable(self.official, "#!/bin/sh\necho official-codex\n")
        self.executable(
            self.bin / "codex-code-mode-host", "#!/bin/sh\necho official-helper\n"
        )
        self.executable(
            self.mocks / "curl",
            """#!/usr/bin/env python3
import os, pathlib, shutil, sys
args = sys.argv[1:]
url = args[-1]
root = pathlib.Path(os.environ['MOCK_RELEASES'])
with (root / 'requests').open('a') as log:
    log.write(url + '\\n')
prefix = 'https://api.github.com/repos/drhelius/codex/releases/'
if url.startswith(prefix):
    endpoint = url[len(prefix):]
    tag = (root / 'latest').read_text() if endpoint == 'latest' else endpoint.removeprefix('tags/')
    source = root / tag / 'api.json'
else:
    prefix = 'https://github.com/drhelius/codex/releases/download/'
    if not url.startswith(prefix):
        sys.exit('Unexpected network request: ' + url)
    source = root / url[len(prefix):]
if not source.is_file():
    sys.exit(22)
shutil.copyfile(source, args[args.index('--output') + 1])
""",
        )
        self.env = dict(
            os.environ,
            HOME=str(self.home),
            SHELL="/bin/sh",
            CODEX_MCP_INSTALL_ROOT=str(self.root),
            CODEX_MCP_BIN_DIR=str(self.bin),
            CODEX_MCP_MODIFY_PATH="0",
            MOCK_RELEASES=str(self.remote),
            PATH=str(self.mocks) + os.pathsep + os.environ["PATH"],
        )
        self.env.pop("CODEX_MCP_RELEASE", None)
        machine = os.uname().machine
        arch = "aarch64" if machine in ("aarch64", "arm64") else "x86_64"
        platform = (
            "apple-darwin" if os.uname().sysname == "Darwin" else "unknown-linux-musl"
        )
        self.target = arch + "-" + platform
        self.asset = "codex-mcp-" + self.target + ".tar.gz"

    def executable(self, path, text):
        path.write_text(text)
        path.chmod(0o755)

    def release(self, version="0.159.0", revision=1, missing=None, unsafe=False):
        tag = f"mcp-rust-v{version}-r{revision}"
        directory = self.remote / tag
        directory.mkdir(exist_ok=True)
        files = {
            name: b"fixture"
            for name in ("LICENSE", "codex-package.json", "fork-build.json")
        }
        for name in (
            "bin/codex",
            "bin/codex-mcp",
            "bin/codex-code-mode-host",
            "bin/codex-responses-api-proxy",
            "codex-path/rg",
            "codex-resources/bwrap",
            "codex-resources/zsh/bin/zsh",
        ):
            files[name] = f'#!/bin/sh\necho "codex-cli {version}"\n'.encode()
        for name in ("install.sh", "install.ps1"):
            files["install/" + name] = (INSTALL / name).read_bytes()
        files.pop(missing, None)
        if unsafe:
            files["../../escaped"] = b"unsafe"
        with tarfile.open(directory / self.asset, "w:gz") as archive:
            for name, data in files.items():
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                entry.mode = 0o755 if data.startswith(b"#!") else 0o644
                archive.addfile(entry, io.BytesIO(data))
        digest = hashlib.sha256((directory / self.asset).read_bytes()).hexdigest()
        (directory / "SHA256SUMS").write_text(f"{digest}  {self.asset}\n")
        metadata = {
            "tag_name": tag,
            "draft": False,
            "prerelease": False,
            "body": 'Decoy: "tag_name": "bad", "assets": [{"digest":"bad"}]',
            "assets": [
                {
                    "digest": "sha256:"
                    + hashlib.sha256((directory / name).read_bytes()).hexdigest(),
                    "name": name,
                }
                for name in (self.asset, "SHA256SUMS")
            ],
        }
        (directory / "api.json").write_text(json.dumps(metadata, separators=(",", ":")))
        (self.remote / "latest").write_text(tag)
        return tag

    def run_install(self, *args, success=True):
        result = subprocess.run(
            ["sh", str(INSTALL / "install.sh"), *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        self.assertEqual(self.official.read_text(), "#!/bin/sh\necho official-codex\n")
        return result

    def selected(self):
        return (self.root / "current").resolve()

    def test_install_reinstall_update_and_pinned_rollback_keep_official_codex(self):
        first = self.release()
        self.run_install()
        original = self.selected()
        self.assertEqual((self.bin / "codex-mcp").resolve(), original / "bin/codex-mcp")
        self.run_install()
        self.assertEqual(self.selected(), original)
        self.release("0.160.0", 2)
        self.run_install()
        self.assertNotEqual(self.selected(), original)
        self.assertTrue((original / "bin/codex-mcp").exists())
        self.run_install("--release", first)
        self.assertEqual(self.selected(), original)
        self.assertEqual(
            sorted(p.name for p in self.bin.iterdir()),
            ["codex", "codex-code-mode-host", "codex-mcp"],
        )

    def test_bad_checksum_incomplete_package_and_traversal_preserve_current(self):
        self.release()
        self.run_install()
        original = self.selected()
        tag = self.release("0.160.0")
        with (self.remote / tag / self.asset).open("ab") as archive:
            archive.write(b"corruption")
        self.assertIn("Checksum mismatch", self.run_install(success=False).stderr)
        self.release("0.161.0", missing="bin/codex-code-mode-host")
        self.assertIn("Incomplete package", self.run_install(success=False).stderr)
        self.release("0.162.0", unsafe=True)
        self.assertIn("Unsafe archive", self.run_install(success=False).stderr)
        tag = self.release("0.163.0")
        metadata = self.remote / tag / "api.json"
        data = json.loads(metadata.read_text())
        data["prerelease"] = True
        metadata.write_text(json.dumps(data))
        self.assertIn("prerelease", self.run_install(success=False).stderr)
        self.assertEqual(self.selected(), original)
        self.assertFalse((self.root / "escaped").exists())

    def test_no_release_and_locked_install_fail_without_touching_commands(self):
        (self.remote / "latest").write_text("missing")
        self.run_install(success=False)
        self.assertFalse((self.bin / "codex-mcp").exists())
        self.release()
        (self.root / "install.lock").mkdir()
        self.assertIn("locked", self.run_install(success=False).stderr)
        self.assertTrue((self.root / "install.lock").exists())

    def test_unrelated_fork_command_and_invalid_tag_are_rejected(self):
        self.release()
        self.executable(self.bin / "codex-mcp", "#!/bin/sh\necho unrelated\n")
        self.assertIn("unrelated", self.run_install(success=False).stderr)
        self.run_install("--release", "../../unsafe", success=False)
        self.assertFalse((self.remote / "requests").exists())

    def test_path_configuration_quotes_paths_and_is_idempotent(self):
        self.release()
        self.env["CODEX_MCP_MODIFY_PATH"] = "1"
        self.run_install()
        self.run_install()
        profile = (self.home / ".profile").read_text()
        self.assertEqual(profile.count("# codex-mcp installer"), 1)
        result = subprocess.check_output(
            ["sh", "-c", '. "$HOME/.profile"; codex-mcp --version; codex --version'],
            env=self.env,
            text=True,
        )
        self.assertEqual(result, "codex-cli 0.159.0\nofficial-codex\n")


if __name__ == "__main__":
    unittest.main()
