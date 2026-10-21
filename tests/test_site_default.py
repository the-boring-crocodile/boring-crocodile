"""site/default is the renderer's output for the default answers, unchanged.
Regenerate with: PYTHONPATH=src python3 site/default/generate.py"""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from boring_crocodile.settings import Settings

DEFAULT = Path(__file__).resolve().parent.parent / "site" / "default"


def load_generator():
    spec = importlib.util.spec_from_file_location("site_default_generate", DEFAULT / "generate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SiteDefaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.expected = load_generator().reference_files()

    def test_files_match_the_renderer(self) -> None:
        for relative, body in self.expected.items():
            with self.subTest(file=relative):
                self.assertEqual((DEFAULT / relative).read_text(encoding="utf-8"), body,
                                 "stale: run site/default/generate.py")

    def test_no_other_rendered_files(self) -> None:
        present = {str(p.relative_to(DEFAULT)) for top in ("etc", "two-ponds")
                   for p in (DEFAULT / top).rglob("*") if p.is_file()}
        self.assertEqual(present, set(self.expected))

    def test_config_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(self.expected["etc/boring-crocodile/config.toml"])
            config = Settings.load(path)
        self.assertEqual(sorted(config.ponds), ["pond"])
        self.assertEqual(config.ponds["pond"].welcome_file, "WELCOME.md")

    def test_two_pond_config_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(self.expected["two-ponds/etc/boring-crocodile/config.toml"])
            config = Settings.load(path)
        self.assertEqual(sorted(config.ponds), ["pond", "second"])


if __name__ == "__main__":
    unittest.main()
