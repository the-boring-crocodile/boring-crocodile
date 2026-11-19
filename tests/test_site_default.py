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

    def test_both_site_descriptions_load_from_all_their_files(self) -> None:
        for prefix, names in (("", ["pond"]), ("two-ponds/", ["pond", "second"])):
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory() as tmp:
                base = prefix + "etc/boring-crocodile/"
                for relative, body in self.expected.items():
                    if relative.startswith(base):
                        path = Path(tmp) / relative.removeprefix(base)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(body)
                config = Settings.load(Path(tmp) / "config.toml")
                self.assertEqual(sorted(config.ponds), names)
                self.assertEqual(config.ponds["pond"].welcome_file, "WELCOME.md")


if __name__ == "__main__":
    unittest.main()
