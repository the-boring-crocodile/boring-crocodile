#!/usr/bin/env python3
"""Regenerate requirements.lock: every runtime dependency pinned with hashes.

One universal lock for Python 3.10-3.14 on all platforms. Pond Common comes
from its wheels (--common-wheels, until Common is on a package index);
everything else from the package index. Existing pins are kept unless you pass
--upgrade-package NAME (repeatable).

    python scripts/lock.py --common-wheels /path/to/boring-pond-common/dist
"""
import sys
from pathlib import Path

from _develop import load

_, develop = load()

ROOT = Path(__file__).resolve().parent.parent


def main():
    args = develop.lock_arguments(__doc__).parse_args()
    develop.lock(args, ROOT / "requirements.lock", [ROOT / "pyproject.toml"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
