"""Compatibilidade SQLite -> PostgreSQL/Supabase para o Entregas Automáticas • LinkRoubadão."""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

import psycopg

DATABASE_URL = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL") or ""
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False
SERIAL_TABLES = {"products", "panels", "orders", "reviews"}


class CompatRow(dict):
    """Linha acessível por nome e também por índice, como sqlite3.Row."""

    def __init__(self, columns: list[str], values: Iterable[Any]):
        self._columns = columns
        self._values = tuple(values)
        super().__init__(zip(columns, self._values))

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


class CompatCursor:
    def __init__(self, connection: "CompatConnection"):
        self.connection = connection
        self._cursor = connection._conn.cursor()
        self.lastrowid: Optional[int] = None
        self._buffer: list[CompatRow] = []
        self._buffer_index = 0

    def _set_buffer_from_cursor(self) -> None:
        self._buffer = []
        self._buffer_index = 0
        if self._cursor.description:
            cols = [d.name for d in self._cursor.description]
            self._buffer = [CompatRow(cols, row) for row in self._cursor.fetchall()]

    def execute(self, sql: str, params: Iterable[Any] | None = None) -> "CompatCursor":
        params = tuple(params or ())
        original = sql.strip()
        upper = original.upper()
        self.lastrowid = None
        self._buffer = []
        self._buffer_index = 0

        # SQLite PRAGMAs e ALTERs antigos não são necessários no Postgres.
        if upper.startswith("PRAGMA "):
            if "TABLE_INFO" in upper:
                table = re.search(r"table_info\(([^)]+)\)", original, re.I)
                table_name = table.group(1).strip(" '\"") if table else ""
                q = """
                    SELECT ordinal_position - 1 AS cid, column_name AS name,
                           data_type AS type, CASE WHEN is_nullable='NO' THEN 1 ELSE 0 END AS notnull,
                           column_default AS dflt_value, 0 AS pk
                    FROM information_schema.columns
                    WHERE table_schema='public' AND table_name=%s
                    ORDER BY ordinal_position
                """
                self._cursor.execute(q, (table_name,))
                self._set_buffer_from_cursor()
            return self

        if upper.startswith("CREATE TABLE IF NOT EXISTS") or upper.startswith(
            "ALTER TABLE"
        ):
            # O esquema completo é administrado por supabase_schema.sql.
            return self

        if upper.startswith("SELECT LAST_INSERT_ROWID()"):
            self._buffer = [
                CompatRow(["last_insert_rowid"], [self.connection._lastrowid])
            ]
            return self

        translated = translate_sql(original)
        insert_match = re.match(
            r"\s*INSERT\s+(?:OR\s+IGNORE\s+)?INTO\s+([\w\"]+)", original, re.I
        )
        table = insert_match.group(1).strip('"').lower() if insert_match else None
        wants_id = table in SERIAL_TABLES and " RETURNING " not in translated.upper()
        if wants_id:
            translated = translated.rstrip().rstrip(";") + " RETURNING id"

        self._cursor.execute(translated, params)
        if wants_id:
            row = self._cursor.fetchone()
            if row:
                self.lastrowid = int(row[0])
                self.connection._lastrowid = self.lastrowid
        elif self._cursor.description:
            self._set_buffer_from_cursor()
        return self

    def executemany(
        self, sql: str, params_seq: Iterable[Iterable[Any]]
    ) -> "CompatCursor":
        translated = translate_sql(sql)
        self._cursor.executemany(translated, params_seq)
        return self

    def fetchone(self) -> Optional[CompatRow]:
        if self._buffer_index >= len(self._buffer):
            return None
        row = self._buffer[self._buffer_index]
        self._buffer_index += 1
        return row

    def fetchall(self) -> list[CompatRow]:
        if self._buffer_index == 0:
            self._buffer_index = len(self._buffer)
            return list(self._buffer)
        rows = self._buffer[self._buffer_index :]
        self._buffer_index = len(self._buffer)
        return rows

    def __iter__(self) -> Iterator[CompatRow]:
        return iter(self.fetchall())

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount


class CompatConnection:
    def __init__(self):
        if not DATABASE_URL:
            raise RuntimeError(
                "SUPABASE_DB_URL não configurada. Coloque a URI do Session Pooler nos Secrets do Replit."
            )
        self._conn = psycopg.connect(DATABASE_URL, connect_timeout=15, autocommit=False)
        self._lastrowid: Optional[int] = None

    def cursor(self) -> CompatCursor:
        return CompatCursor(self)

    def execute(self, sql: str, params: Iterable[Any] | None = None) -> CompatCursor:
        return self.cursor().execute(sql, params)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "CompatConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type:
            self.rollback()
        else:
            self.commit()
        self.close()


def translate_sql(sql: str) -> str:
    out = sql
    out = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", out, flags=re.I)
    if (
        re.search(r"\bINSERT\s+INTO\b", out, re.I)
        and "OR IGNORE" in sql.upper()
        and "ON CONFLICT" not in out.upper()
    ):
        out = out.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    out = out.replace("?", "%s")
    out = re.sub(r"date\('now'\)", "CURRENT_DATE", out, flags=re.I)
    out = re.sub(r"date\(COALESCE\(([^)]+)\)\)", r"DATE(COALESCE(\1))", out, flags=re.I)
    out = re.sub(
        r"strftime\('%Y-%m',\s*COALESCE\(([^)]+)\)\)\s*=\s*strftime\('%Y-%m','now'\)",
        r"TO_CHAR(COALESCE(\1), 'YYYY-MM') = TO_CHAR(NOW(), 'YYYY-MM')",
        out,
        flags=re.I,
    )
    return out


def ensure_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        schema_path = Path(__file__).with_name("supabase_schema.sql")
        if not schema_path.exists():
            raise RuntimeError("Arquivo supabase_schema.sql não encontrado.")
        schema_sql = schema_path.read_text(encoding="utf-8")
        conn = CompatConnection()
        try:
            for statement in schema_sql.split(";"):
                statement = statement.strip()
                if statement:
                    conn._conn.execute(statement)
            conn.commit()
            _SCHEMA_READY = True
        finally:
            conn.close()


def db() -> CompatConnection:
    ensure_schema()
    return CompatConnection()
