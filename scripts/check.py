#!/usr/bin/env python3
"""The one check for boring-crocodile: a fresh environment from the hash lock, then the tests.

    python scripts/check.py --common-wheels /path/to/boring-pond-common/dist [--python /usr/bin/python3.12]

Installs the locked dependencies (--require-hashes) into a new virtual
environment, then boring-crocodile from a copy of its
tracked files (so packaging errors surface). The unit profile
(tests/run_profile.py) then runs the source tree's tests, importing from src/
(site/default/generate.py refuses an installed copy), against those installed
dependencies; then the scoped strict mypy gate (the files under [tool.mypy]).
Private names in shipped text are the family export scan's job, not this check's.
Test tools come from requirements-dev.txt, pinned by version, not by hash.
Exit status 0 means every step passed.
"""
import sys
import tempfile
from pathlib import Path

from _develop import load

_, develop = load()

ROOT = Path(__file__).resolve().parents[1]


def main():
    args = develop.arguments(__doc__).parse_args()
    with tempfile.TemporaryDirectory(prefix="crocodile-check-") as tmp:
        tmp = Path(tmp)
        packages = [develop.package(ROOT, tmp / "app")]
        python = develop.environment(develop.uv(), args.python, tmp / "venv", [ROOT / "requirements.lock"],
                                     args.common_wheels, packages, ROOT / "scripts/build-constraints.txt",
                                     ["-r", str(ROOT / "requirements-dev.txt")])
        develop.run(python, ROOT / "tests/run_profile.py", "unit", cwd=ROOT)
        develop.run(python, "-m", "mypy", cwd=ROOT)  # the scoped strict gate: the files under [tool.mypy]
    print("all boring-crocodile checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
