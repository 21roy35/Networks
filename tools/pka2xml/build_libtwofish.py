#!/usr/bin/env python3
"""Build the libtwofish shared library used by pka2xml.

Compiles ``vendor/twofish/twofish.c`` into ``pka2xml/libtwofish.so`` (Linux/macOS)
or ``pka2xml/libtwofish.dll`` (Windows). Uses whichever compiler is on PATH:
``cc``, ``gcc``, or ``clang``. Falls back to ``tcc`` (Tiny C Compiler) if those
are unavailable.

Run as ``python -m pka2xml.build``.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "vendor" / "twofish" / "twofish.c"
PKG = ROOT / "pka2xml"


def _output_path() -> Path:
    sysname = platform.system().lower()
    if "windows" in sysname:
        return PKG / "libtwofish.dll"
    if "darwin" in sysname:
        return PKG / "libtwofish.dylib"
    return PKG / "libtwofish.so"


def _find_compiler() -> str:
    for cand in ("cc", "gcc", "clang", "tcc"):
        path = shutil.which(cand)
        if path:
            return path
    raise SystemExit(
        "No C compiler found. Install gcc/clang or set the CC environment variable."
    )


def main() -> int:
    if not SRC.exists():
        raise SystemExit(f"twofish source missing: {SRC}")

    cc = os.environ.get("CC") or _find_compiler()
    out = _output_path()
    out.parent.mkdir(parents=True, exist_ok=True)

    cmd = [cc, "-shared", "-fPIC", "-O2", "-o", str(out), str(SRC)]
    if "tcc" in Path(cc).name:
        # tcc needs to be pointed at its own include / runtime directory when
        # invoked from outside the install prefix.
        tcc_root = Path(cc).resolve().parent.parent / "lib"
        for candidate in tcc_root.rglob("tcc"):
            if candidate.is_dir():
                cmd.insert(1, f"-B{candidate}")
                break

    print("$", " ".join(cmd))
    res = subprocess.run(cmd)
    if res.returncode != 0:
        return res.returncode

    # Strip exec-stack permission if a patchelf-like tool is available — required on
    # hardened glibc when the compiler does not emit the .note.GNU-stack section.
    pe = shutil.which("patchelf")
    if pe and out.suffix == ".so":
        subprocess.run([pe, "--clear-execstack", str(out)], check=False)

    print(f"[+] built {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
