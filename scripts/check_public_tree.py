# SPDX-License-Identifier: GPL-3.0-or-later
"""Audit distributable source and document hygiene, without deleting anything."""
import ast
import json
from pathlib import Path
import re
import subprocess
import tomllib

ROOT=Path(__file__).resolve().parents[1]
IGNORED={'.git','build','dist','test-results','__pycache__','.venv'}
ALLOWED={'.py','.cpp','.cu','.hpp','.toml','.md','.txt','.json','.yml','.yaml'}
PATTERNS={
    'private_machine_path':re.compile(r'(?i)\b[a-z]:[\\/](?:users|ai work|steamlibrary)'
                                     r'|/ho' r'me/[^/\s]+|/Us' r'ers/[^/\s]+'),
    'access_token':re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9]{24,})'),
    'private_key':re.compile(r'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----'),
    'excluded_module':re.compile(r'\bzen_quad\b'),
}


def files():
    if (ROOT/'.git').exists():
        names=subprocess.check_output(['git','ls-files','--cached','-z'],cwd=ROOT).decode().split('\0')
        return [ROOT/name for name in names if name]
    return [p for p in ROOT.rglob('*') if p.is_file() and not set(p.relative_to(ROOT).parts)&IGNORED]


def main():
    errors=[]
    paths=files()
    assert paths,'No public files found'
    for p in paths:
        relative=p.relative_to(ROOT)
        if set(relative.parts)&IGNORED:
            errors.append((str(relative),'ignored directory tracked'));continue
        if p.is_symlink():errors.append((str(relative),'symlink'));continue
        if p.suffix not in ALLOWED and p.name not in {'LICENSE','NOTICE','.gitignore','.gitattributes','CMakeLists.txt'}:
            errors.append((str(relative),'unexpected file type'));continue
        text=p.read_text(encoding='utf8')
        for name,pattern in PATTERNS.items():
            if name=='excluded_module' and p.name==Path(__file__).name:continue
            if pattern.search(text):errors.append((str(relative),name))
        if p.suffix=='.py':ast.parse(text,filename=str(relative))
    manifest=tomllib.loads((ROOT/'uv_constraint_kit/blender_manifest.toml').read_text(encoding='utf8'))
    assert manifest['license']==['SPDX:GPL-3.0-or-later']
    assert not (ROOT/'uv_constraint_kit'/'zen_quad.py').exists()
    symmetry=(ROOT/'uv_constraint_kit/symmetry.py').read_text(encoding='utf8')
    assert '2025 Oxicid' in symmetry and 'GPL-3.0-or-later' in symmetry
    assert (ROOT/'THIRD_PARTY_NOTICES.md').read_bytes()==(ROOT/'uv_constraint_kit/THIRD_PARTY_NOTICES.md').read_bytes()
    assert (ROOT/'LICENSE').read_bytes()==(ROOT/'uv_constraint_kit/LICENSE').read_bytes()
    assert not errors,errors
    print(json.dumps(dict(passed=True,files=len(paths),version=manifest['version'],source_only=True)))


if __name__=='__main__':main()
