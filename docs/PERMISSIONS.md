# Narrowing or widening a Pond

The MCP adds no permission layer of its own: a Pond can do what its Unix
account can do: a Pond member gets a real account, not a sandbox. To trust a Pond less, narrow its account and the systemd units it
runs in; to trust it more, give it host administration deliberately. Both use
the ordinary Linux toolkit; nothing in the MCP changes.

## Narrowing: already in place

Every command runs in a socket-activated task unit
(`boring-crocodile-task-<pond>@.service`, `boring-pirate-task-<name>@.service`)
with `ProtectSystem=strict`, `ProtectHome=read-only` plus `ReadWritePaths=` for
the Pond's own tree, `NoNewPrivileges=yes`, `PrivateTmp=yes`, memory, task and
runtime limits, and its own cgroup that the worker verifies and waits to empty.

## Narrowing: further levers (systemd drop-ins on the task units)

| To | Use |
|---|---|
| block the network, or allow only some destinations | `IPAddressDeny=any`, `IPAddressAllow=…`, `RestrictAddressFamilies=`, or `PrivateNetwork=yes` |
| show only the Pond's own home | `TemporaryFileSystem=/home` with `BindPaths=`/`BindReadOnlyPaths=`, or `InaccessiblePaths=` |
| hide other processes | `ProtectProc=invisible`, `ProcSubset=pid` |
| reduce kernel surface | `SystemCallFilter=@system-service`, `ProtectKernelTunables=`, `ProtectKernelModules=`, `ProtectControlGroups=`, `RestrictNamespaces=`, `LockPersonality=`, `PrivateDevices=` |
| cap resources | `MemoryMax=`, `TasksMax=`, `CPUQuota=`, `RuntimeMaxSec=` |

`RestrictSUIDSGID=` is off on purpose for ordinary task units: under it systemd answers `openat2` with ENOSYS, and Ubuntu 26.04's
tar does not fall back, so archives and package builds fail in the Pond.

`systemd-analyze security <unit>` scores a unit and lists what is left open.
Apply changes with `systemctl edit <unit>` (a drop-in), test in a guest first,
and keep in mind they act per Pond (per account), not per command.

## Narrowing: outside systemd

- Firewall by owner: nftables `meta skuid`/`meta skgid` (iptables
  `-m owner --uid-owner`) in the output chain. Matches everything that UID
  sends, the Pond's service included; root containers' forwarded traffic is
  not matched, rootless containers of that UID are.
- Accounts and groups: what a Pond can read is what its Unix account can read.
  Group memberships and home modes are the coarsest and most reliable lever.
- AppArmor or SELinux profiles for the task runner, for sites that already use
  them.
- A whole Pond in a system container or VM (systemd-nspawn, Incus/LXC, or
  Podman/Docker running systemd), with the Pond homes as bind mounts. Possible
  in principle; not yet tried.

## Widening: host administration by a Pond

Every Pond starts with `no_new_privileges = true`: a restricted command task.
A root administrator may set it to `false` in the Pond file's `[execution]`
section and, separately, give that account sudo. Neither happens at install.
Such a Pond can change the whole host, other Ponds and its own restrictions
included; keep work that needs isolation in a separate, restricted Pond.
The controller and worker stay non-root.

Turning off NoNewPrivileges alone does not work: capability and mount
directives still block root work. Render writes the complete task policy into
the base task unit from that one setting:

    sudo boring-crocodile-setup --site-render
    sudo boring-crocodile-setup --site-render --apply

Let the Pond's current tasks finish before applying a policy change. Review the
render plan and type `APPLY`; the common render repairs the units and restarts
the workers/controller. Render accepts and shows every root-owned external
drop-in verbatim, with a warning for settings also written by the
description. Keep secrets in files referenced by `LoadCredential=` and
keep their contents out of drop-ins. Render never
rewrites or deletes those files; symlinks, masks and unsafe file ownership or
modes are refused. There is no directive allow-list. systemd owns precedence.
Site-check and status compare effective task policy with the description;
a mismatch is irregular/degraded. Reversing follows the same path with `true`;
remove the conflicting drop-in and sudo grant separately.

The one-time 0.4.21 importer projects an exact old generated
`50-pond-task-policy.conf` into the description and proposes its removal.
It refuses foreign content. Ordinary render never removes external drop-ins.

`admin status` compares the same properties (`matches`, `mismatch`,
`unknown`; `not_applicable` without commands); a mismatch degrades health.
Matching properties do not prove that sudo works; a real command does. Root
can create services outside the task's cgroup, so a task's timeout does not
stop those.
