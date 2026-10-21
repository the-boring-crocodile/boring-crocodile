"""Review and activate a new configured site from the existing setup receipts.

The runtime owns the workflow. Package adapters only provide its entry point.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import grp
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import time
from .pirates import REGISTRATION_GROUP
from .setup import SetupInputs, plan_digest, unique_object
from .setup_apply import Host, read_receipt, site_receipts, validate_site, write_receipt
from .setup_services import ServiceInputs, render_site
from .settings import Settings
from .site_layout import CREDENTIALS_DIR, STATE_DIR, approval_credential, site_lock, write_file

STATE = STATE_DIR
UNITS = Path("/etc/systemd/system")
TMPFILES = Path("/etc/tmpfiles.d")
SITE_RECEIPT = STATE / "site-activation.json"
# `uninstall --site` without --delete-data marks the activation receipt so and
# records under "kept" the Ponds that were live, their ceilings and the Pirate
# callers. The accounts, homes, credentials, grants and setup receipts stay;
# activation installs the site again from them: the live Ponds with their
# ceilings (the rest from this run's policy), the callers in config.toml, and
# a parked Pond stays parked.
UNINSTALLED = "uninstalled"


class SiteConflict(ValueError):
    pass


def defaults():
    # Conservative, visible host-relative proposal; all ceilings remain editable.
    ram = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    memory = max(512 * 1024**2, min(8 * 1024**3, ram // 4))
    return dict(bind_host="127.0.0.1", bind_port=8503,
                memory_bytes=memory, swap_bytes=0, tasks_max=256,
                task_tasks_max=128, runtime_root=runtime_root())


def runtime_root(executable: str = sys.executable) -> str:
    """The units' runtime: HOME/current for a deployed release (whose Python is
    HOME/releases/<version>/app/bin/python3), so a deploy never rewrites units;
    otherwise the running environment's root."""
    path = Path(executable)
    parts = path.parts
    if "releases" in parts and parts[-3:-1] == ("app", "bin"):
        home = Path(*parts[:parts.index("releases")])
        return str(home / "current")
    return str(path.parent.parent)


def _location(name):
    if name == "config.toml":
        return STATE / name
    if name.endswith(".conf"):
        return TMPFILES / name
    return UNITS / name


def _fingerprint(payload):
    return hashlib.sha256(payload).hexdigest()


def _candidate(policy, kept=None):
    """The Ponds to activate and their files. `kept`: what uninstall --site
    recorded (live Ponds, ceilings, Pirate callers), or None for every
    recorded Pond with this policy."""
    if policy.service_registration_enabled:
        try:
            grp.getgrnam(REGISTRATION_GROUP)
        except KeyError as error:
            raise SiteConflict(f"Create {REGISTRATION_GROUP} and review guest account membership before activation") from error
    host = Host()
    known = site_receipts(host)
    validate_site(host, known, None)  # every Pond, parked ones too: the controller is in all their groups
    ceilings: dict = {}
    if kept is not None:
        known = {name: item for name, item in known.items() if name in kept["live_ponds"]}
        ceilings = kept["ceilings"]
    entries = [(SetupInputs.parse(item["inputs"]),
                ServiceInputs.parse(vars(policy) | ceilings[name]) if name in ceilings else policy, item)
               for name, item in known.items()]
    rendered = render_site(entries)
    callers = (kept or {}).get("pirate_callers") or {}
    if callers:  # as pirate-add wrote them
        rendered["config.toml"] = (rendered["config.toml"].rstrip("\n") + "\n\n[pirate_services.callers]\n"
                                   + "".join(f"{name} = {uid}\n" for name, uid in sorted(callers.items())))
    _check_runtime(policy)
    return known, rendered


def _check_runtime(policy):
    if not policy.runtime_root.startswith("/opt/"):
        raise SiteConflict("Runtime must use the supported /opt installation layout")
    for binary in ("boring-crocodile-mcp", "boring-crocodile-worker",
                   "boring-crocodile-task"):
        path = Path(policy.runtime_root) / "bin" / binary
        if not path.is_file() or not os.access(path, os.X_OK):
            raise SiteConflict("Runtime entry point missing: " + str(path))


def _safe_existing(path, content, *, owner=0):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner
            or info.st_mode & 0o022 or info.st_nlink != 1
            or path.read_bytes() != content.encode()):
        raise SiteConflict("Existing site file differs or is unsafe: " + str(path))


def _state():
    if not os.path.lexists(SITE_RECEIPT):
        return None
    return read_receipt(SITE_RECEIPT, 0)


def plan(policy):
    """Pure planning: derive one proposal from protected setup and host files."""
    if os.path.lexists(STATE / "config.toml") and _state() is None:
        # Reality first: config.toml means a site runs here, whatever records say.
        raise SiteConflict("A site already runs here (config.toml exists); activation is for a fresh "
                           "host. Check it: boring-crocodile-admin site-check. Add a Pond: "
                           "boring-crocodile-setup --add-pond NAME, then --site-extend")
    proposal, rendered, existing = proposal_for(policy)
    if (existing is not None and existing["status"] != UNINSTALLED
            and existing["plan_digest"] != proposal["plan_digest"]):
        if existing["status"] == "complete":
            raise SiteConflict("Recorded site changed; guided activation only covers a fresh site. "
                               "To add a recorded Pond to this site: boring-crocodile-setup --site-extend")
        raise SiteConflict("Recorded activation plan differs; inspect partial site")
    return proposal, rendered


def proposal_for(policy, *, files_may_differ=False):
    """The site proposal for the recorded Ponds and this policy, without
    comparing it with the recorded activation (site_extend does that)."""
    existing = _state()
    kept = existing is not None and existing["status"] == UNINSTALLED
    known, rendered = _candidate(policy, existing.get("kept") if kept else None)
    host = Host()
    for parent in (STATE, UNITS, TMPFILES):
        host.protected(parent)
        if not parent.is_dir():
            raise SiteConflict("Missing protected site directory: " + str(parent))
    outputs = {}
    for name, body in rendered.items():
        target = _location(name)
        if (not os.path.lexists(target) and existing and existing["status"] == "complete"
                and not files_may_differ):
            raise SiteConflict("Recorded site file disappeared: " + str(target))
        if os.path.lexists(target) and not files_may_differ:
            if existing is None:
                raise SiteConflict("Unrecorded site file; review before adoption: " + str(target))
            _safe_existing(target, body)
        outputs[str(target)] = _fingerprint(body.encode())
    credentials = {approval_credential(name) for name in known}
    credential_dir = STATE / CREDENTIALS_DIR.name  # follows STATE, which tests redirect
    if os.path.lexists(credential_dir):
        host.protected(credential_dir)
        if existing is None:
            raise SiteConflict("Unrecorded credential directory; review before adoption")
        if stat.S_IMODE(credential_dir.stat().st_mode) != 0o700:
            raise SiteConflict("Credential directory must be private (0700)")
        for name in credentials:
            path = credential_dir / name
            if not os.path.lexists(path) and existing and (
                    existing["status"] == "prepared" or kept or files_may_differ):
                continue  # created by apply (or by site_extend for a new Pond)
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != host.owner
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise SiteConflict("Missing or unsafe credential: " + name)
    elif existing is not None and existing["status"] != "prepared" and not kept:
        raise SiteConflict("Recorded credentials disappeared")
    proposal = {
        "schema_version": 1,
        "policy": vars(policy),
        "ponds": {name: item["profile_id"] for name, item in known.items()},
        "account_receipts": {name: _fingerprint(json.dumps(
            item, sort_keys=True).encode()) for name, item in known.items()},
        "generated_files": outputs,
        "credential_names": sorted(credentials),
        "status": "proposed" if existing is None or kept
                  else "already_configured" if existing["status"] == "complete" else "resume_required",
        "pending": ["Yours afterwards: DNS, TLS and the reverse proxy; then sign in a client"],
    }
    # Progress is excluded from the approval digest; resume approves the same
    # policy, setup receipts and generated bytes as the initial proposal.
    proposal["plan_digest"] = plan_digest(proposal | {"status": "proposed"})
    return proposal, rendered, existing


def _bytes(value):
    for unit, size in (("G", 1024**3), ("M", 1024**2)):
        if value % size == 0:
            return f"{value // size}{unit}"
    return f"{value // 1024**2}M"  # rounded down: never more than there is


def _plural(count, word):
    return f"{count} {word}" + ("" if count == 1 else "s")


def review_summary(proposal):
    policy = proposal["policy"]
    ponds = sorted(proposal["ponds"])
    return (f"The site: {_plural(len(ponds), 'Pond')} ({', '.join(ponds)}), each with at most "
            f"{_bytes(policy['memory_bytes'])} of memory.\n"
            f"The Crocodile listens on {policy['bind_host']}:{policy['bind_port']}; "
            f"people reach it at {policy['public_base_url']}.\n"
            f"Setup writes {_plural(len(proposal['generated_files']), 'file')} (config.toml, systemd units) "
            f"and {_plural(len(proposal['credential_names']), 'approval secret')}, then starts the site.\n"
            "DNS, TLS and your reverse proxy stay yours to set up.")


def _bind(host, port):
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as listener:
        listener.bind((host, port))


def _check_bind(policy):
    # Only a fresh site needs an unused listener. Binding does not expose it.
    if os.path.lexists(STATE / "config.toml"):
        return
    _bind(policy.bind_host, policy.bind_port)


def provision_credentials(names):
    """Create each missing approval credential; an existing one is never replaced."""
    credentials = STATE / CREDENTIALS_DIR.name
    if not credentials.exists():
        credentials.mkdir(mode=0o700)
    Host().protected(credentials)
    for name in names:
        secret_path = credentials / name
        if secret_path.exists() and not secret_path.is_symlink() and secret_path.read_text().strip():
            continue
        # Written whole or not at all: a crash cannot leave an empty secret
        # that a resumed setup would keep (and that refuses every approval).
        write_file(secret_path, secrets.token_urlsafe(32) + "\n", mode=0o600)


def _local_state(policy):
    from .admin import status_report
    report = status_report(Settings.load(STATE / "config.toml"))
    return report["status"]


def _settled_local_state(policy, *, window=60.0):
    """Allow asynchronous service startup an observation window (a slow host
    needs well over ten seconds to start the controller; the deploy's smoke
    waits a minute too).

    This is a polling window, not an overall deadline for the status probes.
    Each systemctl probe has its own timeout. Persistent failures stay visible.
    """
    deadline = time.monotonic() + window
    while True:
        health = _local_state(policy)
        remaining = deadline - time.monotonic()
        if health == "healthy" or remaining <= 0:
            return health
        time.sleep(min(1.0, remaining))


def apply(policy, approved_digest, *, resume=False, runner=subprocess.run):
    if os.geteuid() != 0:
        raise SiteConflict("Site activation requires root")
    proposal, rendered = plan(policy)
    if proposal["plan_digest"] != approved_digest:
        raise SiteConflict("Host or policy changed since the reviewed plan")
    if proposal["status"] == "already_configured":
        return {"status": "already_configured", "local_health": _local_state(policy)}
    if proposal["status"] == "resume_required" and not resume:
        raise SiteConflict("Interrupted activation; inspect status and use --resume")
    if resume and proposal["status"] != "resume_required":
        raise SiteConflict("No interrupted activation to resume")
    with site_lock(STATE):
        proposal, rendered = plan(policy)
        if proposal["plan_digest"] != approved_digest:
            raise SiteConflict("Host changed during site activation")
        _check_bind(policy)
        # Verify the exact candidate before writing site integration.
        with tempfile.TemporaryDirectory(prefix="crocodile-site-", dir="/run") as directory:
            paths = []
            for name, body in rendered.items():
                output = Path(directory) / name
                output.write_text(body)
                if name.endswith((".service", ".socket", ".slice", ".target")):
                    paths.append(str(output))
            Settings.load(Path(directory) / "config.toml")
            runner(["/usr/bin/systemd-analyze", "verify", *paths], check=True,
                   capture_output=True, text=True)
        if not os.path.lexists(SITE_RECEIPT) or _state()["status"] == UNINSTALLED:
            write_receipt(SITE_RECEIPT, {
                "schema_version": 1, "status": "prepared",
                "plan_digest": approved_digest, "policy": vars(policy),
                "generated_files": proposal["generated_files"],
                "credential_names": proposal["credential_names"],
            })
        provision_credentials(proposal["credential_names"])
        for name, body in rendered.items():
            if name == "boring-crocodile-mcp.target" and (Path("/lib/systemd/system") / name).exists():
                continue
            target = _location(name)
            if not os.path.lexists(target):  # written whole: a resume finds it whole or absent
                write_file(target, body, mode=0o644, create=True)
        for name in rendered:
            if name.endswith(".conf"):
                runner(["/usr/bin/systemd-tmpfiles", "--create", str(_location(name))],
                       check=True)
        runner(["/usr/bin/systemctl", "daemon-reload"], check=True)
        # WantedBy is an installation rule, not a dependency until enabled.
        # Install every generated task socket's target.wants link before
        # starting the site target (including sites with multiple Ponds).
        # Start sockets explicitly too: resume may find an already-active target.
        sockets = sorted(name for name in rendered if name.endswith(".socket"))
        if sockets:
            runner(["/usr/bin/systemctl", "enable", "--now", *sockets], check=True)
        runner(["/usr/bin/systemctl", "enable", "--now", "boring-crocodile-mcp.target"], check=True)
        receipt = _state()
        receipt["status"] = "complete"
        write_receipt(SITE_RECEIPT, receipt)
    health = _settled_local_state(policy)
    return {"status": "configured" if health == "healthy" else "configured_but_degraded",
            "local_health": health,
            "next": ("Configure public HTTPS (the paths: INSTALL, The reverse proxy), then connect a client to /mcp/POND."
                     if health == "healthy" else
                     "Inspect boring-crocodile-admin status --check and the relevant systemd units.")}


def wizard(ask=input, tell=None):
    """Each answer is checked as it is given, and asked again with the reason."""
    from .site_check import _size
    tell = tell or (lambda text: print(text, file=sys.stderr))
    options = defaults()
    # Everything not asked must already hold, so a refused answer is always
    # about the question just asked (a runtime outside /opt stops here once).
    ServiceInputs.parse(options | {"public_base_url": "https://example.org"})

    def field(label, key, default, convert=str):
        while True:
            answer = ask(label + (f" [{default}]" if default != "" else "") + ": ").strip() or str(default)
            try:
                if not answer:
                    raise ValueError("An answer is needed")
                value = convert(answer)
                ServiceInputs.parse(options | {key: value})
                options[key] = value
                return
            except (ValueError, KeyError) as error:
                tell(f"  {error or 'Not a valid answer'}. Again, please.")

    field("Public HTTPS address people reach the Crocodile at (example: https://mcp.example.org)",
          "public_base_url", "")
    def port(text):
        if not text.isdigit():
            raise ValueError("A port is a number between 1024 and 65535")
        return int(text)

    def size(text):
        try:
            return _size(text.upper())
        except (ValueError, KeyError):
            raise ValueError("A size like 2G or 1536M") from None

    def local_address(text):
        ipaddress.ip_address(text)  # an address, never a name to look up
        try:
            _bind(text, 0)
        except OSError:
            raise ValueError(f"This host has no address {text}; keep 127.0.0.1 unless your "
                             "proxy runs on another machine") from None
        return text

    def free_port(text):
        number = port(text)
        if not os.path.lexists(STATE / "config.toml"):  # a running site has its listener
            try:
                _bind(options["bind_host"], number)
            except OSError:
                raise ValueError(f"Port {number} is taken on {options['bind_host']}; "
                                 "choose another") from None
        return number

    ram = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")

    def ceiling(text):
        value = size(text)
        if value > ram:
            raise ValueError(f"This host has {_bytes(ram)} of memory; a ceiling above it "
                             "is no ceiling")
        return value

    field("Local address it listens on", "bind_host", options["bind_host"], local_address)
    field("Local port", "bind_port", options["bind_port"], free_port)
    tell("Memory: each Pond's commands share one ceiling. Proposed: a quarter of this "
         "host's memory, between 512M and 8G. Sizes like 2G or 1536M.")
    field("Memory ceiling per Pond", "memory_bytes", _bytes(options["memory_bytes"]), ceiling)
    return ServiceInputs.parse(options)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--wizard", action="store_true")
    mode.add_argument("--config", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approve-digest")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    if args.resume and not args.apply:
        parser.error("--resume requires --apply")
    if args.apply and args.config and not args.approve_digest:
        parser.error("Unattended apply requires --approve-digest")
    try:
        if args.wizard:
            if not sys.stdin.isatty():
                raise SiteConflict("Guided setup needs a terminal; use --site-config FILE for unattended setup")
            from .setup import terminal_ask
            policy = wizard(terminal_ask)
        else:
            policy = ServiceInputs.parse(json.loads(args.config.read_text(),
                                                   object_pairs_hook=unique_object))
        proposal, _ = plan(policy)
        if not args.apply:
            print(json.dumps(proposal, indent=2))
            return 0
        digest = args.approve_digest
        if args.wizard and not digest:
            print("\n" + review_summary(proposal) + "\n", file=sys.stderr)
            print("Type APPLY to write these files and start the site: ",
                  end="", file=sys.stderr, flush=True)
            if sys.stdin.readline().strip() != "APPLY":
                raise SiteConflict("Activation was not confirmed")
            digest = proposal["plan_digest"]
        outcome = apply(policy, digest, resume=args.resume)
        if args.wizard:
            healthy = outcome["local_health"] == "healthy"
            print(("The site is running." if healthy else
                   "The site was set up, but it is not healthy: " + outcome["next"]), file=sys.stderr)
            if healthy:
                shown = [str(CREDENTIALS_DIR / name) for name in proposal["credential_names"]]
                print("The approval secret people type when they sign in: sudo cat "
                      + " ".join(shown), file=sys.stderr)
                print(f"Next: your reverse proxy (INSTALL, The reverse proxy), then connect a client "
                      f"to {policy.public_base_url}/mcp/pond.", file=sys.stderr)
        else:
            print(json.dumps(outcome, indent=2))
        return 1 if outcome["local_health"] != "healthy" else 0
    except (ValueError, KeyError, OSError, EOFError, subprocess.SubprocessError) as error:
        print("Service setup failed: " + str(error), file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
