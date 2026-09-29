"""Route a real packaged CLI's update command through offline installer fixtures."""

import os
import shutil
import subprocess


def smoke(binary):
    if os.name == "nt":
        from test_install_windows import WindowsInstallTest

        fixture = WindowsInstallTest()
    else:
        from test_install import InstallTest

        fixture = InstallTest()
    try:
        fixture.setUp()
        # Both revisions deliberately use the same CLI version, as fork rebuilds do.
        version = (
            subprocess.check_output([str(binary), "--version"], text=True)
            .strip()
            .split()[1]
            .split("+")[0]
        )
        if os.name == "nt":
            # Native Windows fixtures compile a tiny executable for independent checks.
            # Here the packaged binary exercises the actual Rust update dispatch.
            shutil.copy2(binary, fixture.directory / "fixture.exe")
            # The fixture's version check must follow the current upstream baseline.
            fixture.version = version
            fixture.release()
        else:
            fixture.release(version)
        fixture.run_install()
        installed = next((fixture.root / "releases").iterdir())
        command = installed / "bin" / binary.name
        shutil.copy2(binary, command)
        if os.name == "nt":
            # Inject the local downloader only into this temporary installed script.
            # Production scripts accept no URL/repository overrides.
            script = installed / "install/install.ps1"
            text = script.read_text()
            mock = (
                fixture.wrapper.read_text()
                .split("function Invoke-WebRequest", 1)[1]
                .split("& $Installer", 1)[0]
            )
            first, rest = text.split("\n", 1)
            script.write_text(first + "\nfunction Invoke-WebRequest" + mock + rest)
            fixture.release(2)
        else:
            fixture.release(version, 2)
        result = subprocess.run(
            (
                ["cmd.exe", "/d", "/c", str(fixture.bin / "codex-mcp.cmd"), "update"]
                if os.name == "nt"
                else [str(fixture.bin / "codex-mcp"), "update"]
            ),
            env=dict(fixture.env, CODEX_HOME=str(fixture.root / "test-home")),
            capture_output=True,
            text=True,
            timeout=90,
        )
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        if os.name == "nt":
            assert "-r2-" in (fixture.bin / "codex-mcp.version").read_text()
        else:
            assert "-r2-" in fixture.selected().name
        assert command.exists(), "Updating must retain the running version"
        print(
            "Installed native codex-mcp update and official-command coexistence passed"
        )
    finally:
        fixture.doCleanups()
