import re
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from boring_task_state import ReceiptStore, decode_metadata, decode_result

HEX32 = re.compile(r"[a-f0-9]{32}\Z")


def user_version(path: Path) -> int:
    db = sqlite3.connect(path)
    try:
        return db.execute("PRAGMA user_version").fetchone()[0]
    finally:
        db.close()


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "state.sqlite3"

    def tearDown(self):
        self.dir.cleanup()

    def test_create_mints_ids_and_update_get(self):
        store = ReceiptStore(self.path)
        a = store.create("running", {"timeout": 2})
        b = store.create("running")
        self.assertRegex(a[0], HEX32)
        self.assertRegex(b[0], HEX32)
        self.assertNotEqual(a[0], b[0])
        self.assertEqual(decode_metadata(a)["timeout"], 2)
        self.assertEqual(decode_metadata(b), {})
        self.assertIsNone(decode_result(a))
        store.update(a[0], state="completed", result={"answer": "ok"})
        row = store.get(a[0])
        self.assertEqual(row[1], "completed")
        self.assertEqual(decode_result(row), {"answer": "ok"})
        self.assertIsNone(store.get("0" * 32))
        with self.assertRaisesRegex(ValueError, "unknown"):
            store.update("0" * 32, state="completed")
        self.assertEqual({r[0] for r in store.list(10)}, {a[0], b[0]})
        with self.assertRaisesRegex(ValueError, "limit"):
            store.list(0)
        store.close()
        self.assertEqual(user_version(self.path), 1)

    def test_schema_has_no_key_digest_owner(self):
        store = ReceiptStore(self.path, "jobs")
        columns = [row[1] for row in store.db.execute("PRAGMA table_info(jobs)")]
        self.assertEqual(columns, ["id", "state", "created", "updated", "result", "metadata"])
        store.close()

    def test_invalid_table_name(self):
        with self.assertRaisesRegex(ValueError, "invalid receipt table"):
            ReceiptStore(self.path, "jobs; DROP")

    def test_count_mark_unfinished_and_prune(self):
        store = ReceiptStore(self.path)
        ids = {state: store.create(state)[0]
               for state in ("queued", "running", "stopping", "completed", "cancelling")}
        self.assertEqual(store.count(("queued", "running", "stopping")), 3)
        self.assertEqual(store.count(()), 0)
        store.mark_unfinished()
        for state in ("queued", "running", "stopping"):
            self.assertEqual(store.get(ids[state])[1], "interrupted")
        self.assertEqual(store.get(ids["completed"])[1], "completed")
        # 'cancelling' is a Pirate state; jobs never use it.
        self.assertEqual(store.get(ids["cancelling"])[1], "cancelling")
        store.db.execute("UPDATE receipts SET updated=? WHERE id=?",
                         (time.time() - 1000, ids["completed"]))
        store.prune(500, 1000)
        self.assertIsNone(store.get(ids["completed"]))
        store.prune(10**6, 1)
        self.assertEqual(store.count(("interrupted",)), 0)
        self.assertIsNotNone(store.get(ids["cancelling"]))
        store.close()

    def _legacy(self, table_sql: str, rows: list[tuple]) -> None:
        db = sqlite3.connect(self.path)
        db.execute(table_sql)
        for row in rows:
            db.execute(f"INSERT INTO jobs VALUES ({','.join('?' for _ in row)})", row)
        db.commit()
        db.close()

    def test_refuses_pre_030_schema_unchanged(self):
        # The one-off rebuild of key/digest/owner tables was removed in 0.3.2.
        for table_sql in ("""CREATE TABLE jobs (
            id TEXT PRIMARY KEY, key TEXT NOT NULL, digest TEXT NOT NULL,
            state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
            result TEXT, owner TEXT NOT NULL DEFAULT '',
            metadata TEXT NOT NULL DEFAULT '{}')""", """CREATE TABLE jobs (
            id TEXT PRIMARY KEY,key TEXT UNIQUE NOT NULL,digest TEXT NOT NULL,
            state TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,result TEXT)"""):
            self.path.unlink(missing_ok=True)
            self._legacy(table_sql, [])
            before = self.path.read_bytes()
            with self.assertRaisesRegex(ValueError, "unsupported schema.*open it once with 0.3.1"):
                ReceiptStore(self.path, "jobs")
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(user_version(self.path), 0)

    def test_future_schema_is_refused_without_modification(self):
        store = ReceiptStore(self.path, "jobs")
        store.create("queued")
        store.db.execute("PRAGMA user_version=2")
        store.close()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "unsupported receipt schema version"):
            ReceiptStore(self.path, "jobs")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(user_version(self.path), 2)

    def test_reopening_current_schema_is_a_no_op(self):
        store = ReceiptStore(self.path, "jobs")
        receipt = store.create("queued")
        store.close()
        store = ReceiptStore(self.path, "jobs")
        self.assertEqual(store.get(receipt[0]), receipt)
        store.close()

if __name__ == "__main__":
    unittest.main()
