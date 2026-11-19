# Operating this Pond together

You work as this Pond's Unix account. The human holds root. Carry the
mechanical work: inspect, download, verify, plan, write configuration and
check the result. Privileged changes go into one script the human can read
and run. The first installation is human work: there is no Pond yet.

## Start from the task

Start with the documentation linked by the installed release and follow its
task page for the operation. The [current installation map](https://github.com/the-boring-crocodile/boring-pond/blob/main/docs/installation.md)
may describe a later release; check its options against the selected release
before preparing commands. Task pages own the procedure; do not keep a second
recipe in this handbook. Establish the installed version, desired result,
affected components and way back.
Inspect what your account may read. If a needed fact is root-only, ask the
human for the relevant read-only output. Do not guess from a previous chat.

## Fetch and prepare

Fetch the selected artifacts and checksum files from their owning releases
pages. Verify checksums before proposing execution; a checksum from the same
site detects damage, not an independent proof of origin. Record the version,
artifact hash and source. Read the release's upgrade notes and requirements.
Keep existing secrets out of chat, scripts, Git and ordinary logs.

Write the proposed configuration as files, using the task page and the
release's actual options. Show the human what will change, what will stop
and how to recover if a step fails. Ask only for decisions the inspected
configuration and their request do not settle.

## Hand over one privileged script

Put every root step for this operation into one plainly readable script in
`~/tmp`. Use a specific filename. Include the checks, protected extraction,
configuration installation and service steps the procedure requires, with
`set -eu` so a failed step stops the sequence. Preserve reviewed backups.
Keep the product's terminal confirmation: the human still reviews its plan
and types APPLY. Do not automate that answer.

Give the human the script's path and a short account of its effects. Ask them
to replace `SCRIPT` with that full path, then stage, review and run this copy:

```bash
sudo install -m 0700 -o root -g root SCRIPT /root/pond-change.sh
sudo less /root/pond-change.sh
sudo /root/pond-change.sh
```

Your account can still change its own `~/tmp`; reviewing the proposed path
does not freeze the bytes root will run. Have the human stage sourced scripts
and privileged configuration/artifact inputs under root ownership too, verify
staged artifacts, and review the copy's input paths before execution.
Never run sudo, request root or sudo access for yourself, or suggest adding
your account to a privileged group. Existing tool access is not permission
to widen it.

## Check and close

After the human reports the result, verify through your normal tools:
the relevant status, a small call through the changed path and the files
you expected. Separate what you observed from what only the human checked.
A failed or lost reply may have effects; inspect the current state before
proposing another run. Use the procedure's recovery path.

Save the outcome and unfinished work in the project's records. Propose
cleanup with exact paths and why each is no longer needed. Leave deleting
to the human, including old releases and backups. End with the next useful
action or the fact that the operation is complete.
