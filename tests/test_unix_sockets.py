"""Claiming and releasing a listener's socket path: check before unlink."""
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest

from boring_crocodile.sqlite_oauth_provider import pond_resources
from boring_crocodile.unix_sockets import bound_identity, clear_stale_socket, release_socket


class UnixSocketPathTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir="/tmp", prefix="sock-")
        self.path = Path(self.temporary.name) / "worker.sock"

    def tearDown(self):
        self.temporary.cleanup()

    def bind(self):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.path))
        return listener

    def test_a_stale_socket_is_removed_a_live_one_is_refused(self):
        live = self.bind()
        live.listen(1)
        with self.assertRaisesRegex(FileExistsError, "listener is active"):
            clear_stale_socket(self.path)
        self.assertTrue(self.path.exists())
        live.close()  # the socket file stays: nobody listens any more
        self.assertTrue(self.path.exists())
        clear_stale_socket(self.path)
        self.assertFalse(os.path.lexists(self.path))
        clear_stale_socket(self.path)  # nothing there: nothing to do

    def test_anything_else_at_the_path_is_kept(self):
        self.path.write_text("keep")
        with self.assertRaisesRegex(FileExistsError, "inspect before removing"):
            clear_stale_socket(self.path)
        self.assertEqual(self.path.read_text(), "keep")

    def test_release_removes_only_the_socket_this_listener_bound(self):
        first = self.bind()
        identity = bound_identity(self.path)
        first.close()
        self.path.unlink()
        time.sleep(0.05)  # later than the kernel's coarse change-time tick
        second = self.bind()  # a successor bound the same path meanwhile, perhaps with the same inode number
        release_socket(self.path, identity)
        self.assertTrue(self.path.exists())
        release_socket(self.path, bound_identity(self.path))
        self.assertFalse(self.path.exists())
        second.close()
        release_socket(self.path, identity)  # gone already: no error


class PondNameTest(unittest.TestCase):
    def test_resources_take_the_one_pond_name_rule(self):
        ponds = {name: None for name in ("pond", "rose-garden", "a1")}
        self.assertEqual(sorted(filter(None, pond_resources("https://example.test", ponds).values())),
                         ["a1", "pond", "rose-garden"])
        for name in ("1pond", "pond-", "a" * 25, "under_score"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "path segment"):
                pond_resources("https://example.test", {name: None})


if __name__ == "__main__":
    unittest.main()
