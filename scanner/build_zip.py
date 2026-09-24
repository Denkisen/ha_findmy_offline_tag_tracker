#!/usr/bin/env python3
"""Build the deployable fhn_presence.zip for the HA host."""

from __future__ import annotations

import shutil
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
OUT = REPO / "dist" / "fhn_presence.zip"
MODULES = ["__init__.py", "fhn_const.py", "ec.py", "fhn_codec.py", "presence_engine.py", "fhn_presence.py"]


def main() -> int:
    OUT.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in MODULES:
            src = HERE / name
            if not src.exists():
                print(f"missing {src}", file=sys.stderr)
                return 1
            zf.write(src, f"scanner/{name}")
        # requirements for the host venv
        reqs = HERE / "requirements.txt"
        zf.write(reqs, "requirements.txt")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
