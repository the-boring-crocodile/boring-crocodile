"""Native, read-only account and packaged-hook proof for the one-time importer."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import tempfile

import boring_deploy

from .site_host import Host
from .site_layout import write_file, site_lock
from .site_model import toml_document


LOAD = ("from pathlib import Path; import sys; "
        "from boring_crocodile.settings import Settings; "
        "Settings.load(Path(sys.argv[1]), environment=sys.argv[2] or None, "
        "ponds=tuple(sys.argv[3].split(',')) if not sys.argv[2] else None); print('ok')")
CHECK = """import importlib.util, sys
from pathlib import Path
candidate, home, config = map(Path, sys.argv[1:])
sys.path.insert(0, str(candidate / "deploy.d"))
import boring_deploy
spec = importlib.util.spec_from_file_location("adoption_check", candidate / "deploy.d/steps.py")
steps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(steps)
release = boring_deploy.Release.load(candidate)
release.details["config"] = str(config)
ctx = boring_deploy.Context(release, home, None, lambda line: print(line, file=sys.stderr), new=candidate)
steps.check(ctx)
print("ok")
"""


def _probe(command):
    try:
        with boring_deploy.root_child(command, spawn=subprocess.Popen, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True, start_new_session=True) as process:
            try:
                stdout, stderr = process.communicate(timeout=120)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                raise
            result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    except (OSError, subprocess.SubprocessError, boring_deploy.Failure) as error:
        return dict(status="failed", error=str(error))
    if result.returncode:
        return dict(status="failed", error=(result.stderr or result.stdout).strip(),
                    exit_code=result.returncode)
    return dict(status="ok")


def prove(site, artifact_root):
    """All writes are below one private temporary root; no deploy switch or start."""

    accounts = [dict(role="controller", pond="", identity=site.controller)]
    accounts += [dict(role="worker", pond=name, identity=p.identity) for name, p in site.ponds.items()]
    report = dict(status="failed", accounts=[
        dict(role=row["role"], pond=row["pond"], account=row["identity"].name,
             uid=row["identity"].uid, gid=row["identity"].gid) for row in accounts],
        deploy_check=dict(status="failed"), cleanup="pending")
    temporary = None
    try:
        if os.geteuid() != 0:
            raise ValueError("Native adoption proof requires root to load the copy as the bound UID/GID accounts")
        if artifact_root is None:
            raise ValueError("Native adoption proof requires the verified candidate artifact")
        with tempfile.TemporaryDirectory(prefix=".crocodile-adoption-proof-", dir="/opt") as temporary:
            root = Path(temporary)
            host = Host()
            # No directory listing; description files enforce their own groups.
            # The proof carries no copied secret or caller-private input.
            root.chmod(0o711)
            config = root / "description/config.toml"
            config.parent.mkdir(mode=0o700)
            write_file(config, toml_document(site.document), mode=0o600, create=True)
            for name, pond in site.ponds.items():
                target = config.parent / "ponds" / name / "config.toml"
                target.parent.mkdir(parents=True, mode=0o700)
                write_file(target, toml_document(pond.document), mode=0o600, create=True)
            with site_lock(config.parent, owner=host.owner):
                host.apply_description_permissions(host.description_permissions(site, config))
            release = boring_deploy.Release.load(Path(artifact_root))
            logged = []
            context = boring_deploy.Context(release, root / "runtime", None, logged.append)
            boring_deploy.Venv().prepare(context)  # Offline preparation only, inside root.
            # prepare normalizes the release, but its runtime/release ancestors
            # inherit the caller's umask. Leave the private description outside.
            boring_deploy.readable_by_all(context.home)
            candidate = context.new
            python = str(candidate / "bin/python3")
            for row, result in zip(accounts, report["accounts"]):
                identity = row["identity"]
                result.update(_probe([
                    "/usr/bin/setpriv", "--reuid", str(identity.uid), "--regid", str(identity.gid),
                    *(["--groups", ",".join(str(p.identity.gid) for p in site.ponds.values() if p.enabled)]
                      if row["role"] == "controller" else ["--clear-groups"]),
                    "--no-new-privs", python, "-I", "-B", "-c",
                    LOAD, str(config), row["pond"], ",".join(n for n,p in site.ponds.items() if p.enabled)]))
            # Real check from deploy.d, real Context, real subprocesses. No
            # installed/current link exists in this private runtime home.
            report["deploy_check"] = _probe([
                python, "-I", "-B", "-c", CHECK, str(candidate), str(context.home), str(config)])
            report["status"] = "ok" if all(row["status"] == "ok" for row in [
                *report["accounts"], report["deploy_check"]]) else "failed"
    except (OSError, ValueError, subprocess.SubprocessError, boring_deploy.Failure) as error:
        for result in [*report["accounts"], report["deploy_check"]]:
            if "status" not in result or result["status"] != "ok":
                result.update(status="failed", error=str(error))
        report["status"] = "failed"
    finally:
        report["cleanup"] = "failed" if temporary is not None and Path(temporary).exists() else "done"
        if report["cleanup"] == "failed":
            report.update(status="failed", cleanup_error="Temporary proof directory remains: " + temporary)
    return report
