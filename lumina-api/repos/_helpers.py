"""
Shared helpers for repository modules.

Kept deliberately small. Add to this only when a pattern actually duplicates
across two or more repos — premature helpers make repo functions harder to
read by adding indirection without cutting work.
"""
from typing import Any

from pydantic import BaseModel


def extract_set_fields(payload: BaseModel) -> dict[str, Any]:
    """
    Convert a partial-update Pydantic payload into a `{column: value}` dict
    suitable for an UPDATE … SET clause.

    - `exclude_unset=True` so we don't overwrite columns the caller didn't touch.
    - `None` is filtered out so an explicit `field=None` from the LLM doesn't
      null-out an existing value. (If a future column needs explicit-null
      semantics, that field should accept a sentinel and bypass this helper.)
    """
    return {
        k: v
        for k, v in payload.model_dump(exclude_unset=True).items()
        if v is not None
    }


def build_set_clause(
    fields: dict[str, Any],
    *,
    extra_assignments: list[str] | None = None,
) -> str:
    """
    Build an UPDATE … SET clause from a dict of bindable fields.

    Returns: "col_a = :col_a, col_b = :col_b[, <extra>...]"

    extra_assignments is for raw fragments whose RHS isn't a bind param —
    e.g. ['completed_at = NOW()']. Use sparingly; anything user-provided
    must go through the bindable dict so it gets parameterised.
    """
    parts = [f"{k} = :{k}" for k in fields]
    if extra_assignments:
        parts.extend(extra_assignments)
    return ", ".join(parts)
