# site/default — declared and rendered references

`generate.py` writes one-Pond and two-Pond examples using the same typed
description and pure unit renderer as setup. Each has a main
`etc/boring-crocodile/config.toml` and `ponds/NAME/config.toml` declarations.
The unit and tmpfiles bytes are checked by `tests/test_site_default.py`.

UIDs/GIDs 901–903, UUIDs ending in 1/2, the example public URL and the 2 GiB
ceiling are illustrative. Provisioning plans actual free numeric identities and
creates UUIDs/secrets once; render neither creates accounts/secrets nor deletes data.
Use setup for an installation; do not copy these identity bindings onto a host.

Regenerate from this checkout with `PYTHONPATH=src python site/default/generate.py`
in the development environment. See [INSTALL](../../docs/INSTALL.md#reference-a-default-site).
