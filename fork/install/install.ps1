param([string]$Release = $(if ($env:CODEX_MCP_RELEASE) { $env:CODEX_MCP_RELEASE } else { 'latest' }))
# Windows PowerShell 5.1 and PowerShell 7. No administrator rights are needed.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
# An intermediate CLI/Python process can pass PowerShell 7's module paths to
# Windows PowerShell 5.1. Load hashing and web helpers from this runtime explicitly.
Import-Module "$PSHOME/Modules/Microsoft.PowerShell.Utility"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
if ($env:OS -ne 'Windows_NT' -or $env:PROCESSOR_ARCHITECTURE -notin @('AMD64', 'x86') -or
    -not [Environment]::Is64BitOperatingSystem) { throw 'This installer requires Windows x86_64.' }
if ($Release -ne 'latest' -and $Release -cnotmatch '^mcp-rust-v\d+\.\d+\.\d+-r[1-9]\d*$') {
    throw 'Invalid stable fork release tag.'
}
$root = if ($env:CODEX_MCP_INSTALL_ROOT) { $env:CODEX_MCP_INSTALL_ROOT } else { Join-Path $env:LOCALAPPDATA 'codex-mcp' }
$binDir = Join-Path $root 'bin'
if ($env:CODEX_MCP_BIN_DIR -and [IO.Path]::GetFullPath($env:CODEX_MCP_BIN_DIR) -ine [IO.Path]::GetFullPath($binDir)) {
    throw 'On Windows, the command directory must be INSTALL_ROOT\bin; customize CODEX_MCP_INSTALL_ROOT instead.'
}
foreach ($path in @($root, $binDir)) {
    if ($path -notmatch '^(?:[A-Za-z]:[\\/]|\\\\)' -or $path -match '[\r\n;]') {
        throw 'Install paths must be absolute and cannot contain newlines or semicolons.'
    }
}
$root = [IO.Path]::GetFullPath($root).TrimEnd('\')
$binDir = [IO.Path]::GetFullPath($binDir).TrimEnd('\')
$commandPath = Join-Path $binDir 'codex-mcp.cmd'
$selectionPath = Join-Path $binDir 'codex-mcp.version'
$rootHash = [Security.Cryptography.SHA256]::Create()
try { $rootId = [BitConverter]::ToString($rootHash.ComputeHash([Text.Encoding]::UTF8.GetBytes($root.ToUpperInvariant()))).Replace('-', '') }
finally { $rootHash.Dispose() }
$marker = "@rem codex-mcp installer: $rootId"
[IO.Directory]::CreateDirectory($root) | Out-Null
[IO.Directory]::CreateDirectory($binDir) | Out-Null
if ((Test-Path $commandPath) -and (Get-Content $commandPath -TotalCount 1) -cne $marker) {
    throw "Refusing to replace unrelated $commandPath"
}
if ((Test-Path $selectionPath) -and -not (Test-Path $commandPath)) { throw 'Refusing to replace an unrelated codex-mcp.version file.' }
foreach ($extension in @('exe', 'bat', 'ps1')) {
    if (Test-Path (Join-Path $binDir "codex-mcp.$extension")) { throw 'A conflicting codex-mcp command already exists.' }
}
# An OS-owned lock is released even if the installer is interrupted.
$lock = [IO.File]::Open((Join-Path $root 'install.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
$temporary = Join-Path $root ('.staging.' + [Guid]::NewGuid().ToString('N'))
try {
    [IO.Directory]::CreateDirectory($temporary) | Out-Null
    function Download([string]$Uri, [string]$Destination) {
        Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile $Destination -TimeoutSec 600 `
            -Headers @{ 'User-Agent' = 'codex-mcp-installer' } | Out-Null
    }
    function Verify([string]$Path, [string]$Digest) {
        if ($Digest -cnotmatch '^[0-9a-f]{64}$' -or (Get-FileHash $Path -Algorithm SHA256).Hash.ToLowerInvariant() -cne $Digest) {
            throw "Missing or mismatched SHA-256 digest: $Path"
        }
    }
    $endpoint = if ($Release -eq 'latest') { 'latest' } else { "tags/$Release" }
    $metadataFile = Join-Path $temporary 'release.json'
    Download "https://api.github.com/repos/drhelius/codex/releases/$endpoint" $metadataFile
    $metadata = Get-Content $metadataFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $tag = $metadata.tag_name
    if ($metadata.draft -or $metadata.prerelease -or $tag -cnotmatch '^mcp-rust-v\d+\.\d+\.\d+-r[1-9]\d*$' -or
        ($Release -ne 'latest' -and $Release -cne $tag)) { throw 'GitHub did not return the requested stable fork release.' }
    function Asset-Digest([string]$Name) {
        $assets = @($metadata.assets | Where-Object { $_.name -ceq $Name })
        if ($assets.Count -ne 1 -or $assets[0].digest -cnotmatch '^sha256:[0-9a-f]{64}$') { throw "Missing release asset digest: $Name" }
        return $assets[0].digest.Substring(7)
    }
    $target = 'x86_64-pc-windows-msvc'
    $asset = "codex-mcp-$target.zip"
    $base = "https://github.com/drhelius/codex/releases/download/$tag"
    $checksums = Join-Path $temporary 'SHA256SUMS'
    Download "$base/SHA256SUMS" $checksums
    Verify $checksums (Asset-Digest 'SHA256SUMS')
    $matching = @(Get-Content $checksums | Where-Object { $_ -cmatch ('^[0-9a-f]{64}  ' + [regex]::Escape($asset) + '$') })
    if ($matching.Count -ne 1) { throw 'Missing or duplicate archive checksum.' }
    $digest = $matching[0].Substring(0, 64)
    if ($digest -cne (Asset-Digest $asset)) { throw 'Archive digest does not match release metadata.' }
    $archive = Join-Path $temporary $asset
    Download "$base/$asset" $archive
    Verify $archive $digest
    $package = Join-Path $temporary 'package'
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($archive)
    try {
        foreach ($entry in $zip.Entries) {
            if ($entry.FullName -match '(^[/\\]|(^|[/\\])\.\.([/\\]|$)|:)' -or
                (($entry.ExternalAttributes -shr 16) -band 0xF000) -eq 0xA000) { throw 'Unsafe archive entry.' }
        }
    } finally { $zip.Dispose() }
    [IO.Compression.ZipFile]::ExtractToDirectory($archive, $package)
    foreach ($file in @('bin/codex.exe', 'bin/codex-mcp.exe', 'bin/codex-code-mode-host.exe',
            'bin/codex-responses-api-proxy.exe', 'codex-path/rg.exe',
            'codex-resources/codex-command-runner.exe', 'codex-resources/codex-windows-sandbox-setup.exe',
            'codex-resources/codex-windows-sandbox-service.exe', 'codex-package.json', 'fork-build.json',
            'LICENSE', 'install/install.sh', 'install/install.ps1')) {
        if (-not (Test-Path (Join-Path $package $file) -PathType Leaf)) { throw "Incomplete package: $file" }
    }
    $version = ($tag -replace '^mcp-rust-v', '') -replace '-r\d+$', ''
    $reported = & (Join-Path $package 'bin/codex-mcp.exe') --version
    if ($LASTEXITCODE -ne 0 -or $reported -cne "codex-cli $version") { throw 'Unexpected fork executable version.' }
    $utf8 = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText((Join-Path $package '.codex-mcp-bin-dir'), "$binDir`n", $utf8)
    [IO.File]::WriteAllText((Join-Path $package '.archive-sha256'), "$digest`n", $utf8)
    $releases = Join-Path $root 'releases'
    [IO.Directory]::CreateDirectory($releases) | Out-Null
    $destination = Join-Path $releases "$tag-$target"
    if (Test-Path $destination) {
        if (((Get-Item $destination).Attributes -band [IO.FileAttributes]::ReparsePoint) -or
            (Get-Content (Join-Path $destination '.archive-sha256') -Raw -Encoding UTF8).TrimEnd("`r", "`n") -cne $digest -or
            (Get-Content (Join-Path $destination '.codex-mcp-bin-dir') -Raw -Encoding UTF8).TrimEnd("`r", "`n") -cne $binDir) {
            throw "Refusing to overwrite $destination"
        }
        $reported = & (Join-Path $destination 'bin/codex-mcp.exe') --version
        if ($LASTEXITCODE -ne 0 -or $reported -cne "codex-cli $version") { throw 'Existing release is damaged; move it aside before reinstalling.' }
    } else { [IO.Directory]::Move($package, $destination) }
    # A stable ASCII launcher resolves its own Unicode directory via %~dp0.
    # Only the version pointer changes during updates, including self-updates.
    $launcher = @"
$marker
@echo off
setlocal DisableDelayedExpansion
set /p "CODEX_MCP_RELEASE_DIR="<"%~dp0codex-mcp.version"
"%~dp0..\releases\%CODEX_MCP_RELEASE_DIR%\bin\codex-mcp.exe" %*
exit /b %errorlevel%
"@ -replace "`r?`n", "`r`n"
    $stagedSelection = Join-Path $binDir ('.codex-mcp.' + [Guid]::NewGuid().ToString('N'))
    [IO.File]::WriteAllText($stagedSelection, "$tag-$target`r`n", [Text.Encoding]::ASCII)
    try {
        # Windows PowerShell 5.1 converts a null String argument to an empty
        # path. Keep the atomic replacement and use a real temporary backup.
        if (Test-Path $selectionPath) { [IO.File]::Replace($stagedSelection, $selectionPath, (Join-Path $temporary 'previous-version')) }
        else { [IO.File]::Move($stagedSelection, $selectionPath) }
    } finally { if (Test-Path $stagedSelection) { Remove-Item $stagedSelection } }
    if (-not (Test-Path $commandPath)) {
        try {
            [IO.File]::WriteAllText($stagedSelection, $launcher + "`r`n", [Text.Encoding]::ASCII)
            [IO.File]::Move($stagedSelection, $commandPath)
        } finally { if (Test-Path $stagedSelection) { Remove-Item $stagedSelection } }
    }
    if ($env:CODEX_MCP_MODIFY_PATH -ne '0') {
        $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
        if ($binDir -notin ($userPath -split ';')) {
            [Environment]::SetEnvironmentVariable('Path', "$binDir;$userPath", 'User')
        }
        if ($binDir -notin ($env:PATH -split ';')) { $env:PATH = "$binDir;$env:PATH" }
    }
    Write-Host "Installed $tag. Open a new terminal and run codex-mcp. Update: codex-mcp update"
} finally {
    try { if (Test-Path $temporary) { Remove-Item $temporary -Recurse -Force } }
    finally { $lock.Dispose() }
}
