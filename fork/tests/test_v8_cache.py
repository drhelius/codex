"""Native archive recovery after the dependency cache prunes V8's gn_out."""

import gzip
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import validate


class V8CacheTests(unittest.TestCase):
    def test_cold_and_stale_archives_are_restored_for_both_library_names(self):
        for target, name in (
            ("aarch64-apple-darwin", "librusty_v8.a"),
            ("x86_64-pc-windows-msvc", "rusty_v8.lib"),
        ):
            with (
                self.subTest(target=target),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                archive = root / "verified.gz"
                archive.write_bytes(gzip.compress(b"verified native archive"))
                output = root / "target" / target / "release"
                library = output / "gn_out" / "obj" / name
                for existing in (None, b"stale native archive"):
                    if existing is not None:
                        library.write_bytes(existing)
                    validate.restore_v8_archive(archive, output, target)
                    self.assertEqual(library.read_bytes(), b"verified native archive")
                    self.assertEqual(list(library.parent.iterdir()), [library])

    def test_interrupted_decompression_preserves_existing_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "truncated.gz"
            archive.write_bytes(gzip.compress(b"new native archive")[:-8])
            library = root / "gn_out" / "obj" / "librusty_v8.a"
            library.parent.mkdir(parents=True)
            library.write_bytes(b"previous archive")
            with self.assertRaises(EOFError):
                validate.restore_v8_archive(archive, root, "aarch64-apple-darwin")
            self.assertEqual(library.read_bytes(), b"previous archive")
            self.assertEqual(list(library.parent.iterdir()), [library])

    def test_both_gate_profiles_restore_the_verified_archive_before_cargo(self):
        sys.path.insert(0, str(validate.ROOT / "scripts"))
        with patch.dict(os.environ, CODEX_REPO_ROOT=str(validate.ROOT)):
            from codex_package import v8

        target = "aarch64-apple-darwin"
        for native, explicit_target, relative in (
            (True, "", "target/debug"),
            (True, target, f"target/{target}/debug"),
            (False, "", f"target/{target}/release"),
            (False, target, f"target/{target}/release"),
        ):
            with (
                self.subTest(native=native, target=explicit_target),
                patch.dict(os.environ),
                patch.object(validate, "TARGET", explicit_target),
                patch.object(
                    validate.subprocess,
                    "check_output",
                    return_value=f"host: {target}\n",
                ),
                patch("codex_package.targets.default_target", return_value=target),
                patch.object(
                    v8,
                    "resolve_codex_v8_cargo_env",
                    return_value={
                        "RUSTY_V8_ARCHIVE": "/verified/archive.gz",
                        "RUSTY_V8_SRC_BINDING_PATH": "/verified/binding.rs",
                    },
                ),
                patch.object(validate, "restore_v8_archive") as restore,
            ):
                self.assertEqual(validate.build_env(native=native), target)
                restore.assert_called_once_with(
                    Path("/verified/archive.gz"), validate.RS / relative, target
                )

    def test_warm_cargo_build_recovers_without_rerunning_cached_build_script(self):
        host = subprocess.check_output(["rustc", "-vV"], text=True)
        target = next(
            line[6:] for line in host.splitlines() if line.startswith("host: ")
        )
        for args, relative in (
            ([], "debug"),
            (["--release", "--target", target], f"{target}/release"),
        ):
            with (
                self.subTest(profile=relative),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                (root / "Cargo.toml").write_text(
                    '[package]\nname = "v8-cache-fixture"\nversion = "0.1.0"\nedition = "2024"\n'
                )
                (root / "src").mkdir()
                source = root / "src/lib.rs"
                source.write_text("pub fn value() -> u8 { 1 }\n")
                # A valid empty ar archive exercises rustc's native-library lookup.
                archive = root / "verified.gz"
                archive.write_bytes(gzip.compress(b"!<arch>\n"))
                (root / "build.rs").write_text("""
use std::{env, fs, path::PathBuf};
fn main() {
    println!("cargo:rerun-if-changed=build.rs");
    let out = PathBuf::from(env::var_os("OUT_DIR").unwrap());
    let obj = out.ancestors().nth(3).unwrap().join("gn_out/obj");
    fs::create_dir_all(&obj).unwrap();
    let name = if env::var("CARGO_CFG_TARGET_OS").unwrap() == "windows" {
        "rusty_v8.lib"
    } else {
        "librusty_v8.a"
    };
    fs::write(obj.join(name), b"!<arch>\\n").unwrap();
    let log = PathBuf::from("build-script-runs");
    let mut runs = fs::read_to_string(&log).unwrap_or_default();
    runs.push('1');
    fs::write(log, runs).unwrap();
    println!("cargo:rustc-link-search=native={}", obj.display());
    println!("cargo:rustc-link-lib=static=rusty_v8");
}
""")
                env = dict(os.environ, CARGO_TARGET_DIR=str(root / "target"))
                command = ["cargo", "build", "--offline", *args]
                first = subprocess.run(
                    command, cwd=root, env=env, capture_output=True, text=True
                )
                self.assertEqual(first.returncode, 0, first.stderr)
                output = root / "target" / relative
                shutil.rmtree(output / "gn_out")
                source.write_text("pub fn value() -> u8 { 2 }\n")
                missing = subprocess.run(
                    command, cwd=root, env=env, capture_output=True, text=True
                )
                self.assertNotEqual(missing.returncode, 0)
                self.assertIn(
                    "could not find native static library `rusty_v8`", missing.stderr
                )
                validate.restore_v8_archive(archive, output, target)
                recovered = subprocess.run(
                    command, cwd=root, env=env, capture_output=True, text=True
                )
                self.assertEqual(recovered.returncode, 0, recovered.stderr)
                self.assertEqual((root / "build-script-runs").read_text(), "1")


if __name__ == "__main__":
    unittest.main()
