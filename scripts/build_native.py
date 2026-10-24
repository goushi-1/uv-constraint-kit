# SPDX-License-Identifier: GPL-3.0-or-later
"""Build Windows x64 engines into a local build directory, never an installation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "uv_constraint_kit" / "native_source"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(command: list[str]) -> str:
    completed = subprocess.run(command, capture_output=True,
                               text=True, encoding="utf8", errors="replace")
    if completed.returncode:
        sys.stderr.write(completed.stdout + completed.stderr)
        raise subprocess.CalledProcessError(completed.returncode, command)
    return completed.stdout


def copy_artifact(source: Path, destination: Path) -> None:
    if destination.exists() and sha256(destination) != sha256(source):
        raise FileExistsError(f"Artifact collision: {destination.name}")
    shutil.copyfile(source, destination)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, default=ROOT / "build" / "native")
    parser.add_argument("--cmake", default="cmake")
    parser.add_argument("--gpu", action="store_true", help="Also build the optional CUDA 12.4 sidecar")
    parser.add_argument("--cuda-include", type=Path)
    parser.add_argument("--nvrtc-dir", type=Path)
    parser.add_argument("--reader", action="store_true", help="Also build the optional CPython stable-ABI reader")
    parser.add_argument("--python-include", type=Path,
                        help="Directory containing Python.h for the stable-ABI reader")
    parser.add_argument("--python-library", type=Path,
                        help="Stable ABI import library named python3.lib")
    args = parser.parse_args()
    if os.name != "nt" or platform.machine().upper() not in {"AMD64", "X86_64"}:
        parser.error("The native engine currently requires Windows x64 and MSVC")
    if args.reader:
        if args.python_include is None or args.python_library is None:
            parser.error("--reader requires --python-include and --python-library")
        if not args.python_include.is_dir():
            parser.error("--python-include must be an existing directory")
        if not args.python_library.is_file():
            parser.error("--python-library must be an existing file")
        if args.python_library.name.casefold() != "python3.lib":
            parser.error("--python-library must be the stable ABI python3.lib, not a versioned import library")
    elif args.python_include is not None or args.python_library is not None:
        parser.error("--python-include and --python-library require --reader")
    build = args.build_dir.resolve()
    addon = SOURCE.parent.resolve()
    if build == ROOT or build == addon or addon in build.parents:
        parser.error("Use a separate build directory, outside the add-on source tree")
    cmake = shutil.which(args.cmake)
    if not cmake:
        parser.error("CMake was not found; install CMake 3.20+ or provide --cmake")
    cmake_build = build / "cpu"
    configure = [cmake, "-S", str(SOURCE), "-B", str(cmake_build),
                 "-G", "Visual Studio 17 2022", "-A", "x64", "-DUVCK_BUILD_GPU=OFF",
                 f"-DUVCK_BUILD_READER={'ON' if args.reader else 'OFF'}"]
    if args.reader:
        configure.extend((f"-DUVCK_PYTHON_INCLUDE={args.python_include.resolve()}",
                          f"-DUVCK_PYTHON_LIBRARY={args.python_library.resolve()}"))
    run(configure)
    run([cmake, "--build", str(cmake_build), "--config", "Release", "--target", "uvck_engine"])
    compiled = cmake_build / "Release" / "uvck_engine.dll"
    digest = sha256(compiled)
    output = build / "bin" / "windows-x64"
    output.mkdir(parents=True, exist_ok=True)
    # Only selected artifacts are active when a build directory is reused.
    # Old checksum-named binaries can stay; packaging follows metadata only.
    for enabled, metadata in ((args.reader, "reader.json"), (args.gpu, "gpu.json")):
        if not enabled:
            (output / metadata).unlink(missing_ok=True)
    filename = f"uvck_engine_{digest[:16]}.dll"
    copy_artifact(compiled, output / filename)
    identity = {"abi": 1, "library": filename, "sha256": digest,
                "source_sha256": sha256(SOURCE / "engine.cpp")}
    (output / "engine.json").write_text(json.dumps(identity, indent=2) + "\n", encoding="utf8")
    reader_identity = None
    if args.reader:
        run([cmake, "--build", str(cmake_build), "--config", "Release", "--target", "uvck_reader"])
        reader_compiled = cmake_build / "Release" / "_uvck_reader.pyd"
        reader_digest = sha256(reader_compiled)
        reader_filename = f"uvck_reader_{reader_digest[:16]}.pyd"
        copy_artifact(reader_compiled, output / reader_filename)
        reader_identity = {
            "api": 2,
            "library": reader_filename,
            "sha256": reader_digest,
            "source_sha256": sha256(SOURCE / "mesh_reader.cpp"),
            "minimum_python": "3.11",
            "stable_abi": "0x030B0000",
        }
        (output / "reader.json").write_text(
            json.dumps(reader_identity, indent=2) + "\n", encoding="utf8")
    if args.gpu:
        command = [sys.executable, str(SOURCE / "build_gpu.py"), "--build-dir", str(build / "gpu"),
                   "--cmake", cmake]
        for option, value in (("--cuda-include", args.cuda_include), ("--nvrtc-dir", args.nvrtc_dir)):
            if value is not None:
                command.extend((option, str(value.resolve())))
        artifact_dir = Path(json.loads(run(command))["artifacts"])
        gpu = json.loads((artifact_dir / "gpu.json").read_text(encoding="utf8"))
        if gpu.get("abi") != 1:
            raise ValueError("Unexpected GPU ABI")
        for key, suffix in (("library", ".dll"), ("ptx", ".ptx")):
            name = gpu[key]
            if (Path(name).name != name or not name.startswith("uvck_gpu_")
                    or not name.endswith(suffix)):
                raise ValueError(f"Invalid GPU artifact name: {key}")
            source = artifact_dir / name
            if sha256(source) != gpu[key + "_sha256"]:
                raise ValueError(f"GPU checksum mismatch: {key}")
            copy_artifact(source, output / name)
        shutil.copyfile(artifact_dir / "gpu.json", output / "gpu.json")
    print(json.dumps({"engine_dir": str(output), "cpu": identity, "reader_requested": args.reader,
                      "reader": reader_identity, "gpu_requested": args.gpu}, indent=2))


if __name__ == "__main__":
    main()
