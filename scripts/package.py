# SPDX-License-Identifier: GPL-3.0-or-later
"""Create a Blender extension ZIP from an explicit source and engine allowlist."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "uv_constraint_kit"
RUNTIME = """__init__.py blender_manifest.toml circle_ops.py circle.py const.py core.py
i18n.py LICENSE live_symmetry.py metric.py native_engine.py ops.py overlay.py prefs.py
proxy.py quad_grid.py quadrify.py recovery.py rectangle.py refine.py safety.py shape_constraints.py
shape_ops.py symmetry.py terms_zh.py ui.py untangle.py""".split()
NATIVE_SOURCE = "CMakeLists.txt build_gpu.py engine.cpp engine_gpu.cpp gpu_driver.hpp gpu_metric.cu".split()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def native_members(folder: Path) -> dict[str, Path]:
    result = {}
    for metadata, definitions in (("engine.json", (("library", "uvck_engine_", ".dll", "sha256"),)),
                                  ("gpu.json", (("library", "uvck_gpu_", ".dll", "library_sha256"),
                                                ("ptx", "uvck_gpu_", ".ptx", "ptx_sha256")))):
        path = folder / metadata
        if not path.is_file():
            if metadata == "engine.json":
                raise FileNotFoundError("The selected native directory has no engine.json")
            continue
        identity = json.loads(path.read_text(encoding="utf8"))
        if identity.get("abi") != 1:
            raise ValueError(f"Unsupported ABI in {metadata}")
        if metadata == "engine.json" and identity.get("source_sha256") != sha256(ADDON / "native_source" / "engine.cpp"):
            raise ValueError("CPU engine was built from a different engine.cpp")
        if metadata == "gpu.json":
            for name in ("gpu_driver.hpp", "gpu_metric.cu"):
                if identity.get("source_sha256", {}).get(name) != sha256(ADDON / "native_source" / name):
                    raise ValueError(f"GPU source checksum mismatch: {name}")
            if identity.get("engine_source_sha256") != sha256(ADDON / "native_source" / "engine_gpu.cpp"):
                raise ValueError("GPU engine was built from a different engine_gpu.cpp")
        result[f"bin/windows-x64/{metadata}"] = path
        for key, prefix, suffix, checksum in definitions:
            name = identity[key]
            if (not isinstance(name, str) or Path(name).name != name
                    or not name.startswith(prefix) or not name.endswith(suffix)):
                raise ValueError(f"Invalid engine filename in {metadata}")
            artifact = folder / name
            if artifact.is_symlink() or artifact.resolve().parent != folder.resolve():
                raise ValueError("Engine artifact must be a regular file inside its directory")
            if sha256(artifact) != identity[checksum]:
                raise ValueError(f"Engine checksum mismatch: {name}")
            result[f"bin/windows-x64/{name}"] = artifact
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--native-dir", type=Path,
                        help="Engine directory produced by build_native.py; omit for Python-only ZIP")
    parser.add_argument("--require-gpu", action="store_true",
                        help="Require a validated GPU DLL and PTX in the package")
    args = parser.parse_args()
    if args.require_gpu and args.native_dir is None:
        parser.error("--require-gpu requires --native-dir")
    members = {name: ADDON / name for name in RUNTIME}
    members.update({f"native_source/{name}": ADDON / "native_source" / name for name in NATIVE_SOURCE})
    for name in ("THIRD_PARTY_NOTICES.md", "NOTICE"):
        for base in (ROOT, ADDON):
            if (base / name).is_file():
                members[name] = base / name
    for base in (ROOT, ADDON):
        for path in sorted((base / "LICENSES").glob("*")):
            if path.is_file() and path.suffix.lower() in {".txt", ".md"}:
                members[f"LICENSES/{path.name}"] = path
    if args.native_dir is not None:
        members.update(native_members(args.native_dir.resolve()))
    if args.require_gpu:
        gpu_prefix = "bin/windows-x64/uvck_gpu_"
        if ("bin/windows-x64/gpu.json" not in members
                or not any(name.startswith(gpu_prefix) and name.endswith(".dll") for name in members)
                or not any(name.startswith(gpu_prefix) and name.endswith(".ptx") for name in members)):
            parser.error("--require-gpu needs a validated GPU DLL and PTX in --native-dir")
    for name, path in members.items():
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(f"Required regular source file missing: {name}")
    output = args.output.resolve()
    if output.suffix.lower() != ".zip":
        parser.error("--output must end in .zip")
    if output == ADDON or ADDON in output.parents:
        parser.error("Write packages outside the add-on source directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".zip", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, path in sorted(members.items()):
                info = zipfile.ZipInfo(name, date_time=(2024, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, path.read_bytes())
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None or set(archive.namelist()) != set(members):
                raise ValueError("Archive verification failed")
            for name, path in members.items():
                if hashlib.sha256(archive.read(name)).hexdigest() != sha256(path):
                    raise ValueError(f"Archive source changed during packaging: {name}")
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    print(json.dumps({"archive": str(output), "files": len(members), "sha256": sha256(output),
                      "native": args.native_dir is not None}, indent=2))


if __name__ == "__main__":
    main()
