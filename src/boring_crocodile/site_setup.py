"""Configure desired site policy, then use the common plan/APPLY/render owner."""
from __future__ import annotations

import argparse
from copy import deepcopy
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from urllib.parse import urlsplit

from .approval import confirm_apply
from boring_deploy import Failure

from .setup import read_input_json
from .site_host import SiteConflict as SiteConflict
from .site_layout import STATE_DIR
from .site_model import SiteDescription
from .site_policy import ServiceInputs as ServiceInputs
from . import site_render

STATE = STATE_DIR


def runtime_root(executable=sys.executable):
    path = Path(executable)
    parts = path.parts
    if "releases" in parts and parts[-3:-1] == ("app", "bin"):
        return str(Path(*parts[:parts.index("releases")]) / "current")
    return str(path.parent.parent)


def defaults():
    from .setup_apply import _policy
    return dict(bind_host="127.0.0.1", bind_port=8503, runtime_root=runtime_root(), **_policy())


def _candidate(policy, paths):
    site = SiteDescription.read(paths.config, owner=paths.owner)
    main = deepcopy(site.document)
    main["site"].update(state="running", runtime_root=policy.runtime_root)
    public = urlsplit(policy.public_base_url)
    bind = "[" + policy.bind_host + "]" if ":" in policy.bind_host else policy.bind_host
    main["server"].update(public_base_url=policy.public_base_url, bind_host=policy.bind_host,
        bind_port=policy.bind_port, protected_ports=list(policy.protected_ports),
        allowed_hosts=list(dict.fromkeys([public.netloc, public.hostname, bind+":"+str(policy.bind_port)])))
    ponds = {name: deepcopy(p.document) for name,p in site.ponds.items()}
    first = ponds["pond"]
    first["enabled"] = True
    from .site_pirates import pond_admission
    pond_admission(main, ["pond"], "restore")
    first["limits"] = {key: getattr(policy, key)
                       for key in ("memory_bytes", "swap_bytes", "tasks_max", "task_tasks_max")}
    first["execution"].update(no_new_privileges=policy.no_new_privileges)
    return SiteDescription.parse(main, ponds)


def plan(policy, *, paths=None, host=None):
    paths = paths or site_render.Paths()
    candidate = _candidate(policy, paths)
    proposal, _ = site_render.plan(paths=paths, host=host, candidate=candidate)
    return proposal, candidate


def apply(policy, approved_digest, *, paths=None, host=None):
    paths = paths or site_render.Paths()
    return site_render.apply(approved_digest, paths=paths, host=host, candidate=_candidate(policy, paths))


def review_summary(proposal):
    return site_render.review(proposal)




def _bytes(value):
    for unit, size in (("G", 1024**3), ("M", 1024**2)):
        if value % size == 0:
            return f"{value // size}{unit}"
    return f"{value // 1024**2}M"


def _bind(host, port):
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((host, port))


def wizard(ask=input, tell=None):
    """Each answer is checked as it is given, and asked again with the reason."""
    from .site_policy import parse_size as _size
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
    args = parser.parse_args(argv)
    try:
        if args.wizard:
            if not sys.stdin.isatty():
                raise SiteConflict("Guided setup needs a terminal")
            from .setup import terminal_ask
            policy = wizard(terminal_ask)
        else:
            policy = ServiceInputs.parse(read_input_json(args.config))
        proposal, _ = plan(policy)
        if not args.apply:
            print(json.dumps(proposal, indent=2))
            return 0
        print(review_summary(proposal), file=sys.stderr)
        digest = confirm_apply(proposal, "save this site policy and render the site")
        print(json.dumps(apply(policy, digest), indent=2))
        return 0
    except (Failure, ValueError, KeyError, OSError, EOFError, subprocess.SubprocessError) as error:
        print("Site configuration failed: " + str(error), file=sys.stderr)
        return 2
