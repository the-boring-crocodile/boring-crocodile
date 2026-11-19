#!/usr/bin/env python3
"""One-release offline adoption helper. Use an existing Python 3.12+; --dry-run first."""
import os
from pathlib import Path
import sys

if sys.version_info < (3, 12):
    sys.exit("Python 3.12+ is required; use the installed Crocodile's bin/python3")
if not (sys.flags.isolated and sys.flags.dont_write_bytecode):
    os.execv(sys.executable, [sys.executable, "-I", "-B", os.path.abspath(__file__), *sys.argv[1:]])
here = Path(__file__).resolve().parent
sys.path.insert(0, str(here / "deploy.d"))
import boring_deploy
boring_deploy.verify_files(here)
# The importer/model and their pure Python primitives are read directly from
# this verified artifact. No environment installation or installed-code fallback.
wheels = sorted((here / "wheels").glob("*.whl"))
if len(list((here / "wheels").glob("boring_crocodile_mcp-*.whl"))) != 1:
    sys.exit("Expected exactly one candidate Crocodile wheel")
sys.path[:0] = [str(path) for path in wheels]
from boring_crocodile.site_adopt import main
sys.exit(main(artifact_root=here))
