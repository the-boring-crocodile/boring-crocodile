"""Read the declared site and compare it with the common renderer. No reverse inference."""
from __future__ import annotations
import argparse
import json
import subprocess
from pathlib import Path
from typing import Any
from .site_host import Host
from .site_layout import CONFIG_FILE
from .site_model import SiteDescription
from .site_ports import require_protected_service_port
from . import site_render

UNITS = Path("/etc/systemd/system")
TMPFILES = Path("/etc/tmpfiles.d")


def site_check(config: Path = CONFIG_FILE, *, paths: site_render.Paths | None = None,
               host: Host | None = None) -> dict[str, Any]:
    host = host or Host()
    paths = paths or site_render.Paths(Path(config).parent, UNITS, TMPFILES, host.owner)
    report = {key: [] for key in ("understood", "adjustments", "irregular", "unchecked")}
    try:
        proposal, _ = site_render.plan(paths=paths, host=host)
        report["adjustments"] = proposal["adjustments"]
        changed = {row["path"] for row in proposal["changes"]}
        report["understood"] = [{"path": path, "what": "matches declared rendering"}
                                for path in proposal["observed"] if path not in changed]
        report["irregular"] = [{"what": "generated file needs " + row["action"], **row}
                               for row in proposal["changes"]]
        site = SiteDescription.read(paths.config, owner=paths.owner)
        if site.fingerprints() != proposal["sources"]:
            raise ValueError("Site description changed during inspection; rerun site-check")
        if site.state != "removed" and host.path(site.settings.oauth_database_file).exists():
            for service in host.oauth_store(site).service_list():
                name, url = str(service["name"]), str(service["url"])
                try:
                    require_protected_service_port(url, site.settings.protected_ports, name,
                                                   controller_port=site.settings.bind_port)
                except ValueError as error:
                    report["irregular"].append({"what": "unprotected service port",
                                                "service": name, "detail": str(error)})
        if site.state == "running":
            for name, environment in site.runtime_settings().environments.items():
                if not environment.command_execution_enabled:
                    continue
                try:
                    policy = host.task_policy(environment)
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    report["unchecked"].append({"pond": name, "why": "task policy unavailable",
                                                "detail": str(error)})
                    continue
                if policy["loaded_policy"] != "matches":
                    report["irregular"].append({"what": "task policy differs", "pond": name,
                                                "policy": policy})
        for row in proposal["permissions"]:
            expected = {key: row[key] for key in ("uid", "gid", "mode")} | {"acl": []}
            path = Path(row["path"])
            actual = host.description_metadata(path) if path.exists() else None
            if actual != expected:
                report["irregular"].append({"what": "description permissions differ",
                    "path": row["path"], "expected": expected, "actual": actual})
        for row in proposal["groups"]:
            if row["add"] or row["remove"]:
                report["irregular"].append({"what": "Unix membership differs", **row})
    except PermissionError as error:
        report["unchecked"].append({"path": str(error.filename or paths.config), "why": "needs root"})
    except (ValueError, OSError, KeyError) as error:
        report["irregular"].append({"what": "cannot validate desired site", "detail": str(error)})
    counts = {key: len(items) for key,items in report.items()}
    status = "irregular" if counts["irregular"] else "incomplete" if counts["unchecked"] else "regular"
    return {"status": status, **report, "counts": counts}


def summary(report: dict[str, Any]) -> str:
    lines = ["site-check: " + report["status"] + " " + json.dumps(report["counts"], sort_keys=True)]
    for key in ("irregular", "adjustments", "unchecked"):
        lines.extend("  " + key + ": " + json.dumps(row, sort_keys=True) for row in report[key])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_FILE)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = site_check(args.config)
    print(json.dumps(report, indent=2) if args.json else summary(report))
    return 0 if report["status"] == "regular" else 1
