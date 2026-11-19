"""The artifact must read the runtime owner's OAuth schema without importing it."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

from boring_crocodile.oauth_records import VERSION

ROOT = Path(__file__).resolve().parents[1]


def test_artifact_schema_follows_the_runtime_owner(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "_develop", SimpleNamespace(load=lambda: (object(), object())))
    spec = importlib.util.spec_from_file_location("build_schema_fixture", ROOT / "scripts/build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.OAUTH_SCHEMA == VERSION
    assert module.OAUTH_UPGRADES_FROM == 5
    owner = tmp_path / "src/boring_crocodile"
    owner.mkdir(parents=True)
    (owner / "oauth_records.py").write_text("VERSION = 19\nUPGRADES_FROM = 18\n")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    assert module.oauth_schema() == 19
    assert module.oauth_schema("UPGRADES_FROM") == 18
