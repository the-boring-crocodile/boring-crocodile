#!/usr/bin/env python3
"""The one check for boring-crocodile: a fresh environment from the hash lock, then the tests.

    python scripts/check.py --common-wheels /path/to/boring-pond-common/dist [--python /usr/bin/python3.12]

Installs the locked dependencies (--require-hashes) into a new virtual
environment, then boring-crocodile from a copy of its
tracked files (so packaging errors surface). Pytest runs the source tree's full
test directory against those installed dependencies. The installed-wheel handbook
resource check runs in isolation, followed by the scoped strict mypy gate.
Private names in shipped text are the family export scan's job, not this check's.
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


def main():
    args = develop.arguments(__doc__).parse_args()
    with tempfile.TemporaryDirectory(prefix="crocodile-check-") as tmp:
        tmp = Path(tmp)
        packages = [develop.package(ROOT, tmp / "app")]
        python = develop.environment(develop.uv(), args.python, tmp / "venv", [ROOT / "requirements.lock"],
                                     args.common_wheels, packages, ROOT / "scripts/build-constraints.txt",
                                     ["-r", str(ROOT / "requirements-dev.txt")])
        # Source imports are required by the site generator and test subprocesses.
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(map(str, (ROOT / "src", ROOT / "tests"))))
        develop.run(python, "-m", "pytest", "-q", "-p", "no:cacheprovider", ROOT / "tests",
                    cwd=ROOT, env=env)
        develop.run(python, "-I", ROOT / "tests/test_setup.py",
                    "SetupTest.test_packaged_handbooks_are_release_resources", cwd=tmp)
        develop.run(python, "-m", "mypy", cwd=ROOT)  # the scoped strict gate: the files under [tool.mypy]
    print("all boring-crocodile checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
