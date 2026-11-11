"""SQLite storage for skein.

This module only reads and writes rows. It holds no business rules: who may do
what, which status changes are allowed and every limit live in ``service.py``.

Usage::

    store = Store("./skein.db")          # creates the file and schema if needed
    with store.tx() as db:               # one connection, one transaction
        db.get_task("t_0123456789")

Each ``tx()`` opens its own connection and closes it afterwards, so a Store is
safe to share across threads and processes. Write transactions start with
``BEGIN IMMEDIATE`` so a read-check-write in the service cannot interleave with
another writer. Times are unix floats.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable, Iterator

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS members(
    name TEXT PRIMARY KEY,
    display TEXT NOT NULL,
    token_sha256 TEXT UNIQUE NOT NULL,
    created REAL NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0,
    last_seen REAL
);
CREATE TABLE IF NOT EXISTS meta(
    k TEXT PRIMARY KEY,
    v TEXT
);
CREATE TABLE IF NOT EXISTS tasks(
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    assignee TEXT NOT NULL,
    created_by TEXT NOT NULL,
    status TEXT NOT NULL,
    created REAL,
    claimed REAL,
    updated REAL,
    finished REAL,
    result TEXT,
    notes TEXT NOT NULL DEFAULT '[]',
    seen_by_point INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS asks(
    id TEXT PRIMARY KEY,
    asker TEXT NOT NULL,
    question TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    created REAL,
    answered REAL,
    answer TEXT,
    answered_by TEXT,
    seen_by_asker INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS tasks_assignee_status ON tasks(assignee, status);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS asks_status ON asks(status);
CREATE INDEX IF NOT EXISTS asks_asker_status ON asks(asker, status);
"""

_MEMBER_COLS = ("name", "display", "token_sha256", "created", "disabled", "last_seen")
_MEMBER_UPDATABLE = frozenset({"display", "token_sha256", "disabled", "last_seen"})

_TASK_COLS = (
    "id", "title", "body", "assignee", "created_by", "status", "created", "claimed",
    "updated", "finished", "result", "notes", "seen_by_point",
)
_TASK_UPDATABLE = frozenset(_TASK_COLS) - {"id"}

_ASK_COLS = (
    "id", "asker", "question", "context", "task_id", "status", "created", "answered",
    "answer", "answered_by", "seen_by_asker",
)
_ASK_UPDATABLE = frozenset(_ASK_COLS) - {"id"}

_ORDER_COLS = frozenset({"created", "updated", "finished"})


class Store:
    """A skein database file. Construction creates the schema if it is missing."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = os.fspath(path)
        if self.path == ":memory:" or self.path.startswith("file:"):
            raise ValueError("skein needs a database file path (one connection per operation)")
        self.init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        """Create the database file and schema if needed. Safe to call repeatedly."""
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"{self.path} has schema version {version}; this skein understands "
                    f"up to {SCHEMA_VERSION}. Upgrade skein."
                )
            conn.execute("BEGIN IMMEDIATE")
            try:
                for stmt in _SCHEMA.split(";"):
                    if stmt.strip():
                        conn.execute(stmt)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    @contextmanager
    def tx(self, write: bool = True) -> Iterator["Tx"]:
        """One connection and one transaction; committed on success, rolled back on error."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield Tx(conn)
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


def _task_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    d = _row(row)
    if d is not None:
        d["notes"] = json.loads(d["notes"] or "[]")
    return d


def _check_cols(fields: dict[str, Any], allowed: frozenset[str]) -> None:
    bad = set(fields) - allowed
    if bad:
        raise KeyError(f"not updatable: {sorted(bad)}")


def _in(col: str, values: Iterable[str]) -> tuple[str, list[str]]:
    vals = list(values)
    return f"{col} IN ({','.join('?' * len(vals))})", vals


class Tx:
    """Row operations on one open connection inside a transaction."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -- members ---------------------------------------------------------------

    def insert_member(self, name: str, display: str, token_sha256: str, created: float) -> None:
        self.conn.execute(
            "INSERT INTO members(name, display, token_sha256, created, disabled) "
            "VALUES (?, ?, ?, ?, 0)",
            (name, display, token_sha256, created),
        )

    def get_member(self, name: str) -> dict[str, Any] | None:
        return _row(self.conn.execute("SELECT * FROM members WHERE name = ?", (name,)).fetchone())

    def get_member_by_token_hash(self, token_sha256: str) -> dict[str, Any] | None:
        return _row(
            self.conn.execute(
                "SELECT * FROM members WHERE token_sha256 = ?", (token_sha256,)
            ).fetchone()
        )

    def list_members(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM members ORDER BY created, rowid").fetchall()
        return [dict(r) for r in rows]

    def update_member(self, name: str, **fields: Any) -> bool:
        _check_cols(fields, _MEMBER_UPDATABLE)
        if not fields:
            return False
        sets = ", ".join(f"{k} = ?" for k in fields)
        cur = self.conn.execute(
            f"UPDATE members SET {sets} WHERE name = ?", (*fields.values(), name)
        )
        return cur.rowcount > 0

    # -- meta ------------------------------------------------------------------

    def get_meta(self, k: str) -> str | None:
        row = self.conn.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
        return None if row is None else row[0]

    def set_meta(self, k: str, v: str | None) -> None:
        self.conn.execute(
            "INSERT INTO meta(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (k, v),
        )

    def delete_meta(self, k: str) -> None:
        self.conn.execute("DELETE FROM meta WHERE k = ?", (k,))

    # -- tasks -----------------------------------------------------------------

    def insert_task(self, task: dict[str, Any]) -> None:
        row = dict(task)
        row["notes"] = json.dumps(row.get("notes") or [])
        row.setdefault("seen_by_point", 1)
        cols = [c for c in _TASK_COLS if c in row]
        self.conn.execute(
            f"INSERT INTO tasks({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [row[c] for c in cols],
        )

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return _task_row(self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())

    def update_task(self, task_id: str, **fields: Any) -> bool:
        _check_cols(fields, _TASK_UPDATABLE)
        if not fields:
            return False
        if "notes" in fields:
            fields["notes"] = json.dumps(fields["notes"])
        sets = ", ".join(f"{k} = ?" for k in fields)
        cur = self.conn.execute(
            f"UPDATE tasks SET {sets} WHERE id = ?", (*fields.values(), task_id)
        )
        return cur.rowcount > 0

    def list_tasks(
        self,
        *,
        statuses: Iterable[str] | None = None,
        assignee: str | None = None,
        seen_by_point: int | None = None,
        order_by: str = "created",
        newest_first: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        where, args = self._task_where(statuses, assignee, seen_by_point)
        if order_by not in _ORDER_COLS:
            raise KeyError(order_by)
        direction = "DESC" if newest_first else "ASC"
        sql = f"SELECT * FROM tasks{where} ORDER BY {order_by} {direction}, rowid {direction}"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(int(limit))
        return [_task_row(r) for r in self.conn.execute(sql, args).fetchall()]  # type: ignore[misc]

    def count_tasks(
        self, *, statuses: Iterable[str] | None = None, assignee: str | None = None
    ) -> int:
        where, args = self._task_where(statuses, assignee, None)
        return self.conn.execute(f"SELECT COUNT(*) FROM tasks{where}", args).fetchone()[0]

    def count_tasks_by_assignee(self, statuses: Iterable[str]) -> dict[str, int]:
        clause, args = _in("status", statuses)
        rows = self.conn.execute(
            f"SELECT assignee, COUNT(*) FROM tasks WHERE {clause} GROUP BY assignee", args
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    @staticmethod
    def _task_where(
        statuses: Iterable[str] | None, assignee: str | None, seen_by_point: int | None
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        if statuses is not None:
            clause, vals = _in("status", statuses)
            clauses.append(clause)
            args.extend(vals)
        if assignee is not None:
            clauses.append("assignee = ?")
            args.append(assignee)
        if seen_by_point is not None:
            clauses.append("seen_by_point = ?")
            args.append(int(seen_by_point))
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), args

    # -- asks ------------------------------------------------------------------

    def insert_ask(self, ask: dict[str, Any]) -> None:
        cols = [c for c in _ASK_COLS if c in ask]
        self.conn.execute(
            f"INSERT INTO asks({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [ask[c] for c in cols],
        )

    def get_ask(self, ask_id: str) -> dict[str, Any] | None:
        return _row(self.conn.execute("SELECT * FROM asks WHERE id = ?", (ask_id,)).fetchone())

    def update_ask(self, ask_id: str, **fields: Any) -> bool:
        _check_cols(fields, _ASK_UPDATABLE)
        if not fields:
            return False
        sets = ", ".join(f"{k} = ?" for k in fields)
        cur = self.conn.execute(f"UPDATE asks SET {sets} WHERE id = ?", (*fields.values(), ask_id))
        return cur.rowcount > 0

    def list_asks(
        self,
        *,
        statuses: Iterable[str] | None = None,
        asker: str | None = None,
        seen_by_asker: int | None = None,
        newest_first: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        if statuses is not None:
            clause, vals = _in("status", statuses)
            clauses.append(clause)
            args.extend(vals)
        if asker is not None:
            clauses.append("asker = ?")
            args.append(asker)
        if seen_by_asker is not None:
            clauses.append("seen_by_asker = ?")
            args.append(int(seen_by_asker))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        direction = "DESC" if newest_first else "ASC"
        sql = f"SELECT * FROM asks{where} ORDER BY created {direction}, rowid {direction}"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(int(limit))
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def count_asks(self, *, statuses: Iterable[str] | None = None) -> int:
        if statuses is None:
            return self.conn.execute("SELECT COUNT(*) FROM asks").fetchone()[0]
        clause, args = _in("status", statuses)
        return self.conn.execute(f"SELECT COUNT(*) FROM asks WHERE {clause}", args).fetchone()[0]
