"""The site's one file writer and one lock, on disk."""
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from boring_file_core import StaleEdit

from boring_crocodile import site_layout
from boring_crocodile.site_layout import SiteLocked, site_lock, write_file


class WriteFileTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.path = self.folder / "config.toml"

    def tearDown(self):
        self.temporary.cleanup()

    def names(self):
        return sorted(p.name for p in self.folder.iterdir())

    def test_replace_keeps_the_mode_and_group_of_the_file(self):
        self.path.write_text("old\n")
        self.path.chmod(0o640)
        before = self.path.stat()
        write_file(self.path, "new\n")
        after = self.path.stat()
        self.assertEqual(self.path.read_bytes(), b"new\n")
        self.assertEqual((stat.S_IMODE(after.st_mode), after.st_uid, after.st_gid),
                         (0o640, before.st_uid, before.st_gid))
        self.assertNotEqual(after.st_ino, before.st_ino)  # a new inode, renamed into place
        self.assertEqual(self.names(), ["config.toml"])

    def test_an_explicit_mode_is_exact_whatever_the_umask(self):
        old = os.umask(0o077)
        try:
            write_file(self.path, b"\x00bytes", mode=0o644)
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)
        self.assertEqual(self.path.read_bytes(), b"\x00bytes")

    def test_create_never_replaces(self):
        self.path.write_text("kept\n")
        with self.assertRaises(StaleEdit):
            write_file(self.path, "other\n", mode=0o644, create=True)
        self.assertEqual(self.path.read_text(), "kept\n")
        self.assertEqual(self.names(), ["config.toml"])

    def test_keeping_needs_an_existing_regular_file(self):
        with self.assertRaisesRegex(OSError, "No such file"):
            write_file(self.path, "x")
        (self.folder / "target").write_text("target\n")
        self.path.symlink_to("target")
        with self.assertRaisesRegex(ValueError, "Cannot keep"):
            write_file(self.path, "x")
        self.assertEqual((self.folder / "target").read_text(), "target\n")

    def test_an_interrupted_write_leaves_the_old_file_whole(self):
        self.path.write_text("old\n")
        with patch.object(os, "fsync", side_effect=OSError("disk gone")):
            with self.assertRaisesRegex(OSError, "disk gone"):
                write_file(self.path, "new\n")
        self.assertEqual(self.path.read_text(), "old\n")
        self.assertEqual(self.names(), ["config.toml"])  # no temporary left


class SiteLockTest(unittest.TestCase):
    def test_a_second_holder_refuses_also_in_the_same_process(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            with site_lock(state, owner=os.getuid()):
                with self.assertRaisesRegex(SiteLocked, "Another change to the site"):
                    with site_lock(state, owner=os.getuid()):
                        pass
            with site_lock(state, owner=os.getuid()):  # released with the first
                pass
            lock = state / site_layout.SITE_LOCK
            self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
            lock.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "Unsafe site lock"):
                with site_lock(state, owner=os.getuid()):
                    pass


if __name__ == "__main__":
    unittest.main()
