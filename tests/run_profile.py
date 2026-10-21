"""Run a named test profile with concise success output and useful failure context."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("profile", choices=("unit", "cleanup", "setup"))
args = parser.parse_args()

root = Path(__file__).resolve().parents[1]
env = os.environ.copy()
# Pond Common (boring-registry, -file-core, -service-kit, -worker-kit) comes from
# the interpreter's environment: install the locked wheels or an editable Common
# checkout first (README "Development").
try:
    import boring_registry, boring_file_core, boring_service_kit, boring_worker_kit  # noqa: F401
except ImportError as missing:
    sys.exit(f"Pond Common is not installed in {sys.executable}: {missing.name}")
env["PYTHONPATH"] = os.pathsep.join((str(root / "src"), str(root / "packages/boring-task-state/src"), str(root / "tests"), env.get("PYTHONPATH", "")))
commands = {
    "unit": [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
    "cleanup": [sys.executable, "-m", "unittest", "test_task_client", "test_containment_recovery", "test_execution", "test_batch", "test_jobs"],
    "setup": [sys.executable, "-m", "unittest", "tests.test_setup", "tests.test_setup_apply", "tests.test_setup_additional", "tests.test_setup_services", "tests.test_uninstall", "tests.test_site_setup"],
}
command = commands[args.profile]

started = time.monotonic()
result = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True)
combined = result.stdout + result.stderr
elapsed = round(time.monotonic() - started, 3)
match = re.search(r"Ran (\d+) tests? in", combined)
summary = {"profile": args.profile, "status": "passed" if result.returncode == 0 else "failed", "seconds": elapsed}
if match:
    summary["tests"] = int(match.group(1))
    skips = re.search(r"^(?:OK|FAILED)\b[^\n]*\bskipped=(\d+)", combined, re.MULTILINE)
    summary["skipped"] = int(skips.group(1)) if skips else 0
    summary["executed"] = summary["tests"] - summary["skipped"]
print(json.dumps(summary, sort_keys=True), flush=True)
if result.returncode:
    print(combined[-12000:], file=sys.stderr, end="")
raise SystemExit(result.returncode)
