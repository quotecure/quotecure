"""Postgres connection layer that mimics the sqlite3 interface the app was
built against, so the ~300+ existing .execute() call sites (written with
'?' placeholders and sqlite3.Row-style access) don't need to change.
"""
import re
from functools import lru_cache

import psycopg2
import psycopg2.extensions

_PRAGMA_RE = re.compile(r"^\s*PRAGMA\s+table_info\((\w+)\)\s*;?\s*$", re.IGNORECASE)

_TABLE_INFO_SQL = """
    SELECT (c.ordinal_position - 1) AS cid,
           c.column_name AS name,
           c.data_type AS type,
           CASE WHEN c.is_nullable = 'NO' THEN 1 ELSE 0 END AS notnull,
           c.column_default AS dflt_value,
           CASE WHEN pk.column_name IS NOT NULL THEN 1 ELSE 0 END AS pk
    FROM information_schema.columns c
    LEFT JOIN (
        SELECT kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
        WHERE tc.table_name = %s AND tc.constraint_type = 'PRIMARY KEY'
    ) pk ON pk.column_name = c.column_name
    WHERE c.table_name = %s
    ORDER BY c.ordinal_position;
"""


@lru_cache(maxsize=1024)
def _translate_placeholders(query):
    """Translate '?' -> '%s', skipping '?' inside single-quoted string literals."""
    out = []
    in_str = False
    for ch in query:
        if ch == "'":
            in_str = not in_str
            out.append(ch)
        elif ch == '?' and not in_str:
            out.append('%s')
        else:
            out.append(ch)
    return ''.join(out)


class Row:
    """Mimics sqlite3.Row: supports row[0], row['col'], dict(row), iteration.
    Duplicate column names (e.g. from a JOIN) resolve to the first match,
    matching sqlite3.Row's behavior.
    """
    __slots__ = ('_values', '_index')

    def __init__(self, cols, values):
        self._values = values
        self._index = {}
        for i, name in enumerate(cols):
            self._index.setdefault(name, i)

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        return self._values[self._index[key]]

    def keys(self):
        return list(self._index.keys())

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __repr__(self):
        return f"<Row {dict(zip(self._index, self._values))}>"


class CursorWrapper:
    def __init__(self, cur):
        self._cur = cur

    def _wrap(self, row):
        if row is None:
            return None
        cols = [d[0] for d in self._cur.description]
        return Row(cols, list(row))

    def execute(self, query, params=()):
        m = _PRAGMA_RE.match(query)
        if m:
            table = m.group(1)
            self._cur.execute(_TABLE_INFO_SQL, (table, table))
        else:
            self._cur.execute(_translate_placeholders(query), params)
        return self

    def executemany(self, query, seq_of_params):
        self._cur.executemany(_translate_placeholders(query), seq_of_params)
        return self

    def fetchone(self):
        return self._wrap(self._cur.fetchone())

    def fetchall(self):
        return [self._wrap(r) for r in self._cur.fetchall()]

    def fetchmany(self, size=None):
        rows = self._cur.fetchmany(size) if size is not None else self._cur.fetchmany()
        return [self._wrap(r) for r in rows]

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def lastrowid(self):
        raise AttributeError(
            "lastrowid is not supported on Postgres; use SELECT lastval() instead"
        )

    def close(self):
        self._cur.close()


class Connection:
    def __init__(self, dsn):
        self._conn = psycopg2.connect(dsn)

    def execute(self, query, params=()):
        cur = CursorWrapper(self._conn.cursor())
        return cur.execute(query, params)

    def executemany(self, query, seq_of_params):
        cur = CursorWrapper(self._conn.cursor())
        return cur.executemany(query, seq_of_params)

    def cursor(self):
        return CursorWrapper(self._conn.cursor())

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()
