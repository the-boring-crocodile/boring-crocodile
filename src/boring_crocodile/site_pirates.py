"""Edit Pirate admission and worker Unix access, then use the common site render."""
from __future__ import annotations
import argparse
from copy import deepcopy
import json
import os
import re
import subprocess
import sys

from .approval import confirm_apply, plan_digest
from .site_host import Host, SiteConflict
from .site_layout import site_lock
from .site_model import SiteDescription, check_account_name
from .site_render import Paths as Paths
from . import site_render

NAME = re.compile(r"[a-z][a-z0-9-]{0,62}\Z")


def pond_admission(main, ponds, action):
    """Edit active/saved pairs in the one caller roster; never record worker UIDs."""
    selected = set(ponds)
    for caller in main.get("pirate_services", {}).get("callers", {}).values():
        active, parked = set(caller["ponds"]), set(caller.get("parked_ponds", []))
        if action == "restore":
            active.update(parked & selected)
            parked.difference_update(selected)
        else:
            if action == "park":
                parked.update(active & selected)
            else:
                parked.difference_update(selected)
            active.difference_update(selected)
        caller["ponds"] = sorted(active)
        if parked:
            caller["parked_ponds"] = sorted(parked)
        else:
            caller.pop("parked_ponds", None)


def _prepare(kind, name, account, ponds, paths, host, *, retire=False):
    if not NAME.fullmatch(name):
        raise SiteConflict("Pirate names use lower-case letters, digits and dashes")
    site = SiteDescription.read(paths.config, owner=paths.owner)
    if site.state == "removed":
        raise SiteConflict("Activate the site before changing Pirate admission")
    if retire and kind != "remove":
        raise SiteConflict("Retirement belongs to Pirate removal")
    store = host.oauth_store(site)
    grants = store.caller_services("pirate/" + name)
    main = deepcopy(site.document)
    callers = main.setdefault("pirate_services", {}).setdefault("callers", {})
    documents = {n: deepcopy(p.document) for n,p in site.ponds.items()}
    previous = callers.get(name)
    chosen, uid = [], previous["uid"] if previous else None
    if kind == "add":
        if account is None:
            if previous is None:
                raise SiteConflict("A new Pirate needs --account; existing Pirates keep their recorded identity")
            account = host.user_by_uid(uid)["name"]
        check_account_name(account)
        user = host.account(account)
        if not user:
            raise SiteConflict("The Pirate's installer must create its account first")
        uid = user["uid"]
        if previous and previous["uid"] != uid:
            raise SiteConflict("One Pirate name has one account; remove it before admitting another")
        if any(n != name and value["uid"] == uid for n,value in callers.items()):
            raise SiteConflict("Account is already admitted under another Pirate name")
        chosen = sorted(n for n,p in site.ponds.items() if p.enabled) if ponds == ["all"] else sorted(set(ponds))
        if not chosen or any(n not in site.ponds or not site.ponds[n].enabled for n in chosen):
            raise SiteConflict("Choose enabled Ponds")
        chosen = sorted(set(chosen) | set(previous["ponds"] if previous else []))
        callers[name] = {**(previous or {}), "uid": uid, "ponds": chosen}
        parked = sorted(set(callers[name].get("parked_ponds", [])) - set(chosen))
        if parked:
            callers[name]["parked_ponds"] = parked
        else:
            callers[name].pop("parked_ponds", None)
    elif ponds is not None:
        if retire:
            raise SiteConflict("--retire removes the whole Pirate; omit --ponds")
        chosen = sorted(set(ponds))
        if not chosen or any(n not in site.ponds for n in chosen):
            raise SiteConflict("Choose known Ponds to remove")
        if previous is None:
            raise SiteConflict("No admitted Pirate: " + name)
        pond_admission({"pirate_services": {"callers": {name: previous}}}, chosen, "retire")
    else:
        callers.pop(name, None)
    candidate = SiteDescription.parse(main, documents)
    rendered, prepared = site_render.plan(paths=paths, host=host, candidate=candidate)
    authority = {"socket": str(site.settings.pirate_service_socket), "uid": site.controller.uid}
    unchanged = not rendered["changes"] and not any(g["add"] or g["remove"] for g in rendered["groups"])
    proposal = {"kind": "pirate_" + kind, "pirate": name, "account": account, "uid": uid,
                "ponds": chosen, "render": rendered, "service_authority": authority,
                "retire": retire, "existing_service_grants": grants,
                "status": "already_serves" if kind == "add" and unchanged else "proposed"}
    if kind == "remove" and ponds is not None:
        proposal["partial"] = True
    proposal["plan_digest"] = plan_digest(proposal)
    return proposal, (rendered, prepared, store)


def plan_add(name, account=None, ponds=None, *, paths=None, host=None):
    return _prepare("add", name, account, ponds or [], paths or Paths(), host or Host())


def plan_remove(name, *, ponds=None, paths=None, host=None, retire=False):
    return _prepare("remove", name, None, ponds, paths or Paths(), host or Host(), retire=retire)


def apply(kind, approved_digest, *args, ponds=None, paths=None, host=None, retire=False):
    paths, host = paths or Paths(), host or Host()
    host.require_authority()
    account, ponds = (args[1], args[2]) if kind == "add" else (None, ponds)
    with site_lock(paths.state, owner=paths.owner):
        proposal, (rendered, prepared, store) = _prepare(kind, args[0], account, ponds, paths, host,
                                                       retire=retire)
        if proposal["plan_digest"] != approved_digest:
            raise SiteConflict("Host or admission changed since the reviewed plan")
        if retire:
            store.retire_caller("pirate/" + args[0], proposal["existing_service_grants"])
        outcome = site_render._apply_locked(rendered, prepared, paths, host)
    return {**outcome, "status": "admitted" if kind == "add" else "updated" if ponds is not None else "removed",
            "pirate": args[0], "retired": retire, "ponds": proposal["ponds"],
            "service_authority": proposal["service_authority"]}


def review(proposal):
    label = "Ponds to remove" if proposal.get("partial") else "Ponds"
    return (f"Pirate {proposal['pirate']}: {proposal['kind']}; {label} {proposal['ponds']}\n"
            + ("Service grants to revoke" if proposal["retire"] else "Existing service grants (kept)")
            + ": " + json.dumps(proposal["existing_service_grants"]) + "\n"
            + site_render.review(proposal["render"]))


def main(kind: str, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="boring-crocodile-admin pirate-" + kind, description=__doc__)
    parser.add_argument("name")
    if kind == "add":
        parser.add_argument("--account", help="required only for a new Pirate")
        parser.add_argument("--ponds", required=True)
    else:
        parser.add_argument("--ponds", help="remove these active or parked Ponds; keep the Pirate admitted")
        parser.add_argument("--retire", action="store_true",
                            help="also revoke pirate/NAME service grants and issued service tokens")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    planned = (args.name, args.account, args.ponds.split(",")) if kind == "add" else (args.name,)
    try:
        if os.geteuid() != 0:
            raise SiteConflict("Changing admission requires root")
        options = ({"retire": args.retire, "ponds": None if args.ponds is None else args.ponds.split(",")}
                   if kind == "remove" else {})
        proposal, _ = (plan_add if kind == "add" else plan_remove)(*planned, **options)
        print(review(proposal), file=sys.stderr)
        if not args.apply:
            print(json.dumps(proposal, indent=2))
            return 0
        digest = confirm_apply(proposal, "save admission and render the desired site")
        print(json.dumps(apply(kind, digest, *planned, **options), indent=2))
        return 0
    except (ValueError, OSError, KeyError, EOFError, subprocess.SubprocessError) as error:
        print("Pirate admission failed: " + str(error), file=sys.stderr)
        return 2
