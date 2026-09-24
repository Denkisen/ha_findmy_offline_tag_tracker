"""Deployable entrypoint: run the scanner package from a plain directory or zip.

Deployment layout on the HA host (e.g. /opt/fhn_presence):

    /opt/fhn_presence/
    ├── runner.py        (this file)
    └── fhn_presence.zip (the scanner package, or an unpacked scanner/ dir)

    python3 runner.py --config /opt/fhn_presence/config.env

``fhn_presence.zip`` is built in the dev repo with ``make-zip.sh`` (or
``python3 -m scanner.build_zip``) and contains the ``scanner`` package plus a
``requirements.txt``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

def _bootstrap() -> None:
    """Put the scanner package on sys.path, whether it is a zip or a dir."""
    zips = list(HERE.glob("fhn_presence.zip")) + list(HERE.glob("scanner.zip"))
    if zips:
        sys.path.insert(0, str(zips[0]))
    else:
        scanner_dir = HERE / "scanner"
        if scanner_dir.exists():
            sys.path.insert(0, str(HERE))


def main() -> int:
    _bootstrap()
    from scanner.fhn_presence import main as scanner_main

    return scanner_main()


if __name__ == "__main__":
    raise SystemExit(main())
