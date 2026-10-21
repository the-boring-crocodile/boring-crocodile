#!/usr/bin/env python3
"""Build the Crocodile's release artifact for one target Python, start to finish.

    python scripts/build.py --common-wheels DIR --out NEW_DIR [--python-version 3.14]

1. NEW_DIR/wheelhouse: the application wheels (boring-crocodile, boring-task-state,
   built from the tracked files) and every locked wheel for the target, with
   SHA256SUMS and MANIFEST.json
2. NEW_DIR/boring-crocodile-<version>-linux-x86_64.tar.gz: the wheelhouse, a
   standalone CPython and the deploy (boring-deploy from the Common wheels);
   `sudo <unpacked>/deploy` installs it into /opt/boring-crocodile.
Prints the artifact's path and SHA256 last.
"""
import hashlib
import sys
from pathlib import Path

from _develop import load

artifact, develop = load()

ROOT = Path(__file__).resolve().parent.parent
OAUTH_SCHEMA = 5  # the OAuth store this release writes and reads; it migrates none


def main() -> int:
    args = develop.arguments(__doc__, build=True).parse_args()
    develop.clean_tree(ROOT, args.allow_dirty)
    out = develop.new_directory(args.out)
    wheelhouse = out / "wheelhouse"
    wheelhouse.mkdir()
    lock = ROOT / "requirements.lock"
    wheels = develop.wheelhouse(ROOT, wheelhouse, sources=[".", "packages/boring-task-state"], lock_file=lock,
                                common_wheels=args.common_wheels, python_version=args.python_version,
                                constraints=ROOT / "scripts/build-constraints.txt")
    develop.record(ROOT, wheelhouse, wheels, lock, args.python_version)
    print(f"{len(wheels)} wheels in {wheelhouse}")
    (app,) = wheelhouse.glob("boring_crocodile_mcp-*.whl")
    (state,) = wheelhouse.glob("boring_task_state-*.whl")
    python = artifact.standalone_python(develop.uv(), develop.PYTHONS[args.python_version], out / "python.tar.gz")
    files = {f"wheels/{p.name}": p for p in sorted(wheelhouse.glob("*.whl"))}
    files["wheels/requirements.lock"] = lock
    files["python.tar.gz"] = python
    files.update({name: ROOT / name for name in ("LICENSE", "COPYING.md")})
    release = {"component": "boring-crocodile", "version": app.name.split("-")[1], "runtime": "venv",
               "default_home": "/opt/boring-crocodile", "python": "python.tar.gz",
               "app_wheels": [app.name, state.name], "config": "/etc/boring-crocodile/config.toml",
               "data_generation": OAUTH_SCHEMA, "upgrades_from": OAUTH_SCHEMA,
               "commit": develop.git(ROOT, "rev-parse", "HEAD").strip()}
    archive = artifact.assemble(out, release, ROOT / "deploy" / "steps.py", files)
    print(f"{archive} {hashlib.sha256(archive.read_bytes()).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
