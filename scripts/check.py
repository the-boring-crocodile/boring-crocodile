#!/usr/bin/env python3
"""The one check for boring-crocodile: a fresh environment from the hash lock, then the tests.

    python scripts/check.py --common-wheels /path/to/boring-pond-common/dist [--python /usr/bin/python3.12]

Installs the locked dependencies (--require-hashes) into a new virtual
environment, then boring-crocodile and boring-task-state from a copy of their
tracked files (so packaging errors surface). The unit profile
(tests/run_profile.py) then runs the source tree's tests, importing from src/
(site/default/generate.py refuses an installed copy), against those installed
dependencies; then optional extra checks in a local pond/checks folder, when that
directory exists (a published copy has none); then the scoped strict mypy gate
(the files under [tool.mypy]).
Test tools come from requirements-dev.txt, pinned by version, not by hash.
Exit status 0 means every step passed.
"""
import os
import sys
import tempfile
from pathlib import Path

from _develop import load

_, develop = load()

ROOT = Path(__file__).resolve().parents[1]


def pond_checks(python):
    """Optional local checks in pond/checks (not in the published repository), importing from src/ like the unit profile."""
    if not (ROOT / "pond/checks").is_dir():
        print("pond/checks absent: skipped")
        return
    path = os.pathsep.join(str(ROOT / p) for p in ("src", "packages/boring-task-state/src", "tests"))
    develop.run(python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "pond/checks", cwd=ROOT,
                env=dict(os.environ, PYTHONPATH=path))


def main():
    args = develop.arguments(__doc__).parse_args()
    with tempfile.TemporaryDirectory(prefix="crocodile-check-") as tmp:
        tmp = Path(tmp)
        packages = [develop.package(ROOT, tmp / "app"),
                    develop.package(ROOT, tmp / "task-state", "packages/boring-task-state")]
        python = develop.environment(develop.uv(), args.python, tmp / "venv", [ROOT / "requirements.lock"],
                                     args.common_wheels, packages, ROOT / "scripts/build-constraints.txt",
                                     ["-r", str(ROOT / "requirements-dev.txt")])
        develop.run(python, ROOT / "tests/run_profile.py", "unit", cwd=ROOT)
        pond_checks(python)
        develop.run(python, "-m", "mypy", cwd=ROOT)  # the scoped strict gate: the files under [tool.mypy]
    print("all boring-crocodile checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
