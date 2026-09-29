"""Exercise the Windows installer with local release fixtures on native Windows."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile

INSTALL = Path(__file__).resolve().parents[1] / "install"
POWERSHELL = str(
    Path(os.environ.get("SystemRoot", "C:/Windows"))
    / "System32/WindowsPowerShell/v1.0/powershell.exe"
)


@unittest.skipUnless(
    os.name == "nt", "Runs on the required native Windows release gate"
)
class WindowsInstallTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="codex mcp unicode-ñ ")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.root = self.directory / "packages"
        self.bin = self.root / "bin"
        self.bin.mkdir(parents=True)
        (self.bin / "codex.cmd").write_text("@echo official-codex\n")
        self.env = dict(
            os.environ,
            CODEX_MCP_INSTALL_ROOT=str(self.root),
            CODEX_MCP_BIN_DIR=str(self.bin),
            CODEX_MCP_MODIFY_PATH="0",
            MOCK_RELEASES=str(self.directory),
        )
        self.env.pop("CODEX_MCP_RELEASE", None)
        # Small real executable: no model, desktop app or compiler download.
        subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-Command",
                "Add-Type -TypeDefinition 'public class Fixture { public static void Main() { System.Console.WriteLine(\"codex-cli 0.159.0+gear\"); } }' "
                '-OutputAssembly (Join-Path $env:MOCK_RELEASES "fixture.exe") -OutputType ConsoleApplication',
            ],
            env=self.env,
            check=True,
            capture_output=True,
        )
        self.wrapper = self.directory / "run.ps1"
        self.wrapper.write_text("""param([string]$Installer, [string]$Release = 'latest')
$ErrorActionPreference = 'Stop'
function Invoke-WebRequest {
    param($Uri, $OutFile, $TimeoutSec, $Headers, [switch]$UseBasicParsing)
    Add-Content (Join-Path $env:MOCK_RELEASES 'requests') $Uri
    $map = Get-Content (Join-Path $env:MOCK_RELEASES 'urls.json') -Raw | ConvertFrom-Json
    $property = $map.PSObject.Properties[$Uri]
    if (-not $property) { throw "Unexpected request: $Uri" }
    Copy-Item -LiteralPath $property.Value -Destination $OutFile
}
& $Installer -Release $Release
""")
        self.urls = {}
        self.asset = "codex-mcp-x86_64-pc-windows-msvc.zip"

    def release(self, revision=1, missing=None):
        tag = f"mcp-rust-v{getattr(self, 'version', '0.159.0')}-r{revision}"
        directory = self.directory / tag
        directory.mkdir()
        names = (
            "bin/codex.exe",
            "bin/codex-mcp.exe",
            "bin/codex-code-mode-host.exe",
            "bin/codex-responses-api-proxy.exe",
            "codex-path/rg.exe",
            "codex-resources/codex-command-runner.exe",
            "codex-resources/codex-windows-sandbox-setup.exe",
            "codex-resources/codex-windows-sandbox-service.exe",
            "LICENSE",
            "codex-package.json",
            "fork-build.json",
        )
        with zipfile.ZipFile(directory / self.asset, "w") as archive:
            for name in names:
                if name != missing:
                    if name == "bin/codex-mcp.exe":
                        archive.write(self.directory / "fixture.exe", name)
                    else:
                        archive.writestr(name, b"fixture")
            for name in ("install.sh", "install.ps1"):
                archive.write(INSTALL / name, "install/" + name)
        digest = hashlib.sha256((directory / self.asset).read_bytes()).hexdigest()
        (directory / "SHA256SUMS").write_text(f"{digest}  {self.asset}\n")
        metadata = {
            "tag_name": tag,
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "name": name,
                    "digest": "sha256:"
                    + hashlib.sha256((directory / name).read_bytes()).hexdigest(),
                }
                for name in (self.asset, "SHA256SUMS")
            ],
        }
        (directory / "api.json").write_text(json.dumps(metadata))
        prefix = "https://api.github.com/repos/drhelius/codex/releases/"
        self.urls[prefix + "latest"] = str(directory / "api.json")
        self.urls[prefix + "tags/" + tag] = str(directory / "api.json")
        for name in (self.asset, "SHA256SUMS"):
            self.urls[
                f"https://github.com/drhelius/codex/releases/download/{tag}/{name}"
            ] = str(directory / name)
        (self.directory / "urls.json").write_text(json.dumps(self.urls))
        return tag

    def run_install(self, release="latest", success=True):
        result = subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.wrapper),
                str(INSTALL / "install.ps1"),
                release,
            ],
            env=self.env,
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        self.assertEqual((self.bin / "codex.cmd").read_text(), "@echo official-codex\n")
        return result

    def test_install_update_reinstall_rollback_and_integrity_failure(self):
        first = self.release()
        self.run_install()
        initial = (self.bin / "codex-mcp.version").read_bytes()
        launcher = (self.bin / "codex-mcp.cmd").read_bytes()
        result = subprocess.check_output(
            ["cmd.exe", "/d", "/c", str(self.bin / "codex-mcp.cmd"), "--version"],
            text=True,
        )
        self.assertEqual(result.strip(), "codex-cli 0.159.0+gear")
        self.run_install()
        self.assertEqual((self.bin / "codex-mcp.version").read_bytes(), initial)
        self.release(2)
        self.run_install()
        self.assertNotEqual((self.bin / "codex-mcp.version").read_bytes(), initial)
        self.assertEqual((self.bin / "codex-mcp.cmd").read_bytes(), launcher)
        self.run_install(first)
        self.assertEqual((self.bin / "codex-mcp.version").read_bytes(), initial)
        tag = self.release(3)
        with (self.directory / tag / self.asset).open("ab") as stream:
            stream.write(b"bad digest")
        self.run_install(success=False)
        self.assertEqual((self.bin / "codex-mcp.version").read_bytes(), initial)
        self.release(4, missing="codex-resources/codex-windows-sandbox-service.exe")
        self.run_install(success=False)
        self.assertEqual((self.bin / "codex-mcp.version").read_bytes(), initial)

    def test_refuse_unrelated_command_and_invalid_tag(self):
        self.release()
        (self.bin / "codex-mcp.cmd").write_text("@echo unrelated\n")
        self.run_install(success=False)
        self.run_install("../../unsafe", success=False)
        self.assertFalse((self.directory / "requests").exists())


if __name__ == "__main__":
    unittest.main()
