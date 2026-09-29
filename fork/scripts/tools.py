#!/usr/bin/env python3
"""Install pinned validation binaries, verifying recorded release asset digests."""

import hashlib
import io
from pathlib import Path
import platform
import sys
import tarfile
import urllib.request
import zipfile

TOOLS = {
    ("gh-aw", "linux", "x86_64"): (
        "https://github.com/github/gh-aw/releases/download/v0.87.4/linux-amd64",
        "e5c171a660bc2769ebf4709e1abd33659d031ba9f5def6983d0fb4a6ba5a3d00",
    ),
    ("gh-aw", "darwin", "aarch64"): (
        "https://github.com/github/gh-aw/releases/download/v0.87.4/darwin-arm64",
        "76d3ff6906b6548489be5e69c8e561ee67c51575df6e7b5893df6f375c339fe9",
    ),
    ("actionlint", "linux", "x86_64"): (
        "https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_linux_amd64.tar.gz",
        "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8",
    ),
    ("actionlint", "darwin", "aarch64"): (
        "https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_darwin_arm64.tar.gz",
        "aba9ced2dee8d27fecca3dc7feb1a7f9a52caefa1eb46f3271ea66b6e0e6953f",
    ),
}

TEST_TOOLS = {
    "just": (
        "https://github.com/casey/just/releases/download/1.58.0/just-1.58.0-",
        {
            "aarch64-apple-darwin.tar.gz": "50ae3e996c974a0bf32ea7d10f495070df33f1b43e0616b2769e3d4821ed8f48",
            "x86_64-apple-darwin.tar.gz": "9a09cfef66aaa79da58203970103a0684307716caaabd3e9844cacc4dc0f4023",
            "aarch64-unknown-linux-musl.tar.gz": "748237128c4c40cbdabc65e841d05ceba13cc23a91eaba395495894c1d9764df",
            "x86_64-unknown-linux-musl.tar.gz": "4a5cc2f53e6f0f8c59092a6cc38291eb729d46a7dd95d3ae582008881b84931d",
            "x86_64-pc-windows-msvc.zip": "759f16fb7aa17c5c8b9594b6d4a8c1a6630dfd042cf2b3ff84841454d3d188dc",
        },
    ),
    "cargo-nextest": (
        "https://github.com/nextest-rs/nextest/releases/download/cargo-nextest-0.9.146/cargo-nextest-0.9.146-",
        {
            "universal-apple-darwin.tar.gz": "39785160b3c2f6ed9a765049cf4fa79f3b39aa02eb7598a5a0e2a1a0b9ffb9a8",
            "aarch64-unknown-linux-musl.tar.gz": "62ae8b4ad034704f67417f8ed8b897566d82359ab44952729b925fb37eeb7622",
            "x86_64-unknown-linux-musl.tar.gz": "b64617e8640624e8f9ba99819e37d7971154e97836d7ae4774fed4715501a6aa",
            "x86_64-pc-windows-msvc.tar.gz": "809b01f2c94d031ab6cf5f4d4a4f477c8f51b69c96ab3cb2fe4db7ecb222d0d3",
        },
    ),
}


def install(directory, names=("gh-aw", "actionlint")):
    directory.mkdir(parents=True, exist_ok=True)
    system = platform.system().lower()
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    for name in names:
        if name in TEST_TOOLS:
            base, checksums = TEST_TOOLS[name]
            arch = (
                "universal"
                if system == "darwin" and name == "cargo-nextest"
                else machine
            )
            triple = {
                "darwin": "apple-darwin",
                "linux": "unknown-linux-musl",
                "windows": "pc-windows-msvc",
            }[system]
            extension = ".zip" if system == "windows" and name == "just" else ".tar.gz"
            asset = arch + "-" + triple + extension
            url, digest = base + asset, checksums[asset]
        else:
            url, digest = TOOLS[name, system, machine]
        data = urllib.request.urlopen(url, timeout=120).read()
        if hashlib.sha256(data).hexdigest() != digest:
            raise RuntimeError("Invalid downloaded checksum: " + name)
        executable = name + (".exe" if system == "windows" else "")
        if url.endswith(".tar.gz"):
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
                member = archive.getmember(executable)
                if not member.isfile():
                    raise RuntimeError("Expected a regular executable: " + executable)
                data = archive.extractfile(member).read()
        elif url.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                data = archive.read(executable)
        path = directory / executable
        path.write_bytes(data)
        path.chmod(0o755)
        print("Verified " + str(path))


if __name__ == "__main__":
    install(Path(sys.argv[1]), sys.argv[2:] or ("gh-aw", "actionlint"))
