use anyhow::Context;
use anyhow::ensure;
use codex_utils_absolute_path::AbsolutePathBuf;
use std::path::Path;
use std::process::Command;

pub(crate) fn run() -> anyhow::Result<()> {
    let executable =
        AbsolutePathBuf::from_absolute_path(std::env::current_exe()?)?.canonicalize()?;
    let package = executable
        .as_path()
        .parent()
        .and_then(Path::parent)
        .context("Cannot locate the codex-mcp package")?;
    let bin_dir = std::fs::read_to_string(package.join(".codex-mcp-bin-dir")).context(
        "Install codex-mcp using fork/install/install.sh (Windows: install.ps1) before updating: https://github.com/drhelius/codex/blob/fork-main/fork/INSTALL.md",
    )?;
    let bin_dir = Path::new(bin_dir.trim_end_matches(['\r', '\n']));
    let releases = package.parent().context("Missing releases directory")?;
    ensure!(
        releases.file_name().is_some_and(|name| name == "releases") && bin_dir.is_absolute(),
        "Unrecognized codex-mcp installation; rerun the fork installer"
    );
    let root = releases.parent().context("Missing installation root")?;
    #[cfg(unix)]
    let mut command = {
        let mut command = Command::new("/bin/sh");
        command.arg(package.join("install/install.sh"));
        command.args(["--release", "latest"]);
        command
    };
    #[cfg(windows)]
    let mut command = {
        let system_root = std::env::var_os("SystemRoot").context("SystemRoot is not set")?;
        let mut command = Command::new(
            Path::new(&system_root).join("System32/WindowsPowerShell/v1.0/powershell.exe"),
        );
        command.args([
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
        ]);
        command.arg(package.join("install/install.ps1"));
        command.args(["-Release", "latest"]);
        command
    };
    let status = command
        .env("CODEX_MCP_INSTALL_ROOT", root)
        .env("CODEX_MCP_BIN_DIR", bin_dir)
        .env("CODEX_MCP_MODIFY_PATH", "0")
        .status()
        .context("Could not start the bundled codex-mcp installer")?;
    ensure!(status.success(), "codex-mcp update failed ({status})");
    Ok(())
}
