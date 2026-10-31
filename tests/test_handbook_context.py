import hashlib
import os
from unittest.mock import patch
import tempfile
import unittest
from pathlib import Path

from boring_crocodile.handbook_context import ContextBootstrap

HANDBOOK_DOCUMENTS = ("AGREEMENT.md", "BOUNDARIES.md", "POND_MAP.md")
PROJECT_DOCUMENTS = ("project/README.md", "project/STATUS.md")


class ContextBootstrapTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.handbook_root = root / "handbook"
        self.project_root = self.handbook_root / "project"
        self.outside = root / "outside.md"
        self.outside.write_text("outside")
        self.project_root.mkdir(parents=True)

        for name in HANDBOOK_DOCUMENTS:
            (self.handbook_root / name).write_text(
                f"handbook:{name}",
                encoding="utf-8",
            )
        for name in PROJECT_DOCUMENTS:
            (self.handbook_root / name).write_text(
                f"project:{name}",
                encoding="utf-8",
            )

        self.bootstrap = ContextBootstrap(
            self.handbook_root, HANDBOOK_DOCUMENTS, {"example": PROJECT_DOCUMENTS})

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_loads_the_allowlisted_handbook_documents(self) -> None:
        context = self.bootstrap.load()

        handbook = context["handbook"]
        self.assertEqual(
            {d["filename"] for d in handbook["documents"]},
            set(HANDBOOK_DOCUMENTS),
        )
        self.assertIsNone(context["project"])

    def test_loads_the_selected_project_documents(self) -> None:
        context = self.bootstrap.load(project="example")

        project = context["project"]
        self.assertEqual(project["name"], "example")
        self.assertEqual(
            {d["filename"] for d in project["documents"]},
            set(PROJECT_DOCUMENTS),
        )

    def test_order_and_content_revision(self):
        bootstrap = ContextBootstrap(self.handbook_root, ("BOUNDARIES.md", "POND_MAP.md"))
        first = bootstrap.load()
        self.assertEqual(first["status"], "loaded")
        self.assertEqual([d["filename"] for d in first["handbook"]["documents"]],
                         ["BOUNDARIES.md", "POND_MAP.md"])
        self.assertIn("not orders", first["welcome"])
        (self.handbook_root / "POND_MAP.md").write_text("changed")
        self.assertNotEqual(first["handbook"]["content_revision"],
                            bootstrap.load()["handbook"]["content_revision"])

    def test_rejects_traversal_and_symlink_escape(self):
        (self.handbook_root / "escape.md").symlink_to(self.outside)
        for name in ("../outside.md", str(self.outside), "escape.md"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                ContextBootstrap(self.handbook_root, (name,)).load()

    def test_configured_project_order(self):
        context = ContextBootstrap(self.handbook_root, HANDBOOK_DOCUMENTS,
                                   {"example": PROJECT_DOCUMENTS[::-1]}).load("example")
        self.assertEqual([d["filename"] for d in context["project"]["documents"]],
                         list(PROJECT_DOCUMENTS[::-1]))

    def test_published_context_loads_without_git(self):
        expected = self.bootstrap.load("example")
        empty_path = Path(self.temporary_directory.name) / "no-executables"
        empty_path.mkdir()
        with patch.dict(os.environ, {"PATH": str(empty_path)}):
            actual = self.bootstrap.load("example")
        for key in ("handbook", "project"):
            self.assertIsNone(actual[key]["revision"])
            self.assertEqual(actual[key]["documents"], expected[key]["documents"])
            self.assertEqual(actual[key]["content_revision"],
                             expected[key]["content_revision"])

    def test_welcome_file_is_live_and_missing_or_escaping_fails(self):
        welcome = self.handbook_root / "WELCOME.md"
        welcome.write_text("first welcome")
        context = ContextBootstrap(self.handbook_root, HANDBOOK_DOCUMENTS, welcome_file="WELCOME.md")
        first = context.load()
        self.assertEqual(first["welcome"], "first welcome")
        welcome.write_text("revised welcome")
        self.assertEqual(context.load()["welcome"], "revised welcome")
        welcome.unlink()
        with self.assertRaises(FileNotFoundError):
            context.load()
        welcome.symlink_to(self.outside)
        with self.assertRaises(ValueError):
            context.load()

    def test_welcome_is_returned_once_even_if_in_document_list(self):
        (self.handbook_root / "WELCOME.md").write_text("one welcome")
        for files in (("WELCOME.md",), ("WELCOME.md", *HANDBOOK_DOCUMENTS)):
            with self.subTest(files=files):
                loaded = ContextBootstrap(self.handbook_root, files,
                                          welcome_file="WELCOME.md").load()
                self.assertEqual(loaded["welcome"], "one welcome")
                docs = loaded["handbook"]["documents"]
                self.assertNotIn("WELCOME.md", [doc["filename"] for doc in docs])
                manifest = "\n".join(doc["filename"] + ":" + doc["sha256"] for doc in docs)
                self.assertEqual(loaded["handbook"]["content_revision"],
                                 "sha256:" + hashlib.sha256(manifest.encode()).hexdigest())
                self.assertEqual(loaded["welcome_document"]["sha256"],
                                 hashlib.sha256(b"one welcome").hexdigest())

    def test_fifo_and_oversized_context_fail_visibly(self):
        from boring_worker_kit.framing import MAX_FRAME_BYTES
        path = self.handbook_root / "mutable.md"
        os.mkfifo(path)
        loader = ContextBootstrap(self.handbook_root, ("mutable.md",))
        with self.assertRaisesRegex(ValueError, "regular file"):
            loader.load()
        path.unlink()
        path.write_bytes(b"x" * (MAX_FRAME_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "transport limit"):
            loader.load()

    def test_worker_without_selected_context_can_still_be_constructed(self):
        from types import SimpleNamespace
        self.assertIsNone(ContextBootstrap.for_environment(SimpleNamespace(ponds={}), "other"))

    def test_rejects_an_unknown_project(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Unknown project 'unknown'. Known projects: example",
        ):
            self.bootstrap.load(project="unknown")

    def test_missing_maintained_document_fails_visibly(self) -> None:
        (self.handbook_root / "BOUNDARIES.md").unlink()

        with self.assertRaises(FileNotFoundError):
            self.bootstrap.load()


if __name__ == "__main__":
    unittest.main()

class SharedGuidePackageTest(unittest.TestCase):
    def test_packaged_guide_is_its_own_hashed_document(self):
        from boring_crocodile.shared_guide import load_shared_guide
        guide = load_shared_guide()
        self.assertEqual(guide["version"], "23")
        self.assertIn("Where were we?", guide["content"])
        self.assertEqual(guide["sha256"],
                         hashlib.sha256(guide["content"].encode("utf-8")).hexdigest())
