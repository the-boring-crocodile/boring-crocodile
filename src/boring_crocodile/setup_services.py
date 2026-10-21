"""Render service configuration from completed setup and explicit host policy.

Pure rendering has no Debian dependency; runtime_root is supplied by the site configuration.
The CLI writes a new candidate directory only. It never installs or activates.
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass
from importlib.resources import files
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import UUID

from .setup import SetupInputs, account, group, unique_object
from .settings import Settings
from .task_policy import task_directives
from .site_layout import CONTROL_CREDENTIALS, CREDENTIALS_DIR, INITIAL_POND, OAUTH_DATABASE, approval_credential, budget_slice, tmpfiles_name

@dataclass(frozen=True)
class ServiceInputs:
    public_base_url: str
    bind_host: str
    bind_port: int
    runtime_root: str
    memory_bytes: int
    swap_bytes: int
    tasks_max: int
    task_tasks_max: int
    no_new_privileges: bool = True
    service_registration_enabled: bool = False

    @classmethod
    def parse(cls, value):
        names = set(cls.__dataclass_fields__)
        if not isinstance(value, dict) or set(value) - names or names - set(value) - {"no_new_privileges", "service_registration_enabled"}:
            raise ValueError("Service inputs require: " + ", ".join(sorted(names - {"no_new_privileges", "service_registration_enabled"})))
        if type(value.get("no_new_privileges", True)) is not bool:
            raise ValueError("no_new_privileges must be boolean")
        if type(value.get("service_registration_enabled", False)) is not bool:
            raise ValueError("service_registration_enabled must be boolean")
        for key, minimum, maximum in (
            ("bind_port", 1024, 65535),
            ("memory_bytes", 64*1024*1024, 2**60), ("swap_bytes", 0, 2**60),
            ("tasks_max", 16, 1048576), ("task_tasks_max", 16, 1048576)):
            if type(value[key]) is not int or not minimum <= value[key] <= maximum:
                raise ValueError("Invalid service input: " + key)
        if value["task_tasks_max"] > value["tasks_max"]:
            raise ValueError("Per-task task limit exceeds shared limit")
        host = value["bind_host"]
        if not isinstance(host, str):
            raise ValueError("bind_host must be an IP literal")
        ipaddress.ip_address(host)
        url = value["public_base_url"]
        if not isinstance(url, str):
            raise ValueError("Public URL must be HTTPS")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise ValueError("Public URL must be an HTTPS origin without credentials/path/query")
        if not re.fullmatch(r"[A-Za-z0-9.:-]+|\[[A-Fa-f0-9:]+\](?::[0-9]+)?", parsed.netloc):
            raise ValueError("Invalid public authority")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("Invalid public port")
        runtime = value["runtime_root"]
        if not isinstance(runtime, str) or not re.fullmatch(r"/opt/[A-Za-z0-9_./-]+", runtime) or ".." in Path(runtime).parts or str(Path(runtime)) != runtime:
            raise ValueError("Runtime must be a normalized path below /opt")
        return cls(**(value | {"public_base_url":url.rstrip("/")}))


def render(setup, policy, receipt):
    name = receipt.get("pond_name", INITIAL_POND)
    if name != INITIAL_POND:
        from .setup_apply import pond_name
        pond_name(name)
    if receipt.get("status") != "complete" or receipt.get("inputs") != setup.as_input():
        raise ValueError("A matching completed setup receipt is required")
    users = receipt["users"]
    control = users[setup.control_user]
    execution = users[setup.execution_user]
    if any(type(x["uid"]) is not int or x["uid"] <= 0 for x in (control, execution)):
        raise ValueError("Both runtime identities must be non-root")
    if control["uid"] == execution["uid"]:
        raise ValueError("Runtime identities must be distinct")
    context_root = receipt.get("context_root")
    if context_root != setup.home + "/handbook":
        raise ValueError("The setup receipt names another context root than the home's handbook")
    # Validate document names through the shared input model.
    documents = [entry["filename"] for entry in receipt["documents"]]
    SetupInputs.parse(setup.as_input() | dict(context_mode="import",
                     context_source=context_root, context_files=documents))
    public = urlsplit(policy.public_base_url)
    bind = "["+policy.bind_host+"]" if ":" in policy.bind_host else policy.bind_host
    allowed = list(dict.fromkeys([public.netloc, public.hostname,
                                 public.hostname+":443" if ":" not in public.hostname else "["+public.hostname+"]:443",
                                 bind+":"+str(policy.bind_port)]))
    q = json.dumps
    # Never generate or reinterpret a profile identity while rendering service files.
    profile_id = receipt.get("profile_id")
    if profile_id is None:
        raise ValueError("Receipt needs a persistent profile ID; old single-Pond receipts are not supported")
    if str(UUID(profile_id)) != profile_id:
        raise ValueError("Receipt profile_id must be a canonical persistent UUID")
    connection = f'''[ponds.{name}]
profile_id = {q(profile_id)}
display_name = {q(setup.display_name or ("The Pond" if name == INITIAL_POND else name))}
environment = {q(name)}
approval_secret_file = {q(str(CONTROL_CREDENTIALS / approval_credential(name)))}
context_root = {q(context_root)}
files = {q(documents)}
welcome_file = "WELCOME.md"
'''
    config = f"""[server]
public_base_url = {q(policy.public_base_url)}
bind_host = {q(policy.bind_host)}
bind_port = {policy.bind_port}
allowed_hosts = {q(allowed)}
[auth]
oauth_database_file = {q(str(OAUTH_DATABASE))}
{connection}[environments.{name}]
socket_path = "/run/boring-crocodile/workers/{name}/control.sock"
task_socket_path = "/run/boring-crocodile/tasks/{name}.sock"
task_unit_prefix = "boring-crocodile-task-{name}"
task_cgroup_parent = "/system.slice/{budget_slice(name)}"
default_cwd = {q(setup.home)}
allowed_roots = [{q(setup.home)}]
control_plane_uid = {control["uid"]}
output_limit_bytes = 65536
no_new_privileges = {str(policy.no_new_privileges).lower()}
"""
    values = {
        "POND":name, "BUDGET":budget_slice(name),
        "RUNTIME":policy.runtime_root, "EXECUTION_USER":setup.execution_user,
        "CONTROL_USER":setup.control_user,
        "HOME":setup.home,
        "MEMORY":str(policy.memory_bytes), "SWAP":str(policy.swap_bytes),
        "TASKS":str(policy.tasks_max), "TASK_TASKS":str(policy.task_tasks_max),
        "TASK_POLICY":task_directives(policy.no_new_privileges, setup.home),
    }
    # Home also crosses the systemd/tmpfiles grammar, so accept only simple paths.
    if not re.fullmatch(r"/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+){1,6}", setup.home):
        raise ValueError("Service home contains unsupported unit-file characters")
    output = {"config.toml":config}
    root = files("boring_crocodile").joinpath("templates/services")
    for item in root.iterdir():
        if not item.name.endswith(".in"):
            continue
        text = item.read_text()
        for key, value in values.items():
            text = text.replace("@"+key+"@", value)
        if re.search(r"@[A-Z_]+@", text):
            raise ValueError("Unresolved service template parameter")
        if item.name == "boring-crocodile-mcp.service.in":
            text = text.replace("approval-secret", approval_credential(name))
        filename = item.name[:-3].replace("pond", name)
        if item.name == "system-crocodilepond.slice.in":
            filename = budget_slice(name)
        if filename == "boring-crocodile.conf":
            filename = tmpfiles_name(name)
        output[filename] = text
    if policy.service_registration_enabled:
        # Group creation/membership is an explicit administrator decision.
        from .pirates import REGISTRATION_GROUP, REGISTRATION_ROOT, registration_path
        parent = registration_path(name).parent
        output["config.toml"] += "service_registration_enabled = true\n"
        worker = f"boring-crocodile-worker-{name}.service"
        output[worker] = output[worker].replace("ReadWritePaths=", f"ReadWritePaths={parent} ", 1)
        tmpfiles = tmpfiles_name(name)
        output[tmpfiles] += (f"d {REGISTRATION_ROOT} 0755 root root - -\n"
                             f"d {parent} 2750 {setup.execution_user} {REGISTRATION_GROUP} - -\n")
    # Last, as its own table: lines appended above belong to [environments.NAME].
    # The home is the Pond's file root, so its file tools work from the start.
    output["config.toml"] += (f"[environments.{name}.files.roots.home]\n"
                              f"path = {q(setup.home)}\nread = true\nwrite = true\n")
    return output



def _replace_required(text, old, new):
    """Replace a line the controller template must contain; fail loudly if the
    template no longer contains it rather than render a unit without it."""
    if old not in text:
        raise ValueError("Controller unit template lacks: " + old)
    return text.replace(old, new)


def render_site(entries):
    """Combine reviewed Pond receipts; controller policy belongs to the first entry."""
    if not entries or entries[0][2].get("pond_name", INITIAL_POND) != INITIAL_POND:
        raise ValueError("Site rendering requires the initial Pond first")
    first_setup, first_policy, first_receipt = entries[0]
    output = {}
    fragments = []
    names, users, homes, ids = set(), set(), set(), set()
    workers, access, credentials = [], [], []
    for setup, policy, receipt in entries:
        name = receipt.get("pond_name", INITIAL_POND)
        profile_id = receipt.get("profile_id")
        if (name in names or setup.execution_user in users
                or setup.home in homes or profile_id in ids):
            raise ValueError("Ponds must have distinct names, workers, groups, homes and profile IDs")
        names.add(name); users.add(setup.execution_user)
        homes.add(setup.home); ids.add(profile_id)
        if setup.control_user != first_setup.control_user:
            raise ValueError("Ponds must use the same recorded controller")
        if receipt["users"][setup.control_user] != first_receipt["users"][setup.control_user]:
            raise ValueError("Pond controller identities differ")
        for key in ("public_base_url", "bind_host", "bind_port", "runtime_root"):
            if getattr(policy, key) != getattr(first_policy, key):
                raise ValueError("Shared controller policy differs: " + key)
        part = render(setup, policy, receipt)
        fragments.append(part.pop("config.toml"))
        part.pop("boring-crocodile-mcp.service")
        part.pop("boring-crocodile-mcp.target")
        if output.keys() & part.keys():
            raise ValueError("Rendered Pond artifacts collide")
        output.update(part)
        workers.append("boring-crocodile-worker-" + name + ".service")
        access.append(setup.execution_user)
        credential = approval_credential(name)
        credentials.append(f"LoadCredential={credential}:{CREDENTIALS_DIR / credential}")
    config = fragments[0]
    for fragment in fragments[1:]:
        config += "\n" + fragment[fragment.index("[ponds."):]
    output["config.toml"] = config
    control = render(first_setup, first_policy, first_receipt)["boring-crocodile-mcp.service"]
    control = _replace_required(control, f"boring-crocodile-worker-{INITIAL_POND}.service",
                                " ".join(workers))
    control = _replace_required(control, "SupplementaryGroups=" + first_setup.execution_user,
                                "SupplementaryGroups=" + " ".join(access))
    initial = approval_credential(INITIAL_POND)
    control = _replace_required(control, f"LoadCredential={initial}:{CREDENTIALS_DIR / initial}",
                                "\n".join(credentials))
    output["boring-crocodile-mcp.service"] = control
    output["boring-crocodile-mcp.target"] = render(first_setup, first_policy, first_receipt)["boring-crocodile-mcp.target"]
    return output

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--receipt", type=Path)
    mode.add_argument("--site", type=Path, help="JSON list of receipt/service_inputs file pairs")
    parser.add_argument("--service-inputs", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.site:
            if args.service_inputs:
                raise ValueError("--site supplies per-Pond service inputs")
            pairs = json.loads(args.site.read_text(), object_pairs_hook=unique_object)
            if not isinstance(pairs, list) or not pairs:
                raise ValueError("Site manifest must be a nonempty list")
        else:
            if args.service_inputs is None:
                raise ValueError("--receipt requires --service-inputs")
            pairs = [{"receipt": str(args.receipt), "service_inputs": str(args.service_inputs)}]
        entries = []
        for pair in pairs:
            if set(pair) != {"receipt", "service_inputs"}:
                raise ValueError("Each site entry needs receipt and service_inputs paths")
            receipt = json.loads(Path(pair["receipt"]).read_text(), object_pairs_hook=unique_object)
            setup = SetupInputs.parse(receipt["inputs"])
            policy = ServiceInputs.parse(json.loads(Path(pair["service_inputs"]).read_text(), object_pairs_hook=unique_object))
            for name in (setup.execution_user, setup.control_user):
                if account(name) != receipt["users"][name]:
                    raise ValueError("Host identity differs from completed setup: " + name)
            for name, expected in receipt["groups"].items():
                if group(name) != expected:
                    raise ValueError("Host group differs from completed setup: " + name)
            entries.append((setup, policy, receipt))
        if args.site:
            from .setup_apply import Host, site_receipts, validate_site
            recorded = site_receipts(Host())
            validate_site(Host(), recorded, None)
            supplied = {r.get("pond_name", INITIAL_POND): r for _, _, r in entries}
            if supplied != recorded:
                raise ValueError("Site manifest must match all recorded Ponds")
            rendered = render_site(entries)
        else:
            rendered = render(*entries[0])
        # Never overwrite an existing candidate or live configuration directory.
        args.output.mkdir(mode=0o700)
        for name, text in rendered.items():
            with (args.output/name).open("x") as stream:
                stream.write(text)
        Settings.load(args.output/"config.toml")
        print(json.dumps({"candidate":str(args.output), "files":sorted(rendered),
                          "installed":False, "activated":False}))
        return 0
    except (KeyError, ValueError, OSError) as error:
        print("Service rendering failed: " + str(error), file=__import__("sys").stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
