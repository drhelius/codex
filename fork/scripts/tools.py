#!/usr/bin/env python3
"""Install pinned validation binaries, verifying recorded release asset digests."""

import hashlib
import io
from pathlib import Path
import platform
import sys
import tarfile
import urllib.request

TOOLS = {
    ("gh-aw", "linux", "x86_64"): (
        "https://github.com/github/gh-aw/releases/download/v0.87.4/linux-amd64",
        "e5c171a660bc2769ebf4709e1abd33659d031ba9f5def6983d0fb4a6ba5a3d00",
    ),
    ("gh-aw", "darwin", "arm64"): (
        "https://github.com/github/gh-aw/releases/download/v0.87.4/darwin-arm64",
        "76d3ff6906b6548489be5e69c8e561ee67c51575df6e7b5893df6f375c339fe9",
    ),
    ("actionlint", "linux", "x86_64"): (
        "https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_linux_amd64.tar.gz",
        "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8",
    ),
    ("actionlint", "darwin", "arm64"): (
        "https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_darwin_arm64.tar.gz",
        "aba9ced2dee8d27fecca3dc7feb1a7f9a52caefa1eb46f3271ea66b6e0e6953f",
    ),
}


def install(directory):
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("gh-aw", "actionlint"):
        url, digest = TOOLS[name, platform.system().lower(), platform.machine().lower()]
        data = urllib.request.urlopen(url, timeout=120).read()
        if hashlib.sha256(data).hexdigest() != digest:
            raise RuntimeError("Invalid downloaded checksum: " + name)
        if url.endswith(".tar.gz"):
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
                data = archive.extractfile(name).read()
        path = directory / name
        path.write_bytes(data)
        path.chmod(0o755)
        print("Verified " + str(path))


if __name__ == "__main__":
    install(Path(sys.argv[1]))
