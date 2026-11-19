#!/usr/bin/env python3
"""Build the Crocodile's release artifact for one target Python, start to finish.

    python scripts/build.py --common-wheels DIR --out NEW_DIR [--python-version 3.14]

1. NEW_DIR/wheelhouse: the application wheel (boring-crocodile, built from
   the tracked files) and every locked wheel for the target, with
   SHA256SUMS and MANIFEST.json
2. NEW_DIR/boring-crocodile-<version>-linux-x86_64.tar.gz: the wheelhouse, a
   standalone CPython and the deploy (boring-deploy from the Common wheels);
   `sudo <unpacked>/deploy` installs it into /opt/boring-crocodile.
Prints the artifact's path and SHA256 last.
"""
import ast
import hashlib
import subprocess
import sys
from pathlib import Path

from _develop import load

artifact, develop = load()

ROOT = Path(__file__).resolve().parent.parent
def oauth_schema(name="VERSION"):
    """The runtime owns the schema; read its literal without loading the application."""
    tree = ast.parse((ROOT / "src/boring_crocodile/oauth_records.py").read_text())
    (value,) = [node.value for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)]
    return ast.literal_eval(value)


OAUTH_SCHEMA = oauth_schema()
OAUTH_UPGRADES_FROM = oauth_schema("UPGRADES_FROM")


def main() -> int:
    args = develop.arguments(__doc__, build=True).parse_args()
    develop.clean_tree(ROOT, args.allow_dirty)
    out = develop.new_directory(args.out)
    wheelhouse = out / "wheelhouse"
    wheelhouse.mkdir()
    lock = ROOT / "requirements.lock"
    wheels = develop.wheelhouse(ROOT, wheelhouse, sources=["."], lock_file=lock,
                                common_wheels=args.common_wheels, python_version=args.python_version,
                                constraints=ROOT / "scripts/build-constraints.txt")
    develop.record(ROOT, wheelhouse, wheels, lock, args.python_version)
    print(f"{len(wheels)} wheels in {wheelhouse}")
    (app,) = wheelhouse.glob("boring_crocodile_mcp-*.whl")
    python = artifact.standalone_python(develop.uv(), develop.PYTHONS[args.python_version], out / "python.tar.gz")
    files = {f"wheels/{p.name}": p for p in sorted(wheelhouse.glob("*.whl"))}
    files["wheels/requirements.lock"] = lock
    files["python.tar.gz"] = python
    files["adopt-site.py"] = ROOT / "deploy" / "adopt-site.py"  # one release only; no store migration
    files["deploy.d/site_layout.py"] = ROOT / "src/boring_crocodile/site_layout.py"
    files.update({name: ROOT / name for name in ("LICENSE", "COPYING.md")})
    release = {"component": "boring-crocodile", "version": app.name.split("-")[1], "runtime": "venv",
               "default_home": "/opt/boring-crocodile", "python": "python.tar.gz",
               "app_wheels": [app.name], "config": "/etc/boring-crocodile/config.toml",
               "data_generation": OAUTH_SCHEMA, "upgrades_from": OAUTH_UPGRADES_FROM,
               "commit": develop.git(ROOT, "rev-parse", "HEAD").strip()}
    archive = artifact.assemble(out, release, ROOT / "deploy" / "steps.py", files)
    subprocess.run([sys.executable, "-I", "-B", str(ROOT / "tests/check_deploy_artifact.py"),
                    str(archive.parent / ("boring-crocodile-" + release["version"]))], check=True)
    print(f"{archive} {hashlib.sha256(archive.read_bytes()).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
