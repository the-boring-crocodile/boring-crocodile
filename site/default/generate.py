#!/usr/bin/env python3
"""Regenerate the fresh-install reference in site/default from the renderer.

    PYTHONPATH=src \
        <development venv with Pond Common>/bin/python site/default/generate.py

Every file below etc/ (one Pond) and two-ponds/etc/ (a second Pond added
with `boring-crocodile-setup --add-pond second` before activation) is exactly what `boring-crocodile-setup` and
`--site-config --apply` would install for the default answers, at the path
activation installs it to (site_setup._location). The renderer
(setup_services.render) stays the only owner of these files;
tests/test_site_default.py fails when they drift.

Host-dependent values are fixed for the reference and named in README.md:
account UIDs (useradd assigns them), the memory budget (a quarter of RAM),
the public URL and the profile ID (a fresh UUID at setup).
"""
from __future__ import annotations

import sys
from pathlib import Path

from boring_crocodile.setup import SetupInputs, context_payload
from boring_crocodile.setup_services import ServiceInputs, render, render_site
from boring_crocodile.site_setup import _location, defaults

import boring_crocodile

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent.parent / "src"
if not Path(boring_crocodile.__file__).resolve().is_relative_to(SOURCE.resolve()):
    raise SystemExit(f"boring_crocodile is imported from {boring_crocodile.__file__}, not from "
                     f"{SOURCE}: set PYTHONPATH (see site/default/README.md)")

SETUP_ANSWERS = dict(execution_user="boring-crocodile", control_user="boring-crocodile-mcp",
                     home="/home/boring-crocodile", context_mode="starter",
                     context_source=None, context_files=[])
SITE_ANSWERS = dict(defaults(), public_base_url="https://mcp.example.invalid",
                    runtime_root="/opt/boring-crocodile/current",
                    memory_bytes=2 * 1024**3)
SECOND_POND = "second"
SECOND_ANSWERS = dict(SETUP_ANSWERS, execution_user="boring-crocodile-second",
                      home="/home/boring-crocodile-second")
EXAMPLE_UIDS = {"boring-crocodile": 901, "boring-crocodile-mcp": 902,
                "boring-crocodile-second": 903}
EXAMPLE_PROFILE_IDS = {"pond": "00000000-0000-4000-8000-000000000001",
                       SECOND_POND: "00000000-0000-4000-8000-000000000002"}


def _entry(answers: dict, pond: str):
    setup = SetupInputs.parse(answers)
    receipt = dict(status="complete", inputs=setup.as_input(),
                   profile_id=EXAMPLE_PROFILE_IDS[pond],
                   users={name: {"uid": EXAMPLE_UIDS[name]}
                          for name in (setup.execution_user, setup.control_user)},
                   context_root=setup.home + "/handbook",
                   documents=[{"filename": name} for name, _ in context_payload(setup)])
    if pond != "pond":
        receipt["pond_name"] = pond
    return setup, ServiceInputs.parse(SITE_ANSWERS), receipt


def _placed(rendered: dict[str, str], prefix: str = "") -> dict[str, str]:
    return {prefix + str(_location(name)).lstrip("/"): body
            for name, body in sorted(rendered.items())}


def reference_files() -> dict[str, str]:
    """Relative path under site/default -> content, for both references."""
    one = _placed(render(*_entry(SETUP_ANSWERS, "pond")))
    two = _placed(render_site([_entry(SETUP_ANSWERS, "pond"),
                               _entry(SECOND_ANSWERS, SECOND_POND)]), "two-ponds/")
    return {**one, **two}


def main() -> int:
    for relative, body in reference_files().items():
        path = HERE / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        print(relative)
    return 0


if __name__ == "__main__":
    sys.exit(main())
