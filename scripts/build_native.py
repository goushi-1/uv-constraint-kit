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
    args = parser.parse_args()
    if os.name != "nt" or platform.machine().upper() not in {"AMD64", "X86_64"}:
        parser.error("The native engine currently requires Windows x64 and MSVC")
    build = args.build_dir.resolve()
    addon = SOURCE.parent.resolve()
    if build == ROOT or build == addon or addon in build.parents:
        parser.error("Use a separate build directory, outside the add-on source tree")
    cmake = shutil.which(args.cmake)
    if not cmake:
        parser.error("CMake was not found; install CMake 3.20+ or provide --cmake")
    cmake_build = build / "cpu"
    run([cmake, "-S", str(SOURCE), "-B", str(cmake_build),
         "-G", "Visual Studio 17 2022", "-A", "x64", "-DUVCK_BUILD_GPU=OFF"])
    run([cmake, "--build", str(cmake_build), "--config", "Release", "--target", "uvck_engine"])
    compiled = cmake_build / "Release" / "uvck_engine.dll"
    digest = sha256(compiled)
    output = build / "bin" / "windows-x64"
    output.mkdir(parents=True, exist_ok=True)
    filename = f"uvck_engine_{digest[:16]}.dll"
    copy_artifact(compiled, output / filename)
    identity = {"abi": 1, "library": filename, "sha256": digest,
                "source_sha256": sha256(SOURCE / "engine.cpp")}
    (output / "engine.json").write_text(json.dumps(identity, indent=2) + "\n", encoding="utf8")
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
    print(json.dumps({"engine_dir": str(output), "cpu": identity, "gpu_requested": args.gpu}, indent=2))


if __name__ == "__main__":
    main()
