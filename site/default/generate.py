#!/usr/bin/env python3
"""Generate the one- and two-Pond references from typed site declarations."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import sys

import boring_crocodile
from boring_crocodile.setup import SetupInputs, context_payload
from boring_crocodile.site_layout import CONFIG_FILE, CREDENTIALS_DIR, approval_credential
from boring_crocodile.site_model import Identity, SiteDescription, toml_document
from boring_crocodile.site_policy import ServiceInputs
from boring_crocodile.site_render import Paths
from boring_crocodile.site_setup import defaults
from boring_crocodile.site_templates import render_description

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent.parent / "src"
if not Path(boring_crocodile.__file__).resolve().is_relative_to(SOURCE.resolve()):
    raise SystemExit(f"Set PYTHONPATH={SOURCE}; imported {boring_crocodile.__file__}")

SETUP_ANSWERS = dict(execution_user="boring-crocodile", control_user="boring-crocodile-mcp",
                     home="/home/boring-crocodile", context_mode="starter",
                     context_source=None, context_files=[])
SITE_ANSWERS = asdict(ServiceInputs.parse(dict(defaults(), public_base_url="https://mcp.example.invalid",
                    runtime_root="/opt/boring-crocodile/current", memory_bytes=2 * 1024**3)))
SECOND_POND = "second"
SECOND_ANSWERS = dict(SETUP_ANSWERS, execution_user="boring-crocodile-second",
                      home="/home/boring-crocodile-second")
EXAMPLE_UIDS = {"boring-crocodile": 901, "boring-crocodile-mcp": 902,
                "boring-crocodile-second": 903}
EXAMPLE_PROFILE_IDS = {"pond": "00000000-0000-4000-8000-000000000001",
                       SECOND_POND: "00000000-0000-4000-8000-000000000002"}


def _identity(name, home):
    return asdict(Identity(name, EXAMPLE_UIDS[name], name, EXAMPLE_UIDS[name], home, "/usr/sbin/nologin"))


def _pond(answers, name):
    setup = SetupInputs.parse(answers)
    return dict(enabled=True, identity=_identity(setup.execution_user, setup.home),
        credential_file=str(CREDENTIALS_DIR / approval_credential(name)),
        limits={key: SITE_ANSWERS[key] for key in ("memory_bytes", "swap_bytes", "tasks_max", "task_tasks_max")},
        pond=dict(profile_id=EXAMPLE_PROFILE_IDS[name], display_name=setup.display_name or (
            "The Pond" if name == "pond" else name),
            files=[name for name,_ in context_payload(setup)], welcome_file="WELCOME.md"),
        execution=dict(files={"roots": {"home": {"read": True, "write": True}}}))


def description(second=False):
    policy = SITE_ANSWERS
    main = dict(site=dict(schema=1, state="running", runtime_root=policy["runtime_root"],
                         controller=_identity(SETUP_ANSWERS["control_user"], "/nonexistent")),
        server=dict(public_base_url=policy["public_base_url"], bind_host=policy["bind_host"],
            bind_port=policy["bind_port"], protected_ports=list(policy["protected_ports"]),
            allowed_hosts=["mcp.example.invalid", "127.0.0.1:8503"]),
        auth=dict(oauth_database_file="/var/lib/boring-crocodile/oauth.sqlite3"))
    ponds = {"pond": _pond(SETUP_ANSWERS, "pond")}
    if second:
        ponds[SECOND_POND] = _pond(SECOND_ANSWERS, SECOND_POND)
    return SiteDescription.parse(main, ponds)


def reference_files():
    output = {}
    paths = Paths()
    for prefix, site in (("", description()), ("two-ponds/", description(True))):
        files = {str(CONFIG_FILE): toml_document(site.document)}
        files.update({str(CONFIG_FILE.parent / "ponds" / name / "config.toml"): toml_document(p.document)
                      for name,p in site.ponds.items()})
        files.update({str(paths.location(name)): body for name,body in render_description(site).items()})
        output.update({prefix+path.lstrip("/"): body for path,body in sorted(files.items())})
    return output


def main():
    for relative, body in reference_files().items():
        path = HERE / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        print(relative)
    return 0


if __name__ == "__main__":
    sys.exit(main())
