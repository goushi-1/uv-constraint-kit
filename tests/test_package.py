# SPDX-License-Identifier: GPL-3.0-or-later
"""Standard-library checks for CPU/GPU package selection and archive hashes."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CPU_ENGINE_DIR = ROOT / "build" / "native" / "bin" / "windows-x64"
GPU_ENGINE_DIR = ROOT / "build" / "native_gpu" / "bin" / "windows-x64"
PACKAGE_SCRIPT = ROOT / "scripts" / "package.py"

spec = importlib.util.spec_from_file_location("uvck_package", PACKAGE_SCRIPT)
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


def run_package(*arguments: str) -> tuple[int, str]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        with patch.object(sys, "argv", [str(PACKAGE_SCRIPT), *arguments]):
            try:
                package.main()
            except SystemExit as exc:
                return int(exc.code), output.getvalue()
    return 0, output.getvalue()


class PackageTests(unittest.TestCase):
    def test_require_gpu_needs_native_dir(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as temporary:
            output = Path(temporary) / "must-not-exist.zip"
            code, message = run_package("--require-gpu", "--output", str(output))
            self.assertEqual(code, 2, message)
            self.assertIn("--require-gpu requires --native-dir", message)
            self.assertFalse(output.exists())

    def test_cpu_engine_is_rejected_without_creating_zip(self):
        if not (CPU_ENGINE_DIR / "engine.json").is_file():
            self.skipTest(f"CPU engine not found: {CPU_ENGINE_DIR}")
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as temporary:
            output = Path(temporary) / "must-not-exist.zip"
            code, message = run_package(
                "--native-dir", str(CPU_ENGINE_DIR), "--require-gpu", "--output", str(output)
            )
            self.assertEqual(code, 2, message)
            self.assertIn("validated GPU DLL and PTX", message)
            self.assertFalse(output.exists())

    def test_gpu_engine_packages_and_preserves_validated_hashes(self):
        gpu_json = GPU_ENGINE_DIR / "gpu.json"
        if not gpu_json.is_file():
            self.skipTest(f"GPU engine not found; pass --engine-dir when available: {GPU_ENGINE_DIR}")
        identity = json.loads(gpu_json.read_text(encoding="utf8"))
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as temporary:
            output = Path(temporary) / "with-gpu.zip"
            code, message = run_package(
                "--native-dir", str(GPU_ENGINE_DIR), "--require-gpu", "--output", str(output)
            )
            self.assertEqual(code, 0, message)
            self.assertTrue(output.is_file())
            with zipfile.ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                names = set(archive.namelist())
                expected = {
                    "bin/windows-x64/gpu.json",
                    f"bin/windows-x64/{identity['library']}",
                    f"bin/windows-x64/{identity['ptx']}",
                }
                self.assertTrue(expected.issubset(names), expected - names)
                for key, checksum_key in (("library", "library_sha256"), ("ptx", "ptx_sha256")):
                    member = f"bin/windows-x64/{identity[key]}"
                    digest = hashlib.sha256(archive.read(member)).hexdigest()
                    self.assertEqual(digest, identity[checksum_key], member)
                packaged_identity = json.loads(archive.read("bin/windows-x64/gpu.json"))
                self.assertEqual(packaged_identity, identity)


def main() -> None:
    global GPU_ENGINE_DIR
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine-dir", type=Path, default=GPU_ENGINE_DIR,
                        help="GPU engine directory to package-test; defaults to build/native_gpu/bin/windows-x64")
    args, remaining = parser.parse_known_args()
    GPU_ENGINE_DIR = args.engine_dir.resolve()
    # Keep the GPU directory override local to the GPU success test; CPU rejection
    # intentionally always uses the repository's known CPU-only build.
    unittest.main(argv=[sys.argv[0], *remaining])


if __name__ == "__main__":
    main()
