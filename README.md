# The Boring Crocodile

**A self-hosted place for AI clients to work.** The Crocodile is an MCP server:
each Pond is a real Unix account on your host with a home, a handbook the AI
reads at the start of every conversation, file tools, a shell, durable jobs,
and two kinds of neighbours: Pirates (local agents) and services (MCP
servers). You own the host and its policy; clients and models can change while
the working place stays.

All of it — code, tests, docs — was written by AI (OpenAI's models and Anthropic's Claude); Wald decided what to build, tested it, ran it and had the last word ([all of it is AI](https://github.com/the-boring-crocodile/boring-pond/blob/main/README.md#all-of-it-is-ai)).

It is meant for a personally controlled server or a small trusted
collaboration. Enterprise identity, hostile multi-tenancy, high availability
and fleet-scale audit are not goals. It began as a practical question about
letting an AI run commands on one's own Linux machine; its shape came from
use, and should keep coming from use.

Part of the [Pond family](https://github.com/the-boring-crocodile/boring-pond/blob/main/README.md); shared libraries from
[Pond Common](https://github.com/the-boring-crocodile/boring-pond-common/blob/main/README.md).

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/crocodile-dark.svg">
  <img alt="The Crocodile, one server with a Unix account per Pond: Claude, signed in to the Pond pond, and ChatGPT or VS Code, signed in to garden, call the controller over MCP; the controller, in the control account with its sign-in store, where tokens are kept only as SHA-256, hands each call over a local socket to its Pond's worker, one per Pond in that Pond's own account. A worker runs each command in a task runner, a systemd unit with a deadline and a cgroup, hands assignments to Pirates and calls services with short-lived tokens from the controller; every worker does all three, each drawn once." src="docs/crocodile.svg">
</picture>

## Documentation

| | |
|---|---|
| [INSTALL](docs/INSTALL.md) | A new site, and adding Ponds, Pirates and services |
| [OPERATIONS](docs/OPERATIONS.md) | Updating, configuring, backing up, uninstalling |
| [ADMIN](docs/ADMIN.md) | The admin command |
| [ARCHITECTURE](docs/ARCHITECTURE.md) | How it is built, and where trust lies |
| [TOOLS](docs/TOOLS.md) | What the MCP tools promise |
| [SERVICES_AND_PIRATES](docs/SERVICES_AND_PIRATES.md) | The neighbours |
| [PERMISSIONS](docs/PERMISSIONS.md) | Narrowing or widening a Pond |
| [OAUTH](docs/OAUTH.md) | Sign-in and its store |
| [CHANGELOG](CHANGELOG.md) | What changed |

The agent-facing guide that `pond_bootstrap` serves is
`src/boring_crocodile/templates/CAPABILITIES.md`. What a fresh install writes:
[site/default](site/default/README.md).

## Tools

| Group | Tools |
| --- | --- |
| Pond | `pond_profile`, `pond_ping`, `pond_bootstrap`, `pond_limits` |
| Files | `file_list`, `file_search`, `file_read`, `file_patch`, `file_write`, `file_read_bytes`, `file_write_bytes` |
| Commands | `exec`, `batch` |
| Jobs | `job_start` (command, operations or Pirate), `job_list`, `job_status`, `job_output`, `job_wait`, `job_stop` |
| Pirates | `pirate_list`, `pirate_describe` |
| Services | `service_list`, `service_describe`, `service_call` |

The connection decides the Pond; no tool takes one. Unknown arguments are
refused by name. A refusal is `{code, message, ...}`, a failure `{failure}`.

## Security invariant

The Crocodile's accounts are ordinary, unprivileged users. If one ever holds
authority it should not (root, sudo, another account's files), that is a
misconfiguration to fix, never a feature to build on. The one exception is
chosen, not found: a Pond you give host administration on purpose
([PERMISSIONS](docs/PERMISSIONS.md#widening-host-administration-by-a-pond)).

## Development checks

Dependencies: pyproject.toml declares ranges; `requirements.lock` pins every
runtime dependency with hashes for Python 3.10-3.14 (regenerate with
`scripts/lock.py`). Pond Common (registry, file-core, service-kit,
worker-kit, deploy) comes from its released wheels. Set up a development venv:

```text
python -m venv .venv && . .venv/bin/activate
pip install --require-hashes --find-links <Common dist> -r requirements.lock
pip install --no-deps -e .
pip install -r requirements-dev.txt
```

To change Common alongside, install your Common checkout editable into the same
venv instead (`pip install --no-deps -e ../boring-pond-common/packages/<name>`).
Then run the tests and the scoped strict mypy gate (the files listed under
`[tool.mypy]`; Common's modules are checked in Common):

```text
python tests/run_profile.py unit
python -m mypy
```

The same from scratch, as CI would run it (a fresh venv from the lock, then
both): `python scripts/check.py --common-wheels <Common dist>`.

Release artifact (standalone CPython, locked wheels, deploy): `scripts/build.py
--common-wheels <Common dist> --out <new dir>`; see [OPERATIONS](docs/OPERATIONS.md).

`PYTHONPATH=tests` matters: a test starts a child process that imports
`tests/pond_config.py`; without it that one test fails for the wrong reason. Configuration and tool inputs are validated
strictly at runtime: unknown keys and arguments are refused, booleans and
numeric strings are not numbers.

Shared libraries (boring-registry, boring-file-core, boring-service-kit,
boring-worker-kit, boring-deploy) come from [Pond Common](https://github.com/the-boring-crocodile/boring-pond-common/blob/main/README.md)
releases.

## Licence

Code under the GNU AGPL, version 3 or later; documentation under CC BY-SA
4.0. The Pond handbook template and the site defaults
(`src/boring_crocodile/templates/HANDBOOK.md`, `site/default/etc/`,
`site/default/two-ponds/`) are public domain (CC0). What you make with the
tools is yours. Details and the exact terms: [COPYING.md](COPYING.md).
