# Crocodile outcome causes

Local producers use the closed shared [Common cause vocabulary](https://github.com/the-boring-crocodile/boring-pond-common/tree/main/packages/boring-outcomes)
plus the component additions below. A cause is independent of status: refused
means nothing started or changed; failed means work started and may have effects.
Valid received outcomes retain their original cause and details.

The declarations and reviewed dynamic exceptions live in
[the source test](tests/test_local_cause_codes.py). It scans src for literal
codes in Refused/Failed calls. A new local cause needs a meaning and must not
be a synonym of a shared cause.

| Extra cause | Meaning |
|---|---|
| worker_failed | an unexpected local worker operation failed; consult its log |
| worker_interrupted | the worker connection ended after dispatch; inspect retained work |
| authority_failed | an unexpected local authority operation failed; consult its log |
| registration_failed | an unexpected local Pirate registration operation failed; consult its log |

A lost Pirate connection uses failed/unavailable with unknown effects. A typed
Pirate failure keeps its own cause; the controller does not invent pirate_failed.
Service protocol/unstructured tool errors use Common's upstream_error, preserving
service_code or the service text. Body parsing and local carrier wrappers forward
their caller-owned causes.
