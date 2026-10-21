"""OAuth store schema 5: the issuer's records and clients, and each service with its own heartbeat timeout.

A store of any other schema is refused, unchanged: this release migrates none.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from boring_crocodile.oauth_records import (SERVICE_COLUMNS, VERSION, add_service, initialize,
                                            service, service_ping_timeout, service_url, services)


class SchemaTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "oauth.sqlite3"

    def test_a_new_store_is_schema_5(self):
        with sqlite3.connect(self.path) as db:
            initialize(db)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(tuple(r[1] for r in db.execute("PRAGMA table_info(services)")), SERVICE_COLUMNS)
        self.assertEqual(VERSION, 5)

    def test_opening_again_changes_nothing(self):
        with sqlite3.connect(self.path) as db:
            initialize(db)
            add_service(db, "canvas", "http://127.0.0.1:18511/mcp", ["pond"])
        with sqlite3.connect(self.path) as db:
            initialize(db)
            self.assertEqual(service(db, name="canvas")["ponds"], ["pond"])
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 5)

    def test_another_schema_is_refused_unchanged(self):
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE records (kind TEXT, key TEXT)")
            db.execute("PRAGMA user_version=4")
        with sqlite3.connect(self.path) as db, self.assertRaisesRegex(ValueError, "schema 4; this release reads schema 5"):
            initialize(db)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)

    def test_a_service_has_its_own_ping_timeout(self):
        with sqlite3.connect(self.path) as db:
            initialize(db)
            added = add_service(db, "remote", "https://far.example/mcp", ["pond"], 2.5)
            self.assertEqual(added["ping_timeout"], 2.5)
            add_service(db, "local", "http://127.0.0.1:18512/mcp", ["pond"])
            self.assertEqual({s["name"]: s["ping_timeout"] for s in services(db)}, {"local": 1.0, "remote": 2.5})
            for bad in (0, -1, 2.6, 3, 3.0, 10, True, "2", float("nan"), float("inf")):
                with self.subTest(timeout=bad), self.assertRaises(ValueError):
                    add_service(db, f"bad{len(str(bad))}", "http://127.0.0.1:1/mcp", ["pond"], bad)
            self.assertEqual(len(services(db)), 2)
        self.assertEqual(service_ping_timeout(2), 2.0)
        self.assertEqual(service_ping_timeout(2.5), 2.5)

    def test_plain_http_only_on_this_host(self):
        for url in ("http://127.0.0.1:18511/mcp", "http://127.5.0.1/mcp", "http://[::1]:1/mcp",
                    "http://localhost:1/mcp", "https://far.example/mcp", "https://192.168.0.5/mcp"):
            with self.subTest(url=url):
                self.assertEqual(service_url(url), url)
        for url in ("http://192.168.0.5:18511/mcp", "http://far.example/mcp", "http://10.0.0.1/mcp",
                    "http://localhost.far.example/mcp"):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "https"):
                service_url(url)


if __name__ == "__main__":
    unittest.main()
