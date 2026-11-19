"""Enable parked declarations, then use the common site render. Service policy stays in SQLite."""
from __future__ import annotations
import argparse
from copy import deepcopy
import json
import os
import shlex
import subprocess
import sys

from .approval import confirm_apply, plan_digest
from .site_host import Host, SiteConflict
from .site_layout import site_lock
from .site_model import SiteDescription
from .site_render import Paths as Paths
from . import site_render
from .site_pirates import pond_admission


def budget(memory=None, swap=None, tasks=None, task_tasks=None):
    from .site_policy import parse_size as _size
    return {key: value for key,value in dict(
        memory_bytes=_size(memory.upper()) if memory is not None else None,
        swap_bytes=_size(swap.upper()) if swap is not None else None,
        tasks_max=tasks, task_tasks_max=task_tasks).items() if value is not None}


def _prepare(grant, ceilings, bring_back, paths, host):
    site = SiteDescription.read(paths.config, owner=paths.owner)
    if site.state == "removed":
        raise SiteConflict("Configure/activate the site before enabling Ponds")
    chosen = sorted(set(bring_back or []))
    if not set(chosen) <= set(site.ponds):
        raise SiteConflict("Unknown Pond: " + ", ".join(sorted(set(chosen) - set(site.ponds))))
    documents = {n: deepcopy(p.document) for n,p in site.ponds.items()}
    for name in chosen:
        documents[name]["enabled"] = True
        documents[name]["limits"].update(ceilings or {})
        limits = documents[name]["limits"]
        if ceilings and "tasks_max" in ceilings and "task_tasks_max" not in ceilings:
            limits["task_tasks_max"] = min(limits["task_tasks_max"], limits["tasks_max"])
    main = deepcopy(site.document)
    pond_admission(main, chosen, "restore")
    candidate = SiteDescription.parse(main, documents)
    rendered, prepared = site_render.plan(paths=paths, host=host, candidate=candidate)
    grants, store = [], None
    if grant and chosen:
        store = host.oauth_store(site)
        available = {entry["name"]: entry for entry in store.service_list()}
        names = sorted(available) if grant == ["all"] else sorted(set(grant))
        if not set(names) <= set(available):
            raise SiteConflict("Unknown service: " + ", ".join(sorted(set(names) - set(available))))
        for name in names:
            before = available[name]["callers"]
            after = {**before, **{pond: available[name]["default_role"] for pond in chosen if pond not in before}}
            if after != before:
                grants.append(store.service_grants(name, after))
    proposal = {"kind": "site_extension", "added_ponds": chosen, "render": rendered,
                "service_grants": grants, "status": "proposed"}
    proposal["plan_digest"] = plan_digest(proposal)
    return proposal, (rendered, prepared, store)


def plan(grant=None, ceilings=None, *, paths=None, host=None, bring_back=None):
    return _prepare(grant, ceilings, bring_back, paths or Paths(), host or Host())


def apply(approved_digest, grant=None, ceilings=None, *, paths=None, host=None, bring_back=None):
    paths, host = paths or Paths(), host or Host()
    host.require_authority()
    with site_lock(paths.state, owner=paths.owner):
        proposal, (rendered, prepared, store) = _prepare(grant, ceilings, bring_back, paths, host)
        if proposal["plan_digest"] != approved_digest:
            raise SiteConflict("Host, declarations or service policy changed since the reviewed plan")
        try:
            for row in proposal["service_grants"]:
                store.service_grants(row["name"], row["after"], row["plan_digest"])
            outcome = site_render._apply_locked(rendered, prepared, paths, host)
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            retry = ["boring-crocodile-setup", "--site-extend"]
            if proposal["added_ponds"]:
                retry += ["--bring-back", ",".join(proposal["added_ponds"])]
            if grant:
                retry += ["--grant-services", ",".join(grant)]
            for key, flag in (("memory_bytes", "--memory"), ("swap_bytes", "--swap"),
                              ("tasks_max", "--tasks")):
                if ceilings and key in ceilings:
                    retry += [flag, str(ceilings[key])]
            retry.append("--apply")
            raise SiteConflict("Extension did not finish; approved service grants may be saved. "
                               "Rerun " + shlex.join(retry) + " to review and repair. " + str(error)) from error
    return {**outcome, "status": "extended", "added_ponds": proposal["added_ponds"]}


def review(proposal):
    return ("Enable Ponds: " + (", ".join(proposal["added_ponds"]) or "none; repair the desired site")
            + "\nService grants: " + json.dumps(proposal["service_grants"]) + "\n"
            + site_render.review(proposal["render"]))


def told(outcome):
    return "Applied the desired site; selected Ponds: " + (", ".join(outcome.get("added_ponds", [])) or "none")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="boring-crocodile-setup --site-extend", description=__doc__)
    parser.add_argument("--grant-services", help="'all' or comma-separated services")
    parser.add_argument("--memory")
    parser.add_argument("--swap")
    parser.add_argument("--tasks", type=int)
    parser.add_argument("--bring-back", help="explicit Ponds to enable or repair, comma-separated; omission selects none")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    grant = args.grant_services.split(",") if args.grant_services else None
    back = args.bring_back.split(",") if args.bring_back else None
    try:
        if os.geteuid() != 0:
            raise SiteConflict("Extending the site requires root")
        ceilings = budget(args.memory, args.swap, args.tasks)
        proposal, _ = plan(grant, ceilings, bring_back=back)
        print(review(proposal), file=sys.stderr)
        if not args.apply:
            print(json.dumps(proposal, indent=2))
            return 0
        digest = confirm_apply(proposal, "save this desired site and render it")
        outcome = apply(digest, grant, ceilings, bring_back=back)
        print(told(outcome), file=sys.stderr)
        print(json.dumps(outcome, indent=2))
        return 0
    except (ValueError, OSError, KeyError, EOFError, subprocess.SubprocessError) as error:
        print("Site extension failed: " + str(error), file=sys.stderr)
        return 2
