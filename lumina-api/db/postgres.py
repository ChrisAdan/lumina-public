import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

POSTGRES_URL = os.getenv(
    "POSTGRES_URL",
    "postgresql://lumina:changeme@postgres:5432/lumina"
)

engine = create_engine(POSTGRES_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Persistence pattern: Pydantic schemas (`schemas/`) at the boundary,
# repository functions (`repos/`) running parameterized SQL on `engine`.
# No declarative_base / ORM models. The Session below is kept only for
# legacy callers that still execute raw SQL via `db.execute(text(...))`;
# new code should import `engine` and the relevant repo module instead.


def get_db():
    """FastAPI dependency — yields a DB session and closes it after."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()