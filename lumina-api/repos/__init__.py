"""
Repository layer.

Every persistent write goes through one of these modules. Each function:
  - takes a validated Pydantic model in,
  - runs parameterized SQL on `db.postgres.engine`,
  - returns a Pydantic model out (or None / raises for not-found / conflict).

No SQLAlchemy ORM, no `session.add()` escape hatch — the type system
enforces "validated input only". This matters because Lumina writes on
the user's behalf; clean validation errors are how the LLM recovers, and
a single chokepoint is how we audit what was written and why.
"""
