"""Load the one immutable MCP guide from the installed application package."""
from __future__ import annotations

import hashlib
from importlib.resources import files


def load_shared_guide() -> dict[str, str]:
    content = files("boring_crocodile").joinpath(
        "templates/CAPABILITIES.md").read_bytes()
    return {
        "filename": "CAPABILITIES.md",
        "version": "20",
        "sha256": hashlib.sha256(content).hexdigest(),
        "content": content.decode("utf-8"),
    }
