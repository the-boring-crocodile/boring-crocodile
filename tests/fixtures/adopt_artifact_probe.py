"""Test-only bridge: run the real offline dry run against shared fake host facts."""
import json
from pathlib import Path
import runpy
import sys

release, root, facts_file = map(Path, sys.argv[1:])
sys.path.insert(0, str(release/"deploy.d"))
import boring_deploy
boring_deploy.verify_files(release)
sys.path[:0] = [str(p) for p in sorted((release/"wheels").glob("*.whl"))]
sys.path.append(str(Path(__file__).resolve().parents[1]))
from boring_crocodile import site_adopt
from boring_crocodile.site_model import Identity
from site_host import FakeHost
assert str(site_adopt.__file__).startswith(str(release/"wheels")), site_adopt.__file__
facts = json.loads(facts_file.read_text())
host = FakeHost(root, provisioned=False)
host.identities = {row["name"]: Identity.parse(row) for row in facts["identities"]}
host.extra_groups = facts["groups"]
host.owned = facts["owned"]
host.role_members = {key: set(value) for key,value in facts["members"].items()}
from boring_crocodile import site_adopt_proof
# This fixture tests verified wheel/planner loading, not native host acceptance.
site_adopt_proof.prove = lambda site, artifact_root: {"status": "ok", "fixture": "planner-only; native proof stubbed"}
original = site_adopt.plan
site_adopt.plan = lambda: original(paths=host.paths, host=host)
sys.argv = [str(release/"adopt-site.py"), "--dry-run"]
runpy.run_path(sys.argv[0], run_name="__main__")
