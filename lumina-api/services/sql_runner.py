"""SQL execution tools for Lumina.

Provides:
  get_schema() — introspects public schema via information_schema
  query_sql(query) — SELECT-only guard; caps at 100 rows
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from db.postgres import engine

log = logging.getLogger(__name__)

_SELECT_ONLY_ERROR = (
    "Only SELECT (and CTEs starting with WITH) are allowed without explicit user "
    "confirmation. Use write-safety rules for INSERT/UPDATE/DELETE."
)


def get_schema() -> dict:
    """Return {tables: [{name, columns: [{name, type}]}]} for the public schema."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT table_name, column_name, data_type "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' "
                "ORDER BY table_name, ordinal_position"
            )).mappings().all()
    except Exception as e:
        log.warning("get_schema failed: %s", e)
        return {"error": str(e)}

    tables: dict[str, list[dict]] = {}
    for row in rows:
        t = row["table_name"]
        if t not in tables:
            tables[t] = []
        tables[t].append({"name": row["column_name"], "type": row["data_type"]})

    return {"tables": [{"name": t, "columns": cols} for t, cols in tables.items()]}


def query_sql(query: str) -> dict:
    """Execute a read-only SQL query. Returns {row_count, capped, rows}."""
    q = (query or "").strip()
    if not q:
        return {"error": "query is empty"}

    first_keyword = q.split()[0].upper()
    if first_keyword not in ("SELECT", "WITH"):
        return {"error": _SELECT_ONLY_ERROR}

    try:
        with engine.connect() as conn:
            result = conn.execute(text(q))
            rows = result.mappings().fetchmany(100)
            row_list = [dict(r) for r in rows]
        return {
            "row_count": len(row_list),
            "capped": len(row_list) == 100,
            "rows": row_list,
        }
    except Exception as e:
        log.warning("query_sql error: %s", e)
        return {"error": str(e)}
