"""Reconcile the standard site's description. Rerunning always repairs the runtime."""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass, replace
import difflib
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import tomllib

from .approval import confirm_apply, plan_digest
from .site_host import Host as Host, SiteConflict as SiteConflict
from .site_layout import STATE_DIR, budget_slice, site_lock, write_file
from .site_model import SiteDescription, toml_document
from .site_templates import render_description
from .site_ports import PIRATE_BIND_DROPIN, socket_bind_denials

CONTROLLER = "boring-crocodile-mcp.service"
LISTENER = "boring-crocodile-mcp.socket"
TARGET = "boring-crocodile-mcp.target"
UNIT_SUFFIXES = (".service", ".socket", ".slice", ".target")


@dataclass(frozen=True)
class Paths:
    state: Path = STATE_DIR
    units: Path = Path("/etc/systemd/system")
    tmpfiles: Path = Path("/etc/tmpfiles.d")
    owner: int = 0

    @property
    def config(self):
        return self.state / "config.toml"

    def location(self, name):
        return (self.tmpfiles if name.endswith(".conf") and "/" not in name else self.units) / name


def _read(path, owner):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != owner
                or info.st_mode & 0o022 or info.st_size > 1024**2):
            raise SiteConflict("Unsafe site file: " + str(path))
        body = stream.read(1024**2 + 1)
        if len(body) > 1024**2:
            raise SiteConflict("Site file grew beyond 1 MiB: " + str(path))
        return body


def _sha(body):
    return hashlib.sha256(body).hexdigest() if body is not None else None


def _protection(body):
    return tuple(line.strip() for line in (body or b"").decode().splitlines()
                 if line.strip().startswith(("SocketBindAllow=", "SocketBindDeny=")))


def _directive_rows(text):
    """Observe section/key pairs, including continuations; systemd validates syntax."""
    section, pending = "", ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        pending += line
        if pending.endswith("\\"):
            pending = pending[:-1] + " "
            continue
        line, pending = pending, ""
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif "=" in line:
            key, value = line.split("=", 1)
            yield section, key.strip(), value.strip()


def dropin_adjustments(path, body, generated, unit):
    """Show the complete external file; flag generated settings without owning precedence."""
    text = body.decode()
    owned = {(section, key) for section, key, _ in _directive_rows(generated)}
    result = [{"path": str(path), "unit": unit, "content": text}]
    for section, key, value in _directive_rows(text):
        row = {"path": str(path), "unit": unit, "section": section,
               "directive": key, "value": value}
        if (section, key) in owned:
            row["warning"] = "Overrides a generated setting: [" + section + "] " + key
        result.append(row)
    return result


def _pirate_protection(site, proposed, paths, host):
    """Crocodile owns only its bind drop-in, including for older Pirate releases."""
    names = {p.name for p in paths.units.glob("system-pirate*.slice")}
    names.update("system-pirate" + name + ".slice"
                 for name in proposed.settings.pirate_service_callers)
    retired = {"system-pirate" + name + ".slice"
               for name in site.settings.pirate_service_callers} - names
    retired.update(p.parent.name.removesuffix(".d")
                   for p in paths.units.glob("system-pirate*.slice.d/" + PIRATE_BIND_DROPIN))
    server = proposed.settings
    body = "[Slice]\n" + socket_bind_denials(server.bind_port, server.protected_ports) + "\n"
    desired = {}
    known = {}
    for unit in names | retired:
        key = unit + ".d/" + PIRATE_BIND_DROPIN
        host.protected(paths.location(key).parent)
        known[key] = body
        if unit in names:
            desired[key] = body
    return desired, known


@dataclass(frozen=True)
class ServicePortReport:
    ports: tuple[int, ...] = ()
    reason: str | None = None


def rendered_service_ports(site: SiteDescription, paths: Paths, host: Host) -> ServicePortReport:
    """Advertise the port policy only when the matching slice files are in place."""
    if site.state != "running":
        return ServicePortReport(reason="site is stopped")
    policy = site.settings
    expected = tuple(socket_bind_denials(policy.bind_port, policy.protected_ports).splitlines())
    pirate, _ = _pirate_protection(site, site, paths, host)
    guards = {paths.location(budget_slice(p.name)): budget_slice(p.name)
              for p in site.ponds.values() if p.enabled}
    guards.update({paths.location(name): name.split(".d/", 1)[0] for name in pirate})
    for location, unit in guards.items():
        if _protection(_read(location, paths.owner)) != expected:
            return ServicePortReport(reason=f"port protection not rendered in {location}")
        for dropin in host.dropins(unit, paths.units):
            if dropin == location:
                continue
            overrides = _directive_rows((_read(dropin, paths.owner) or b"").decode())
            if any(section == "Slice" and (key == "SocketBindAllow"
                                          or key == "SocketBindDeny" and not value)
                   for section, key, value in overrides):
                return ServicePortReport(reason=f"foreign drop-in can override port protection: {dropin}")
    installed = sorted({unit for unit in guards.values() if paths.location(unit).is_file()})
    if not installed:
        return ServicePortReport(reason="port protection not rendered in any installed slice")
    try:
        result = host.command(["/usr/bin/systemctl", "show", "--property=NeedDaemonReload", "--value", *installed])
    except (OSError, subprocess.SubprocessError):
        return ServicePortReport(reason="systemctl could not verify the rendered slices")
    reloads = result.stdout.split()
    if "yes" in reloads:
        return ServicePortReport(reason="systemd daemon-reload is pending")
    if reloads != ["no"] * len(installed):
        return ServicePortReport(reason="systemctl did not return a reload status for each slice")
    return ServicePortReport(tuple(port for port in policy.protected_ports if port != policy.bind_port))


def plan(*, paths=None, host=None, candidate=None):
    paths, host = paths or Paths(), host or Host()
    for directory in (paths.state, paths.units, paths.tmpfiles):
        host.protected(directory)
    site = SiteDescription.read(paths.config, owner=paths.owner)
    proposed = candidate or site
    if set(proposed.ponds) != set(site.ponds):
        raise SiteConflict("Provision declarations explicitly before rendering; render never deletes them")
    ponds = {name: deepcopy(p.document) for name, p in proposed.ponds.items()}
    candidate = SiteDescription.parse(proposed.document, ponds, sources=site.sources)
    host.prerequisites(candidate, allow_permission_drift=True)
    permissions = host.description_permissions(candidate, paths.config)
    desired = render_description(candidate)
    # Disabled declarations remain known, so their base artifacts can be removed
    # without a receipt. Their homes, context, secrets and drop-ins are never removed.
    complete = replace(candidate, state="stopped",
                       ponds={n: replace(p, enabled=True) for n,p in candidate.ponds.items()})
    known = render_description(complete)
    pirate_desired, pirate_known = _pirate_protection(site, candidate, paths, host)
    desired.update(pirate_desired)
    known.update(pirate_known)
    for directory, patterns in ((paths.units, ("boring-crocodile-*", "system-crocodile*.slice")),
                                (paths.tmpfiles, ("boring-crocodile*.conf",))):
        for pattern in patterns:
            for artifact in directory.glob(pattern):
                if artifact.suffix in (*UNIT_SUFFIXES, ".conf") and artifact.name not in known:
                    raise SiteConflict("Undeclared site artifact; adopt or retire it explicitly: " + str(artifact))
    changes, writes, observed, adjustments = [], {}, {}, []
    sensitive = False
    for name in sorted(known):
        target = paths.location(name)
        before = _read(target, paths.owner)
        observed[str(target)] = _sha(before)
        after = desired[name].encode() if name in desired else None
        unit = name.split(".d/", 1)[0]
        if unit.endswith(UNIT_SUFFIXES):
            for dropin in host.dropins(unit, paths.units):
                if dropin == target:
                    continue
                body = _read(dropin, paths.owner)
                observed[str(dropin)] = _sha(body)
                adjustments.extend(dropin_adjustments(dropin, body, known[name], unit))
        if before == after:
            continue
        action = "remove" if after is None else "new" if before is None else "replace"
        changes.append({"path": str(target), "action": action, "sha256_before": _sha(before),
                        "sha256_after": _sha(after),
                        "diff": list(difflib.unified_diff((before or b"").decode().splitlines(),
                                                        (after or b"").decode().splitlines(), lineterm=""))})
        writes[str(target)] = after
        sensitive |= name == LISTENER or (name.endswith(".slice") and before is not None
                                           and after is not None and _protection(before) != _protection(after))
    documents = {paths.state / "ponds" / name / "config.toml": document for name, document in ponds.items()}
    # Every saved prefix is a valid site. Stop intent precedes parking the initial
    # Pond; initial enablement precedes running intent. No transaction journal.
    if candidate.state == "running":
        documents[paths.config] = candidate.document
    else:
        documents = {paths.config: candidate.document, **documents}
    pending = {path: tomllib.loads(raw.decode()) for path, raw in site.sources.items()}
    intent = {}
    for target, document in documents.items():
        before = site.sources[target]
        if pending[target] != document:
            after = toml_document(document).encode("utf-8")
            pending[target] = tomllib.loads(after.decode("utf-8"))
            SiteDescription.parse(pending[paths.config],
                                  {name: pending[paths.state / "ponds" / name / "config.toml"] for name in ponds})
            intent[str(target)] = after
            changes.append({"path": str(target), "action": "replace",
                            "sha256_before": _sha(before), "sha256_after": _sha(after),
                            "diff": list(difflib.unified_diff(before.decode().splitlines(),
                                                            after.decode().splitlines(), lineterm=""))})
    writes = {**intent, **writes}
    enabled = [n for n,p in candidate.ponds.items() if p.enabled]
    workers = [f"boring-crocodile-worker-{n}.service" for n in enabled]
    sockets = [f"boring-crocodile-task-{n}.socket" for n in enabled]
    slices = [budget_slice(n) for n in enabled]
    retired = [n for n,p in candidate.ponds.items() if not p.enabled]
    stop_ponds = list(candidate.ponds) if sensitive or candidate.state != "running" else retired
    stop = ([LISTENER, TARGET, CONTROLLER] if sensitive or candidate.state != "running" else [])
    stop += [f"boring-crocodile-worker-{n}.service" for n in stop_ponds]
    stop += [f"boring-crocodile-task-{n}.socket" for n in stop_ponds]
    stop += [Path(change["path"]).name for change in changes
             if change["path"].endswith(".socket") and change["action"] != "new"
             and Path(change["path"]).name != LISTENER and Path(change["path"]).name not in stop]
    tasks = [f"boring-crocodile-task-{n}@*.service" for n in stop_ponds]
    wanted = {TARGET, *sockets} if candidate.state == "running" else set()
    disable = sorted(n for n in known if n.endswith((".socket", ".target"))
                     and n not in wanted and paths.location(n).exists())
    tail = [["/usr/bin/systemctl", "daemon-reload"]]
    if candidate.state == "running":
        tail += [["/usr/bin/systemctl", "enable", "--no-reload", TARGET, *sockets],
                 ["/usr/bin/systemctl", "start", *slices, *sockets],
                 ["/usr/bin/systemctl", "restart", *workers],
                 ["/usr/bin/systemctl", "start", LISTENER, TARGET],
                 ["/usr/bin/systemctl", "restart", CONTROLLER]]
    proposal = {"kind": "site_render", "state": candidate.state,
                "listen": {"host": candidate.document["server"]["bind_host"],
                           "port": candidate.document["server"]["bind_port"]},
                "sources": site.fingerprints(), "observed": observed, "adjustments": adjustments,
                "changes": changes, "stop": stop, "tasks": tasks, "disable": disable, "tail": tail,
                "status": "proposed", "listener_transition": sensitive, "groups": host.group_plan(candidate),
                "permissions": permissions}
    proposal["plan_digest"] = plan_digest(proposal)
    return proposal, (writes, desired, workers, sockets, slices)


def apply(approved_digest, *, paths=None, host=None, candidate=None):
    paths, host = paths or Paths(), host or Host()
    host.require_authority()
    with site_lock(paths.state, owner=paths.owner):
        proposal, (writes, desired, workers, sockets, slices) = plan(
            paths=paths, host=host, candidate=candidate)
        if proposal["plan_digest"] != approved_digest:
            raise SiteConflict("Host or site changed since the reviewed plan")
        return _apply_locked(proposal, (writes, desired, workers, sockets, slices), paths, host)


def _apply_locked(proposal, prepared, paths, host):
    """The single effect owner; caller holds the site lock and checked its fresh plan."""
    writes, desired, workers, sockets, slices = prepared
    with tempfile.TemporaryDirectory(prefix="crocodile-render-") as folder:
        units = []
        for name, body in desired.items():
            if name.endswith(UNIT_SUFFIXES):
                target = Path(folder) / name
                target.write_text(body)
                units.append(str(target))
        if units:
            host.command(["/usr/bin/systemd-analyze", "verify", *units])
    if proposal["stop"]:
        host.stop(proposal["stop"], proposal["tasks"])
    saved, readiness = [], None
    try:
        if proposal["disable"]:
            host.command(["/usr/bin/systemctl", "disable", "--no-reload", *proposal["disable"]])
        for target, body in writes.items():
            path = Path(target)
            if body is None:
                path.unlink()
            else:
                mode = 0o600 if target in proposal["sources"] else 0o644 if not path.exists() else None
                if path.name == PIRATE_BIND_DROPIN:
                    path.parent.mkdir(mode=0o755, exist_ok=True)
                write_file(path, body, mode=mode, create=not path.exists())
                if target in proposal["sources"]:
                    saved.append(target)
        host.apply_description_permissions(proposal["permissions"])
        host.apply_groups(proposal["groups"])
        tmpfiles = [str(paths.location(n)) for n in desired if n.endswith(".conf") and "/" not in n]
        if tmpfiles:
            host.command(["/usr/bin/systemd-tmpfiles", "--create", *tmpfiles])
        for command in proposal["tail"]:
            host.command(command)
        if proposal["state"] == "running":
            host.running([LISTENER, CONTROLLER, *workers, *sockets, *slices])
            readiness = host.wait_ready(SiteDescription.read(paths.config, owner=paths.owner))
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise SiteConflict("Render did not finish; data remain. Saved desired files: "
                           + json.dumps(saved) + ". Rerun --site-render --apply to repair. "
                           + str(error)) from error
    return {"status": "rendered", "state": proposal["state"], "files": list(writes),
            "listener_transition": proposal["listener_transition"], "local_readiness": readiness,
            "external_registration": "not_checked; Pirates and services reconnect asynchronously"}


def review(proposal):
    lines = ["Render desired site state: " + proposal["state"]]
    if not ipaddress.ip_address(proposal["listen"]["host"]).is_loopback:
        lines.append("REVIEW: this non-loopback listener exposes plaintext HTTP beyond this host. "
                     "Restrict access to your trusted reverse proxy with the host firewall.")
    for change in proposal["changes"]:
        lines.append(change["action"] + " " + change["path"])
        lines.extend(change.get("diff", []))
    for row in proposal.get("groups", []):
        if row["add"] or row["remove"]:
            lines.append(f"Group {row['group']}: add {row['add']}; remove {row['remove']}")
    lines.append("Description access: restore main config to controller/all Ponds; each Pond file to its worker/controller only.")
    lines.append("Stop: " + ", ".join(proposal["stop"] + proposal["tasks"]))
    lines.append("Disable boot activation: " + ", ".join(proposal["disable"]))
    lines.append("Always run: " + "; ".join(" ".join(c) for c in proposal["tail"]))
    lines.append("Running jobs in restarted workers are interrupted.")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="boring-crocodile-setup --site-render", description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise SiteConflict("Rendering the site requires root")
        proposal, _ = plan()
        print(review(proposal), file=sys.stderr)
        if not args.apply:
            print(json.dumps(proposal, indent=2))
            return 0
        digest = confirm_apply(proposal, "render this desired site and restart its workers")
        outcome = apply(digest)
        print(json.dumps(outcome, indent=2))
        return 0
    except (ValueError, KeyError, OSError, EOFError, subprocess.SubprocessError) as error:
        print("Site render failed: " + str(error), file=sys.stderr)
        return 2
