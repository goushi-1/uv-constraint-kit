# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the optional CUDA sidecar on Windows x64.

Requires Visual Studio 2022 C++ tools, CMake 3.20+, Python 3.8+, CUDA
runtime headers, and NVRTC 12.4. The CPU CMake target remains the default.
For a local CUDA Toolkit installation, CUDA_PATH supplies include/ and bin/;
wheel/extracted NVRTC layouts can be passed explicitly:

  py build_gpu.py --build-dir build/gpu --cuda-include CUDA/include \
      --nvrtc-dir CUDA/nvrtc/bin --cmake cmake

The host target uses MSVC /O2 /fp:precise /MT and C++17. NVRTC uses
--gpu-architecture=compute_89, --std=c++17, --fmad=false, --ftz=false,
--prec-div=true, and --prec-sqrt=true. The tested NVRTC distribution is
12.4.127. Outputs and provenance stay below --build-dir; PE timestamps and
toolchain details mean this recipe does not promise bitwise reproducibility.
"""
from __future__ import annotations

import argparse
import ctypes as ct
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET


SOURCE = Path(__file__).resolve().parent
NVRTC_OPTIONS = (
    "--gpu-architecture=compute_89", "--std=c++17", "--fmad=false",
    "--ftz=false", "--prec-div=true", "--prec-sqrt=true",
)
HOST_FLAGS = ("/O2", "/fp:precise", "/MT")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def nvrtc_files(directory: Path) -> tuple[Path, Path]:
    libraries = [directory / name for name in
                 ("nvrtc64_124_0.dll", "nvrtc64_120_0.dll")]
    library = next((path for path in libraries if path.is_file()), None)
    preferred_builtins = directory / "nvrtc-builtins64_124.dll"
    found_builtins = [path for path in directory.glob("nvrtc-builtins64_*.dll")
                      if path.is_file()]
    builtins = (preferred_builtins if preferred_builtins.is_file()
                else found_builtins[0] if len(found_builtins) == 1 else None)
    if library is None or builtins is None:
        raise FileNotFoundError(
            f"Expected NVRTC 12.4 and nvrtc-builtins DLLs in {directory}")
    return library, builtins


def compile_ptx(cuda_source: Path, nvrtc_dir: Path, output: Path) -> dict:
    if os.name != "nt" or ct.sizeof(ct.c_void_p) != 8:
        raise OSError("NVRTC build is supported on Windows x64 only")
    nvrtc_path, builtins_path = nvrtc_files(nvrtc_dir)
    with os.add_dll_directory(str(nvrtc_dir)):
        builtins = ct.WinDLL(str(builtins_path))
        nvrtc = ct.WinDLL(str(nvrtc_path))
        major, minor = ct.c_int(), ct.c_int()
        nvrtc.nvrtcVersion.argtypes = [ct.POINTER(ct.c_int), ct.POINTER(ct.c_int)]
        nvrtc.nvrtcVersion.restype = ct.c_int
        status = nvrtc.nvrtcVersion(ct.byref(major), ct.byref(minor))
        if status or (major.value, minor.value) != (12, 4):
            raise RuntimeError(f"Expected NVRTC 12.4, got {major.value}.{minor.value} (status {status})")

        nvrtc.nvrtcCreateProgram.argtypes = [ct.POINTER(ct.c_void_p), ct.c_char_p,
            ct.c_char_p, ct.c_int, ct.c_void_p, ct.c_void_p]
        nvrtc.nvrtcCompileProgram.argtypes = [ct.c_void_p, ct.c_int,
            ct.POINTER(ct.c_char_p)]
        nvrtc.nvrtcDestroyProgram.argtypes = [ct.POINTER(ct.c_void_p)]
        for suffix in ("ProgramLog", "PTX"):
            getattr(nvrtc, "nvrtcGet" + suffix + "Size").argtypes = [
                ct.c_void_p, ct.POINTER(ct.c_size_t)]
            getattr(nvrtc, "nvrtcGet" + suffix).argtypes = [ct.c_void_p, ct.c_void_p]

        program = ct.c_void_p()
        source = cuda_source.read_bytes()
        status = nvrtc.nvrtcCreateProgram(ct.byref(program), source,
                                           b"gpu_metric.cu", 0, None, None)
        if status:
            raise RuntimeError(f"nvrtcCreateProgram failed: {status}")
        options = tuple(item.encode("ascii") for item in NVRTC_OPTIONS)
        try:
            status = nvrtc.nvrtcCompileProgram(
                program, len(options), (ct.c_char_p * len(options))(*options))
            size = ct.c_size_t()
            nvrtc.nvrtcGetProgramLogSize(program, ct.byref(size))
            log = ct.create_string_buffer(size.value)
            nvrtc.nvrtcGetProgramLog(program, log)
            if status:
                raise RuntimeError("NVRTC compile failed: " +
                                   log.value.decode("utf8", "replace"))
            if nvrtc.nvrtcGetPTXSize(program, ct.byref(size)):
                raise RuntimeError("nvrtcGetPTXSize failed")
            ptx = ct.create_string_buffer(size.value)
            if nvrtc.nvrtcGetPTX(program, ptx):
                raise RuntimeError("nvrtcGetPTX failed")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(ptx.raw[:max(0, size.value - 1)])
        finally:
            nvrtc.nvrtcDestroyProgram(ct.byref(program))
    return {
        "version": f"{major.value}.{minor.value}",
        "nvrtc_dll_sha256": sha256(nvrtc_path),
        "builtins_dll_sha256": sha256(builtins_path),
        "options": list(NVRTC_OPTIONS),
    }


def run(command: list[str | Path]) -> str:
    result = subprocess.run([str(value) for value in command], check=True,
                            capture_output=True, text=True, encoding="utf8",
                            errors="replace")
    return result.stdout + result.stderr


def cache_value(cache: Path, key: str) -> str | None:
    prefix = key + ":"
    for line in cache.read_text(encoding="utf8", errors="replace").splitlines():
        if line.startswith(prefix):
            return line.split("=", 1)[1]
    return None


def cmake_compiler_info(build_dir: Path) -> dict:
    result = {"path": None, "id": None, "version": None}
    for path in sorted((build_dir / "CMakeFiles").glob("*/CMakeCXXCompiler.cmake")):
        text = path.read_text(encoding="utf8", errors="replace")
        for field, variable in (("path", "CMAKE_CXX_COMPILER"),
                                ("id", "CMAKE_CXX_COMPILER_ID"),
                                ("version", "CMAKE_CXX_COMPILER_VERSION")):
            match = re.search(r'^set\(' + variable + r'\s+"([^"]*)"\)',
                              text, re.MULTILINE)
            if match:
                result[field] = match.group(1)
        if result["path"] and result["version"]:
            break
    return result


def vcxproj_info(build_dir: Path) -> dict:
    result = {"toolset": None, "windows_sdk": None}
    for path in sorted(build_dir.glob("*.vcxproj")):
        try:
            root = ET.parse(path).getroot()
        except (ET.ParseError, OSError):
            continue
        for node in root.iter():
            name = node.tag.rsplit("}", 1)[-1]
            value = (node.text or "").strip()
            if name == "PlatformToolset" and value:
                result["toolset"] = value
            elif name == "WindowsTargetPlatformVersion" and value:
                result["windows_sdk"] = value
        if result["toolset"] or result["windows_sdk"]:
            break
    return result


def immutable_copy(source: Path, destination: Path) -> str:
    digest = sha256(source)
    if destination.exists():
        if sha256(destination) != digest:
            raise FileExistsError(f"Immutable artifact collision: {destination}")
    else:
        shutil.copyfile(source, destination)
    return digest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--cuda-include", type=Path,
                        default=Path(os.environ["CUDA_PATH"]) / "include"
                        if os.environ.get("CUDA_PATH") else None)
    parser.add_argument("--nvrtc-dir", type=Path,
                        default=Path(os.environ["CUDA_PATH"]) / "bin"
                        if os.environ.get("CUDA_PATH") else None)
    parser.add_argument("--cmake", default=shutil.which("cmake") or "cmake")
    args = parser.parse_args()
    if args.cuda_include is None or not (args.cuda_include / "cuda.h").is_file():
        parser.error("provide --cuda-include or set CUDA_PATH to a CUDA 12.4 toolkit")
    if args.nvrtc_dir is None:
        parser.error("provide --nvrtc-dir or set CUDA_PATH to a CUDA 12.4 toolkit")
    build_dir = args.build_dir.resolve()
    cuda_include = args.cuda_include.resolve()
    nvrtc_dir = args.nvrtc_dir.resolve()
    cmake = shutil.which(args.cmake) or args.cmake
    output = build_dir / "output" / "gpu_metric.ptx"
    nvrtc_record = compile_ptx(SOURCE / "gpu_metric.cu", nvrtc_dir, output)

    cmake_build = build_dir / "cmake"
    configure = [cmake, "-S", SOURCE, "-B", cmake_build,
                 "-G", "Visual Studio 17 2022", "-A", "x64",
                 "-DUVCK_BUILD_GPU=ON", f"-DUVCK_CUDA_INCLUDE={cuda_include}"]
    configure_log = run(configure)
    build_log = run([cmake, "--build", cmake_build, "--config", "Release",
                     "--target", "uvck_gpu"])
    dll_candidates = list(cmake_build.rglob("uvck_gpu.dll"))
    if len(dll_candidates) != 1:
        raise FileNotFoundError(f"Expected one uvck_gpu.dll below {cmake_build}; found {dll_candidates}")
    dll = dll_candidates[0]

    source_hashes = {path.name: sha256(path) for path in
                     (SOURCE / "engine_gpu.cpp", SOURCE / "gpu_driver.hpp",
                      SOURCE / "gpu_metric.cu", SOURCE / "CMakeLists.txt")}
    cache = cmake_build / "CMakeCache.txt"
    compiler_info = cmake_compiler_info(cmake_build)
    project_info = vcxproj_info(cmake_build)
    compiler = (cache_value(cache, "CMAKE_CXX_COMPILER")
                or compiler_info["path"])
    compiler_id = compiler_info["id"]
    compiler_version = compiler_info["version"]
    toolset_version = (cache_value(cache, "CMAKE_VS_PLATFORM_TOOLSET_VERSION")
                       or project_info["toolset"])
    generator_instance = cache_value(cache, "CMAKE_GENERATOR_INSTANCE")
    generator_toolset = (cache_value(cache, "CMAKE_GENERATOR_TOOLSET")
                         or toolset_version)
    windows_sdk = project_info["windows_sdk"]
    version_output = None
    if compiler and Path(compiler).is_file():
        version_result = subprocess.run([compiler, "/Bv"], capture_output=True,
                                        text=True, encoding="utf8", errors="replace")
        version_output = {"returncode": version_result.returncode,
                          "output": version_result.stdout + version_result.stderr}
    cmake_version = run([cmake, "--version"]).splitlines()[0]
    cuda_header_hash = sha256(cuda_include / "cuda.h")
    build_key = hashlib.sha256(json.dumps(
        {"sources": source_hashes, "nvrtc": nvrtc_record,
         "cuda_h_sha256": cuda_header_hash, "cmake": cmake_version,
         "msvc_toolset": toolset_version, "msvc_version": compiler_version,
         "host_flags": HOST_FLAGS}, sort_keys=True).encode()).hexdigest()[:16]
    artifact_dir = build_dir / "artifacts" / build_key
    artifact_dir.mkdir(parents=True, exist_ok=True)
    dll_hash = immutable_copy(dll, artifact_dir / ("uvck_gpu_" + sha256(dll)[:16] + ".dll"))
    ptx_hash = immutable_copy(output, artifact_dir / ("uvck_gpu_" + sha256(output)[:16] + ".ptx"))
    gpu_identity = {
        "library": next(p.name for p in artifact_dir.glob("*.dll") if sha256(p) == dll_hash),
        "library_sha256": dll_hash,
        "ptx": next(p.name for p in artifact_dir.glob("*.ptx") if sha256(p) == ptx_hash),
        "ptx_sha256": ptx_hash,
        "abi": 1,
        "minimum_cuda_driver": 12040,
        "minimum_compute_capability": 89,
        "source_sha256": {name: source_hashes[name]
                           for name in ("gpu_driver.hpp", "gpu_metric.cu")},
        "engine_source_sha256": source_hashes["engine_gpu.cpp"],
        "build_key": build_key,
    }
    (artifact_dir / "gpu.json").write_text(json.dumps(gpu_identity, indent=2) + "\n",
                                             encoding="utf8")
    provenance = {
        "build_key": build_key,
        "platform": "Windows x64",
        "generator": "Visual Studio 17 2022 -A x64",
        "cmake": cmake_version,
        "msvc_compiler": compiler,
        "msvc_compiler_id": compiler_id,
        "msvc_compiler_version": compiler_version,
        "generator_instance": generator_instance,
        "generator_toolset": generator_toolset,
        "msvc_toolset_version": toolset_version,
        "windows_sdk_version": windows_sdk,
        "msvc_version_output": version_output,
        "cuda_h_sha256": cuda_header_hash,
        "host_flags": list(HOST_FLAGS),
        "nvrtc": nvrtc_record,
        "nvrtc_options": list(NVRTC_OPTIONS),
        "source_sha256": source_hashes,
        "build_script_sha256": sha256(Path(__file__).resolve()),
        "artifacts": {"gpu_json": str(artifact_dir / "gpu.json"),
                      "library_sha256": dll_hash, "ptx_sha256": ptx_hash},
        "commands": {"configure": [str(item) for item in configure],
                     "build": [str(cmake), "--build", str(cmake_build),
                               "--config", "Release", "--target", "uvck_gpu"]},
        "bitwise_reproducibility_claim": False,
        "configure_output": configure_log,
        "build_output": build_log,
    }
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n",
                                               encoding="utf8")
    print(json.dumps({"artifacts": str(artifact_dir), "provenance": str(build_dir / "provenance.json")},
                     indent=2))


if __name__ == "__main__":
    main()
