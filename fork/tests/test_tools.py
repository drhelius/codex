"""Offline checks for pinned build-tool integrity and archive extraction."""

import hashlib
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import tools


class ToolTests(unittest.TestCase):
    def test_checksum_failure_preserves_existing_executable(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            executable = directory / "fixture"
            executable.write_bytes(b"existing verified tool")
            with (
                patch.dict(
                    tools.TOOLS,
                    {
                        ("fixture", "linux", "x86_64"): (
                            "https://invalid.test/tool",
                            "0" * 64,
                        )
                    },
                ),
                patch.object(tools.platform, "system", return_value="Linux"),
                patch.object(tools.platform, "machine", return_value="x86_64"),
                patch.object(
                    tools.urllib.request,
                    "urlopen",
                    return_value=io.BytesIO(b"corrupt download"),
                ),
                self.assertRaisesRegex(RuntimeError, "Invalid downloaded checksum"),
            ):
                tools.install(directory, ("fixture",))
            self.assertEqual(executable.read_bytes(), b"existing verified tool")

    def test_extracts_only_named_executable_for_tar_and_windows_zip(self):
        for system, machine, extension in (
            ("Linux", "x86_64", ".tar.gz"),
            ("Windows", "AMD64", ".zip"),
        ):
            with (
                self.subTest(system=system),
                tempfile.TemporaryDirectory() as temporary,
            ):
                binary = "fixture.exe" if system == "Windows" else "fixture"
                archive = io.BytesIO()
                if extension == ".zip":
                    with zipfile.ZipFile(archive, "w") as bundle:
                        bundle.writestr(binary, b"verified executable")
                        bundle.writestr("unrequested", b"must not be installed")
                else:
                    with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
                        for name, data in (
                            (binary, b"verified executable"),
                            ("unrequested", b"must not be installed"),
                        ):
                            member = tarfile.TarInfo(name)
                            member.size = len(data)
                            bundle.addfile(member, io.BytesIO(data))
                data = archive.getvalue()
                with (
                    patch.dict(
                        tools.TOOLS,
                        {
                            ("fixture", system.lower(), "x86_64"): (
                                "https://invalid.test/tool" + extension,
                                hashlib.sha256(data).hexdigest(),
                            )
                        },
                    ),
                    patch.object(tools.platform, "system", return_value=system),
                    patch.object(tools.platform, "machine", return_value=machine),
                    patch.object(
                        tools.urllib.request, "urlopen", return_value=io.BytesIO(data)
                    ),
                ):
                    directory = Path(temporary)
                    tools.install(directory, ("fixture",))
                    self.assertEqual([p.name for p in directory.iterdir()], [binary])
                    self.assertEqual(
                        (directory / binary).read_bytes(), b"verified executable"
                    )


if __name__ == "__main__":
    unittest.main()
