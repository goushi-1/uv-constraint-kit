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


def write_native_fixture(folder: Path, *, reader: dict | None = None, gpu: bool = False) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    engine = b"fixture engine"
    (folder / "uvck_engine_fixture.dll").write_bytes(engine)
    engine_identity = {
        "abi": 1,
        "library": "uvck_engine_fixture.dll",
        "sha256": hashlib.sha256(engine).hexdigest(),
        "source_sha256": package.sha256(package.ADDON / "native_source" / "engine.cpp"),
    }
    (folder / "engine.json").write_text(json.dumps(engine_identity), encoding="utf8")
    if gpu:
        library, ptx = b"fixture gpu", b"fixture ptx"
        (folder / "uvck_gpu_fixture.dll").write_bytes(library)
        (folder / "uvck_gpu_fixture.ptx").write_bytes(ptx)
        gpu_identity = {
            "abi": 1,
            "library": "uvck_gpu_fixture.dll",
            "ptx": "uvck_gpu_fixture.ptx",
            "library_sha256": hashlib.sha256(library).hexdigest(),
            "ptx_sha256": hashlib.sha256(ptx).hexdigest(),
            "source_sha256": {
                name: package.sha256(package.ADDON / "native_source" / name)
                for name in ("gpu_driver.hpp", "gpu_metric.cu")
            },
            "engine_source_sha256": package.sha256(package.ADDON / "native_source" / "engine_gpu.cpp"),
        }
        (folder / "gpu.json").write_text(json.dumps(gpu_identity), encoding="utf8")
    if reader is not None:
        library = b"fixture reader"
        filename = reader.setdefault("library", "uvck_reader_fixture.pyd")
        (folder / filename).write_bytes(library)
        identity = {
            "api": 2,
            "library": filename,
            "sha256": hashlib.sha256(library).hexdigest(),
            "source_sha256": package.sha256(package.ADDON / "native_source" / "mesh_reader.cpp"),
            "minimum_python": "3.11",
            "stable_abi": "0x030B0000",
        }
        identity.update(reader)
        (folder / "reader.json").write_text(json.dumps(identity), encoding="utf8")


class PackageTests(unittest.TestCase):
    def test_require_gpu_needs_native_dir(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            output = Path(temporary) / "must-not-exist.zip"
            code, message = run_package("--require-gpu", "--output", str(output))
            self.assertEqual(code, 2, message)
            self.assertIn("--require-gpu requires --native-dir", message)
            self.assertFalse(output.exists())

    def test_cpu_engine_is_rejected_without_creating_zip(self):
        if not (CPU_ENGINE_DIR / "engine.json").is_file():
            self.skipTest(f"CPU engine not found: {CPU_ENGINE_DIR}")
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            output = Path(temporary) / "must-not-exist.zip"
            code, message = run_package(
                "--native-dir", str(CPU_ENGINE_DIR), "--require-gpu", "--output", str(output)
            )
            self.assertEqual(code, 2, message)
            self.assertIn("validated GPU DLL and PTX", message)
            self.assertFalse(output.exists())

    def test_require_reader_needs_native_dir_and_reader(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            output = Path(temporary) / "must-not-exist.zip"
            code, message = run_package("--require-reader", "--output", str(output))
            self.assertEqual(code, 2, message)
            self.assertIn("--require-reader requires --native-dir", message)
            self.assertFalse(output.exists())

            native = Path(temporary) / "native"
            write_native_fixture(native)
            code, message = run_package("--native-dir", str(native), "--require-reader",
                                        "--output", str(output))
            self.assertEqual(code, 2, message)
            self.assertIn("validated reader module", message)
            self.assertFalse(output.exists())

    def test_invalid_reader_api_source_and_hash_are_rejected(self):
        bad_metadata = (
            ({"api": 1}, "API"),
            ({"source_sha256": "0" * 64}, "different mesh_reader.cpp"),
            ({"sha256": "0" * 64}, "checksum mismatch"),
        )
        for changes, expected in bad_metadata:
            with self.subTest(changes=changes), tempfile.TemporaryDirectory(dir=ROOT) as temporary:
                native = Path(temporary) / "native"
                output = Path(temporary) / "must-not-exist.zip"
                write_native_fixture(native, reader=dict(changes))
                with self.assertRaisesRegex(ValueError, expected):
                    run_package("--native-dir", str(native), "--output", str(output))
                self.assertFalse(output.exists())

    def test_reader_and_gpu_package_with_required_reader(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            native = Path(temporary) / "native"
            output = Path(temporary) / "reader-gpu.zip"
            write_native_fixture(native, reader={}, gpu=True)
            code, message = run_package("--native-dir", str(native), "--require-reader",
                                        "--require-gpu", "--output", str(output))
            self.assertEqual(code, 0, message)
            with zipfile.ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                names = set(archive.namelist())
                for name in (
                    "mesh_reader.py",
                    "native_source/mesh_reader.cpp",
                    "bin/windows-x64/reader.json",
                    "bin/windows-x64/uvck_reader_fixture.pyd",
                    "bin/windows-x64/gpu.json",
                    "bin/windows-x64/uvck_gpu_fixture.dll",
                    "bin/windows-x64/uvck_gpu_fixture.ptx",
                ):
                    self.assertIn(name, names)

    def test_python_only_package_keeps_reader_optional(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            output = Path(temporary) / "python-only.zip"
            code, message = run_package("--output", str(output))
            self.assertEqual(code, 0, message)
            with zipfile.ZipFile(output) as archive:
                names = set(archive.namelist())
                self.assertIn("mesh_reader.py", names)
                self.assertNotIn("bin/windows-x64/reader.json", names)

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
