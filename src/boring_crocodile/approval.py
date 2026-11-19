"""The operator reviews a fresh plan and confirms it at the terminal."""
from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Mapping


def plan_digest(proposal: Mapping[str, object]) -> str:
    value = {key: item for key, item in proposal.items() if key != "plan_digest"}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def confirm_apply(proposal: Mapping[str, object], action: str) -> str:
    """Show the plan; return its internal stale-state guard only after typed APPLY."""
    digest = proposal.get("plan_digest")
    if not isinstance(digest, str) or not digest:
        raise ValueError("The plan has no state digest")
    print(json.dumps(proposal, indent=2), file=sys.stderr)
    if not sys.stdin.isatty():
        raise ValueError("--apply requires a terminal; review the plan and type APPLY")
    print(f"Type APPLY to {action}: ", file=sys.stderr, end="", flush=True)
    if sys.stdin.readline().strip() != "APPLY":
        raise ValueError("Apply was not confirmed")
    return digest
