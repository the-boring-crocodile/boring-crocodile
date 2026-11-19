"""One-release legacy conversion; preserve users, secrets and DB, provision reception."""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict, replace
import difflib
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tomllib

from .approval import confirm_apply, plan_digest
from .settings import EnvironmentSettings, PondSettings, Settings
from boring_worker_kit.validation import table
from .site_host import Host, SiteConflict
from .site_layout import CONFIG_FILE, CONTROL_CREDENTIALS, REGISTRATION_GROUP, approval_credential, site_lock, write_file
from .site_model import Identity, SiteDescription, toml_document
from .site_policy import parse_size
from .site_render import Paths, _read, dropin_adjustments
from .site_templates import render_description
from .task_policy import task_directives

POLICY_DROPIN = "50-pond-task-policy.conf"


def legacy_settings(data):
    """Parse the published 0.4.21 mapping only; normal readers use SiteDescription."""
    table(data, "legacy config", {"server", "auth", "environments"}, {"ponds", "pirate_services"})
    environments = table(data["environments"], "environments")
    ponds = table(data.get("ponds", {}), "ponds")
    site = {key: deepcopy(data[key]) for key in ("server", "auth", "pirate_services") if key in data}
    pirates = table(site.get("pirate_services", {}), "pirate_services", set(), {"callers", "socket"})
    for name, uid in table(pirates.get("callers", {}), "pirate_services.callers").copy().items():
        if type(uid) is not int:
            raise SiteConflict("Published Pirate service caller must be a UID: " + name)
        site["pirate_services"]["callers"][name] = {"uid": uid, "ponds": []}
    return Settings.from_site(
        site,
        environments={name: EnvironmentSettings.from_mapping(name, value)
                      for name, value in environments.items()},
        ponds={name: PondSettings.from_mapping(value) for name, value in ponds.items()})


def _policy_mode(path, body, home, units, name):
    if path != units / ("boring-crocodile-task-"+name+"@.service.d") / POLICY_DROPIN:
        raise SiteConflict("Old task-policy drop-in outside its generated location: " + str(path))
    for mode in (True, False):
        if body == ("[Service]\n" + task_directives(mode, str(home)) + "\n").encode():
            return mode
    raise SiteConflict("Old task-policy drop-in needs review: " + str(path))


def _consumers(names):
    return ["boring-crocodile-mcp.target", "boring-crocodile-mcp.socket", "boring-crocodile-mcp.service"] + [
        "boring-crocodile-"+kind+"-"+name+suffix for name in names
        for kind,suffix in (("worker", ".service"), ("task", ".socket"))]



def _fields(body):
    section, result = "", {}
    for line in body.decode().splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.endswith("\\"):
            raise SiteConflict("Continued unit directives require manual review before adoption")
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, sep, value = line.partition("=")
        if not sep:
            raise SiteConflict("Unrecognized legacy unit syntax")
        result.setdefault((section, key.strip()), []).append(value.strip())
    return result


def _one(fields, section, key):
    values = fields.get((section, key), [])
    if len(values) != 1 or not values[0]:
        raise SiteConflict("Expected one explicit legacy unit directive: " + section + "." + key)
    return values[0]


def _identity(host, name, group):
    user, primary = host.account(name), host.group(group)
    if not user or not primary or group != name or user["gid"] != primary["gid"]:
        raise SiteConflict("Legacy account/private group binding needs review: " + name)
    identity = Identity.parse({**user, "group": group})
    if identity.shell != "/usr/sbin/nologin":
        raise SiteConflict("Legacy dedicated account shell needs review: " + name)
    return identity


def plan(*, paths=None, host=None):
    paths, host = paths or Paths(), host or Host()
    host.protected(paths.state)
    observed = {}

    def read(path):
        host.protected(path.parent)
        body = _read(path, paths.owner)
        observed[str(path)] = hashlib.sha256(body).hexdigest() if body is not None else None
        return body

    original = read(paths.config)
    if original is None:
        raise SiteConflict("No existing site to adopt; use ordinary provisioning")
    raw = tomllib.loads(original.decode())
    if "site" in raw:
        site = SiteDescription.read(paths.config, owner=paths.owner)
        removals = []
        for name, pond in site.ponds.items():
            for path in host.dropins("boring-crocodile-task-"+name+"@.service", paths.units):
                if path.name == POLICY_DROPIN:
                    if _policy_mode(path, read(path), pond.identity.home, paths.units, name) != pond.execution.no_new_privileges:
                        raise SiteConflict("Old task-policy drop-in differs from converted description: " + str(path))
                    removals.append(str(path))
        result = dict(kind="site_adoption", status="already_converted",
                      sources={**site.fingerprints(), **observed}, removals=removals,
                      stop_before_apply=_consumers(site.ponds),
                      documents=[], permissions=host.description_permissions(site, paths.config),
                      note="Use the common render to repair this declared site")
        result["plan_digest"] = plan_digest(result)
        return result, (site, {})

    legacy = deepcopy(raw)
    reception = []
    for name, execution in legacy.get("environments", {}).items():
        enabled = execution.pop("service_registration_enabled", False)
        if type(enabled) is not bool:
            raise SiteConflict("Legacy service_registration_enabled must be boolean: " + name)
        if not enabled:
            reception.append(name)
    old = legacy_settings(legacy)
    if "pond" not in old.ponds or {p.environment for p in old.ponds.values()} != set(old.environments):
        raise SiteConflict("Adoption requires the standard initial Pond and one worker per Pond")

    def unit(name):
        body = read(paths.units / name)
        if body is None:
            raise SiteConflict("Missing standard legacy unit: " + name)
        return _fields(body)

    control = unit("boring-crocodile-mcp.service")
    controller = _identity(host, _one(control, "Service", "User"), _one(control, "Service", "Group"))
    command = shlex.split(_one(control, "Service", "ExecStart"))
    if len(command) != 1 or not command[0].endswith("/bin/boring-crocodile-mcp"):
        raise SiteConflict("Nonstandard controller command needs review")
    runtime = str(Path(command[0]).parent.parent)
    credentials = {}
    for value in control.get(("Service", "LoadCredential"), []):
        key, sep, source = value.partition(":")
        if not sep or key in credentials:
            raise SiteConflict("Ambiguous legacy credential binding")
        credentials[key] = source

    ponds, metadata, removals = {}, {}, []
    environments = dict(old.environments)
    from .site_layout import budget_slice
    for name, profile in old.ponds.items():
        if profile.environment != name:
            raise SiteConflict("Nonstandard Pond/environment mapping needs review")
        env = old.environments[name]
        worker = unit("boring-crocodile-worker-"+name+".service")
        task = unit("boring-crocodile-task-"+name+"@.service")
        identity = _identity(host, _one(worker, "Service", "User"), _one(worker, "Service", "Group"))
        if env.control_plane_uid != controller.uid:
            raise SiteConflict("Legacy controller UID differs from NSS")
        if (_one(task, "Service", "User"), _one(task, "Service", "Group")) != (identity.name, identity.group):
            raise SiteConflict("Task and worker identity bindings differ")
        for fields, program in ((worker, "worker"), (task, "task")):
            expected = [runtime+"/bin/boring-crocodile-"+program, "--config", str(CONFIG_FILE), "--environment", name]
            if shlex.split(_one(fields, "Service", "ExecStart")) != expected:
                raise SiteConflict("Nonstandard worker/task runtime or configuration path")
        budget = unit(budget_slice(name))
        limits = dict(memory_bytes=parse_size(_one(budget, "Slice", "MemoryMax")),
                      swap_bytes=parse_size(_one(budget, "Slice", "MemorySwapMax")),
                      tasks_max=int(_one(budget, "Slice", "TasksMax")),
                      task_tasks_max=int(_one(task, "Service", "TasksMax")))
        credential = approval_credential(name)
        if profile.approval_secret_file != CONTROL_CREDENTIALS / credential or credential not in credentials:
            raise SiteConflict("Legacy approval credential mapping needs review")
        source = host.path(credentials[credential])
        info = source.lstat()
        metadata[str(source)] = [info.st_dev, info.st_ino, info.st_uid, info.st_gid,
                                 info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns]
        execution = deepcopy(legacy["environments"][name])
        if "display_name" in execution and execution["display_name"] != profile.display_name:
            raise SiteConflict("Conflicting legacy display names: environments." + name + ".display_name")
        execution.pop("display_name", None)
        env = replace(env, display_name=profile.display_name)
        environments[name] = env
        for path in host.dropins("boring-crocodile-task-"+name+"@.service", paths.units):
            if path.name == POLICY_DROPIN:
                mode = _policy_mode(path, read(path), identity.home, paths.units, name)
                execution["no_new_privileges"] = mode
                environments[name] = replace(env, no_new_privileges=mode)
                removals.append(str(path))
        for key in ("socket_path", "task_socket_path", "task_unit_prefix", "task_cgroup_parent", "control_plane_uid"):
            execution.pop(key, None)
        document = deepcopy(raw["ponds"][name])
        document.pop("environment")
        document.pop("approval_secret_file")
        if document["context_root"] == identity.home + "/handbook":
            document.pop("context_root")
        for key, default in (("default_cwd", identity.home), ("allowed_roots", [identity.home])):
            if execution.get(key) == default:
                execution.pop(key)
        home_root = execution.get("files", {}).get("roots", {}).get("home")
        if home_root is not None:
            if home_root["path"] != identity.home:
                raise SiteConflict("Legacy home file-root alias differs from identity.home: " + name)
            home_root.pop("path")
        ponds[name] = dict(enabled=True, identity=asdict(identity), credential_file=credentials[credential],
            limits=limits, pond=document, execution=execution)
    if set(credentials) != {approval_credential(name) for name in ponds}:
        raise SiteConflict("Unaccounted controller credentials need review")
    main = {key: deepcopy(raw[key]) for key in ("server", "auth", "pirate_services") if key in raw}
    if "pirate_services" in main:
        # A shared Unix client group proves no per-Pirate pairing. Preserve
        # authority identities, but require explicit admission after adoption.
        main["pirate_services"]["callers"] = {
            name: {"uid": caller.uid, "ponds": []} for name, caller in old.pirate_callers.items()}
    main["site"] = dict(schema=1, state="stopped", runtime_root=runtime, controller=asdict(controller))
    candidate = SiteDescription.parse(main, ponds)
    if candidate.runtime_settings() != replace(old, environments=environments):
        raise SiteConflict("Standard typed projection would change existing runtime settings; review instead")
    # Published sites with reception disabled never provisioned this shared group.
    # Plan its creation without changing NSS during dry-run; render owns membership.
    provision_groups = [] if host.group(REGISTRATION_GROUP) else [REGISTRATION_GROUP]
    host.prerequisites(candidate, registration_required=False)
    groups = host.group_plan(candidate)  # Validate the same admission bindings as render; never apply them here.
    store_path = host.path(old.oauth_database_file)
    fd = os.open(store_path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as store:
        info = os.fstat(store.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != host.database_owner(candidate) or info.st_mode & 0o077):
            raise SiteConflict("Unsafe OAuth store metadata")
        header = store.read(100)  # No SQLite connection: even a reader may create WAL sidecars.
        if header[:16] != b"SQLite format 3\x00" or int.from_bytes(header[60:64], "big") != 5:
            raise SiteConflict("OAuth database header is not generation 5; checkpoint/review its consistent backup")
        store_info = dict(path=str(store_path), generation=5,
                          metadata=[info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns])
    expected = render_description(candidate)
    adjustments, render_changes = [], []
    for name, body in expected.items():
        before = read(paths.location(name))
        if before != body.encode():
            render_changes.append(dict(path=str(paths.location(name)), diff=list(difflib.unified_diff(
                (before or b"").decode().splitlines(), body.splitlines(), lineterm=""))))
        if not name.endswith((".service", ".socket", ".slice", ".target")):
            continue
        for path in host.dropins(name, paths.units):
            body = read(path)
            if path.name == POLICY_DROPIN:
                if str(path) not in removals:
                    raise SiteConflict("Task-policy drop-in needs review: " + str(path))
                continue
            adjustments.extend(dropin_adjustments(path, body, expected[name], name))
    for directory, patterns in ((paths.units, ("boring-crocodile-*", "system-crocodile*.slice")),
                                 (paths.tmpfiles, ("boring-crocodile*.conf",))):
        for pattern in patterns:
            for path in directory.glob(pattern):
                if path.suffix in {".service", ".socket", ".slice", ".target", ".conf"} and path.name not in expected:
                    raise SiteConflict("Undeclared legacy artifact needs review: " + str(path))
    namespace = paths.state / "ponds"
    if namespace.exists():
        host.protected(namespace)
        for folder in namespace.iterdir():
            if folder.name not in ponds:
                raise SiteConflict("Undeclared/parked legacy Pond needs review: " + folder.name)
    documents = {paths.state / "ponds" / name / "config.toml": toml_document(p.document).encode()
                 for name,p in candidate.ponds.items()}
    documents[paths.config] = toml_document(candidate.document).encode()  # publish main last
    changes, writes = [], {}
    for path, body in documents.items():
        before = read(path)
        if path != paths.config and before is not None and before != body:
            raise SiteConflict("Existing per-Pond document differs from this conversion: " + str(path))
        if before != body:
            changes.append(dict(path=str(path), content=body.decode()))
            writes[str(path)] = body
    units = _consumers(ponds)
    result = dict(kind="site_adoption", status="proposed", state="stopped", sources=observed,
                  identities=[asdict(controller), *[asdict(p.identity) for p in candidate.ponds.values()]],
                  credentials=metadata, store=store_info, adjustments=adjustments, groups=groups, removals=removals,
                  reception_enabled=sorted(reception), provision_groups=provision_groups,
                  pirate_admission="No per-Pirate Pond pairs inferred from the shared Unix group; "
                                   "admit each Pirate explicitly with pirate-add --ponds after adoption",
                  permissions=host.description_permissions(candidate, paths.config), documents=changes, render_changes=render_changes,
                  stop_before_apply=units, snapshot="Take the reviewed private config snapshot and consistent OAuth backup before conversion")
    result["plan_digest"] = plan_digest(result)
    return result, (candidate, writes)


def apply(approved_digest, *, paths=None, host=None):
    paths, host = paths or Paths(), host or Host()
    host.require_authority()
    with site_lock(paths.state, owner=paths.owner):
        proposal, (site, writes) = plan(paths=paths, host=host)
        if proposal["plan_digest"] != approved_digest:
            raise SiteConflict("Adoption inputs changed since review")
        if proposal["status"] == "already_converted" and not proposal["removals"]:
            host.apply_description_permissions(proposal["permissions"])
            return proposal
        host.inactive(proposal["stop_before_apply"])
        if host.task_instances(["boring-crocodile-*.service"]):
            raise SiteConflict("Crocodile consumers/tasks still run; stop and verify before adoption")
        saved = []
        try:
            for name in proposal.get("provision_groups", []):
                host.create_group(name)
            for target, body in writes.items():
                path = Path(target)
                for directory in (paths.state / "ponds", path.parent):
                    host.protected(directory.parent)
                    if not directory.exists():
                        directory.mkdir(mode=0o755)
                    host.protected(directory)
                write_file(path, body, mode=0o600, create=not path.exists())
                saved.append(target)
            host.apply_description_permissions(proposal["permissions"])
            for target in proposal["removals"]:
                Path(target).unlink()
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise SiteConflict("Adoption interrupted; saved "+json.dumps(saved)+
                               ". Re-read the current description and rerun adoption; already converted output is kept. "
                               "Or review the private snapshot. "+str(error)) from error
        return dict(status="converted", state=site.state, files=saved, next="Deploy the candidate while stopped, then review and APPLY render")


def main(argv=None, *, artifact_root=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="read-only plan and native account/hook proof in a private temporary copy (default)")
    mode.add_argument("--apply", action="store_true", help="convert an already stopped site after typed APPLY")
    args = parser.parse_args(argv)
    try:
        proposal, (site, _) = plan()
        if args.apply:
            proposal = apply(confirm_apply(proposal, "convert this stopped legacy site; keep accounts, secrets and data"))
        else:
            from .site_adopt_proof import prove
            proposal["native_proof"] = prove(site, artifact_root)
        print(json.dumps(proposal, indent=2))
        return 0 if proposal.get("native_proof", {}).get("status", "ok") == "ok" else 2
    except (ValueError, KeyError, OSError, EOFError, subprocess.SubprocessError) as error:
        print("Site adoption refused: "+str(error), file=sys.stderr)
        return 2
