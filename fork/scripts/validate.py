#!/usr/bin/env python3
"""Unprivileged release gates and canonical upstream package assembly."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile

POLICY = Path(__file__).resolve().parents[2]
ROOT = Path(os.environ.get("CODEX_REPO_ROOT", Path.cwd())).resolve()
RS = ROOT / "codex-rs"
TARGET = os.environ.get("TARGET", "")
IDENTITY = Path(os.environ.get("FORK_IDENTITY", ROOT / "identity.json"))


def run(*args, cwd=RS):
    print("+", " ".join(map(str, args)), flush=True)
    subprocess.run(list(map(str, args)), cwd=cwd, check=True)


def setup():
    toolchain = tomllib.loads((RS / "rust-toolchain.toml").read_text())["toolchain"][
        "channel"
    ]
    if not re.fullmatch(r"\d+\.\d+\.\d+", toolchain):
        raise RuntimeError("Release must declare an exact stable Rust toolchain")
    run(
        "rustup",
        "toolchain",
        "install",
        toolchain,
        "--profile",
        "minimal",
        "--component",
        "rustfmt",
        "--component",
        "clippy",
    )
    if TARGET:
        run("rustup", "target", "add", "--toolchain", toolchain, TARGET)
    for package, version in (("just", "1.58.0"), ("cargo-nextest", "0.9.146")):
        run("cargo", "install", "--locked", "--version", version, package)


def build_env(native=False):
    os.environ["CODEX_REPO_ROOT"] = str(ROOT)
    os.environ.setdefault("CARGO_PROFILE_DEV_DEBUG", "0")
    os.environ.setdefault("CARGO_PROFILE_TEST_DEBUG", "0")
    os.environ.setdefault("CARGO_PROFILE_RELEASE_DEBUG", "0")
    os.environ.setdefault("CARGO_INCREMENTAL", "0")
    os.environ.setdefault("CARGO_BUILD_JOBS", "2")
    # Identity/updater behavior is compiled in; no user configuration is changed.
    os.environ["CODEX_GEAR_FORK"] = "1"
    sys.path.insert(0, str(ROOT / "scripts"))
    from codex_package.targets import TARGET_SPECS, default_target
    from codex_package.v8 import resolve_codex_v8_cargo_env

    host = subprocess.check_output(["rustc", "-vV"], cwd=RS, text=True)
    host = next(line[6:] for line in host.splitlines() if line.startswith("host: "))
    target = TARGET or (host if native else default_target())
    os.environ.update(resolve_codex_v8_cargo_env(TARGET_SPECS[target]))
    return target


def require_selector_tests(listing):
    registered = {
        (suite["package-name"], name.rsplit("::", 1)[-1]): case
        for suite in listing["rust-suites"].values()
        for name, case in suite["testcases"].items()
    }
    for package, path in (
        ("codex-core", "core/tests/suite/mcp_server_selection.rs"),
        ("codex-tui", "tui/src/bottom_pane/mcp_selection_tests.rs"),
    ):
        source = (POLICY / "codex-rs" / path).read_text()
        for name in re.findall(
            r"^(?:async )?fn (mcp_(?:selection|selector)_\w+)\(", source, re.M
        ):
            case = registered.get((package, name))
            if (
                not case
                or case["ignored"]
                or case["filter-match"]["status"] != "matches"
            ):
                raise RuntimeError(
                    "Mandatory selector test is absent or disabled: " + name
                )


def tests():
    target = build_env(native=True)
    target_args = ["--target", target] if TARGET else []
    run("cargo", "build", "--locked", *target_args, "-p", "codex-rmcp-client", "--bins")
    output = RS / "target" / target / "debug" if TARGET else RS / "target/debug"
    suffix = ".exe" if os.name == "nt" else ""
    for binary in output.glob("test_*" + suffix):
        if binary.is_file() and (
            binary.suffix == ".exe" if suffix else not binary.suffix
        ):
            os.environ["CARGO_BIN_EXE_" + binary.stem] = str(binary)
    test_args = [
        "--locked",
        *target_args,
        "-p",
        "codex-core",
        "-p",
        "codex-tui",
        "-p",
        "codex-mcp",
        "-p",
        "codex-rmcp-client",
        "-E",
        "test(mcp_selection) or test(mcp_selector) or package(codex-mcp) or (package(codex-rmcp-client) and test(streamable_http_)) or (package(codex-tui) and test(mcp))",
    ]
    listing = subprocess.check_output(
        ["cargo", "nextest", "list", "--message-format", "json", *test_args], cwd=RS
    )
    require_selector_tests(json.loads(listing))
    # Use the trusted command here so candidate changes cannot replace the gate recipe.
    os.environ["RUST_MIN_STACK"] = "8388608"
    os.environ["NEXTEST_PROFILE"] = "local"
    run("cargo", "nextest", "run", "--no-fail-fast", *test_args)


def package():
    target = build_env()
    if os.environ.get("GITHUB_ACTIONS") == "true":
        # Tests have finished. Reclaim only this job's debug outputs before release linking.
        for debug in (RS / "target/debug", RS / "target" / target / "debug"):
            if debug.is_dir():
                shutil.rmtree(debug)
    spec_windows = "windows" in target
    suffix = ".exe" if spec_windows else ""
    out = RS / "target" / target / "release"
    args = ["--target", target, "--release", "--locked"]
    prebuilt = []
    if "linux" in target:
        run("cargo", "build", *args, "--bin", "bwrap")
        run("strip", "--strip-debug", "--strip-unneeded", out / "bwrap")
        os.environ["CODEX_BWRAP_SHA256"] = hashlib.sha256(
            (out / "bwrap").read_bytes()
        ).hexdigest()
        prebuilt += ["--bwrap-bin", str(out / "bwrap")]
    binaries = ["codex", "codex-code-mode-host", "codex-responses-api-proxy"]
    if spec_windows:
        binaries += [
            "codex-command-runner",
            "codex-windows-sandbox-setup",
            "codex-windows-sandbox-service",
        ]
    run(
        "cargo",
        "build",
        *args,
        *[part for binary in binaries for part in ("--bin", binary)],
    )
    identity = json.loads(IDENTITY.read_text())
    directory = ROOT / "gear-package"
    # All Cargo builds above use locked dependencies. Assembly receives prebuilt binaries.
    prebuilt += [
        "--entrypoint-bin",
        str(out / ("codex" + suffix)),
        "--code-mode-host-bin",
        str(out / ("codex-code-mode-host" + suffix)),
    ]
    if spec_windows:
        prebuilt += [
            "--codex-command-runner-bin",
            str(out / "codex-command-runner.exe"),
            "--codex-windows-sandbox-setup-bin",
            str(out / "codex-windows-sandbox-setup.exe"),
        ]
    run(
        sys.executable,
        ROOT / "scripts/build_codex_package.py",
        "--target",
        target,
        "--package-dir",
        directory,
        "--package-version",
        identity["tag"][6:] + "+gear." + str(identity["revision"]),
        *prebuilt,
        cwd=ROOT,
    )
    for name in ("LICENSE", "NOTICE"):
        if (ROOT / name).exists():
            shutil.copy2(ROOT / name, directory / name)
    shutil.copy2(POLICY / "fork/INSTALL.md", directory / "INSTALL.md")
    licenses = directory / "licenses"
    licenses.mkdir()
    shutil.copy2(
        ROOT / "codex-rs/vendor/bubblewrap/COPYING", licenses / "bubblewrap.txt"
    )
    # Preserve notices carried by the checksum-verified helper distributions.
    from codex_package.dotslash import default_cache_root

    for helper in ("rg", "zsh"):
        cache = default_cache_root() / f"{target}-{helper}"
        if not cache.exists():
            continue
        for downloaded in cache.iterdir():
            if downloaded.name.endswith(".tar.gz"):
                with tarfile.open(downloaded) as bundle:
                    for member in bundle.getmembers():
                        if member.isfile() and Path(
                            member.name
                        ).name.upper().startswith(("LICENSE", "COPYING", "NOTICE")):
                            (
                                licenses / (helper + "-" + Path(member.name).name)
                            ).write_bytes(bundle.extractfile(member).read())
            elif downloaded.suffix == ".zip":
                with zipfile.ZipFile(downloaded) as bundle:
                    for name in bundle.namelist():
                        if (
                            Path(name)
                            .name.upper()
                            .startswith(("LICENSE", "COPYING", "NOTICE"))
                        ):
                            (licenses / (helper + "-" + Path(name).name)).write_bytes(
                                bundle.read(name)
                            )
    shutil.copy2(
        out / ("codex-responses-api-proxy" + suffix),
        directory / "bin" / ("codex-responses-api-proxy" + suffix),
    )
    if spec_windows:
        shutil.copy2(
            out / "codex-windows-sandbox-service.exe",
            directory / "codex-resources/codex-windows-sandbox-service.exe",
        )
    # Native executable alongside the original name retains the canonical resource layout.
    shutil.copy2(
        directory / "bin" / ("codex" + suffix),
        directory / "bin" / ("codex-gear" + suffix),
    )
    if "apple" in target:
        for binary in (directory / "bin").iterdir():
            run("codesign", "--force", "--sign", "-", binary)
    manifest = {
        "schema": 1,
        "release_id": identity["id"],
        "upstream_tag": identity["tag"],
        "upstream_sha": identity["upstream_sha"],
        "source_sha": identity["source_sha"],
        "target": target,
        "run_id": int(os.environ["GITHUB_RUN_ID"]),
    }
    (directory / "fork-build.json").write_text(json.dumps(manifest, indent=2) + "\n")
    dist = ROOT / "gear-dist"
    dist.mkdir(exist_ok=True)
    name = "codex-gear-" + target + (".zip" if spec_windows else ".tar.gz")
    archive = dist / name
    if spec_windows:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(directory.rglob("*")):
                if path.is_file():
                    bundle.write(path, path.relative_to(directory))
    else:
        with tarfile.open(archive, "w:gz") as bundle:
            for path in sorted(directory.iterdir()):
                bundle.add(path, arcname=path.name)
    # Test the extracted archive, not the Cargo output tree.
    with tempfile.TemporaryDirectory(prefix="codex-gear-extracted-") as extracted:
        if spec_windows:
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(extracted)
        else:
            with tarfile.open(archive) as bundle:
                bundle.extractall(extracted, filter="data")
        run(sys.executable, POLICY / "fork/tests/archive_smoke.py", extracted, cwd=ROOT)
    manifest.update(asset=name, sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
    (dist / (name + ".json")).write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    {"setup": setup, "tests": tests, "package": package}[sys.argv[1]]()
