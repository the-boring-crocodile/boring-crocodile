"""Small SQLite receipt store; execution owners define lifecycle semantics.

The store mints every receipt ID itself (``uuid4().hex``). Callers never
choose identifiers, so there is nothing to collide, deduplicate or match.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from pathlib import Path

TERMINAL_STATES = frozenset({"completed", "refused", "failed", "timed_out", "cancelled", "interrupted"})
UNFINISHED_STATES = ("queued", "running", "stopping")
SCHEMA_VERSION = 1
_NAME = re.compile(r"[a-z][a-z0-9_]{0,62}\Z")
_COLUMNS = "id,state,created,updated,result,metadata"


def _dump(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), allow_nan=False)


class ReceiptStore:
    """Persist opaque lifecycle receipts without owning their state machine.

    A row is ``(id, state, created, updated, result, metadata)``.
    """

    def __init__(self, path: Path, table: str = "receipts") -> None:
        if not _NAME.fullmatch(table):
            raise ValueError("invalid receipt table")
        self.table = table
        self.db = sqlite3.connect(path, isolation_level=None)
        try:
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise ValueError(f"unsupported receipt schema version {version}; database left unchanged")
            self.db.execute("PRAGMA busy_timeout=3000")
            columns = [row[1] for row in self.db.execute(f"PRAGMA table_info({table})")]
            if columns and columns != _COLUMNS.split(","):
                raise ValueError(f"receipt table {table!r} in {path} has an unsupported schema "
                                 f"(columns: {', '.join(columns)}); database left unchanged")
            self.db.execute(self._schema(table))
            self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        except BaseException:
            self.db.close()
            raise

    @staticmethod
    def _schema(table: str) -> str:
        return f"""CREATE TABLE IF NOT EXISTS {table} (
            id TEXT PRIMARY KEY, state TEXT NOT NULL,
            created REAL NOT NULL, updated REAL NOT NULL,
            result TEXT, metadata TEXT NOT NULL DEFAULT '{{}}')"""

    def close(self) -> None:
        self.db.close()

    def _select(self) -> str:
        return f"SELECT {_COLUMNS} FROM {self.table}"

    def get(self, receipt_id: str):
        return self.db.execute(self._select() + " WHERE id=?", (receipt_id,)).fetchone()

    def create(self, state: str, metadata: dict | None = None):
        receipt_id = uuid.uuid4().hex
        now = time.time()
        self.db.execute(
            f"INSERT INTO {self.table} ({_COLUMNS}) VALUES (?,?,?,?,NULL,?)",
            (receipt_id, state, now, now, _dump(metadata or {})),
        )
        return self.get(receipt_id)

    def update(self, receipt_id: str, *, state: str,
               result: dict | None = None, metadata: dict | None = None) -> None:
        fields = ["state=?", "updated=?", "result=?"]
        values: list[object] = [state, time.time(),
                                _dump(result) if result is not None else None]
        if metadata is not None:
            fields.append("metadata=?")
            values.append(_dump(metadata))
        values.append(receipt_id)
        changed = self.db.execute(
            f"UPDATE {self.table} SET {','.join(fields)} WHERE id=?", values).rowcount
        if changed != 1:
            raise ValueError("unknown receipt")

    def list(self, limit: int):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        return self.db.execute(
            self._select() + " ORDER BY created DESC,id DESC LIMIT ?", (limit,)).fetchall()

    def count(self, states) -> int:
        states = tuple(states)
        if not states:
            return 0
        marks = ",".join("?" for _ in states)
        return self.db.execute(
            f"SELECT count(*) FROM {self.table} WHERE state IN ({marks})", states).fetchone()[0]

    def mark_unfinished(self, state: str = "interrupted") -> None:
        marks = ",".join("?" for _ in UNFINISHED_STATES)
        self.db.execute(
            f"UPDATE {self.table} SET state=?,updated=? WHERE state IN ({marks})",
            (state, time.time(), *UNFINISHED_STATES),
        )

    def prune(self, retention_seconds: int, max_stored: int) -> None:
        marks = ",".join("?" for _ in TERMINAL_STATES)
        terminal = sorted(TERMINAL_STATES)
        self.db.execute(
            f"DELETE FROM {self.table} WHERE state IN ({marks}) AND updated < ?",
            (*terminal, time.time() - retention_seconds),
        )
        excess = self.db.execute(
            f"SELECT max(0,count(*)-?) FROM {self.table}", (max_stored,)).fetchone()[0]
        if excess:
            self.db.execute(
                f"DELETE FROM {self.table} WHERE id IN (SELECT id FROM {self.table} "
                f"WHERE state IN ({marks}) ORDER BY updated LIMIT ?)",
                (*terminal, excess),
            )


def decode_result(row) -> dict | None:
    return json.loads(row[4]) if row[4] is not None else None


def decode_metadata(row) -> dict:
    return json.loads(row[5])
