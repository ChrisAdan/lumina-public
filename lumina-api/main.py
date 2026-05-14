from contextlib import asynccontextmanager

from fastapi import FastAPI
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# ── Routers ───────────────────────────────────────────────────────────────────
from routers.health         import router as health_router
from routers.search         import router as search_router
from routers.weather        import router as weather_router
from routers.briefing       import router as briefing_router
from routers.library        import router as library_router
from routers.openai_compat  import router as openai_compat_router
from routers.people         import router as people_router
from routers.calendar       import router as calendar_router
from routers.reminders      import router as reminders_router
from routers.gmail          import router as gmail_router
from routers.observability  import router as observability_router

# Middleware
from middleware import RequestIDMiddleware, AuditMiddleware

# Scheduler
from scheduler import start_scheduler, stop_scheduler

# Tool registry (postgres-backed; idempotent on startup)
from inference.tool_registry import register_tools, embed_tools as _embed_tools

# Semantic tool selector — ChromaDB index built from TOOLS at startup
from inference.tool_selector import build_index as _build_tool_index
from services.tools import TOOLS as _all_tools

# DB
from db.postgres import SessionLocal


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db_session_factory = SessionLocal
    register_tools()
    _embed_tools(_all_tools)
    _build_tool_index(_all_tools)
    start_scheduler()
    yield
    stop_scheduler()


# ── Rate limiter ──────────────────────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address)

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Lumina Agent API",
    version="1.0.0",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Middleware — RequestID first so AuditMiddleware can read the header
app.add_middleware(RequestIDMiddleware)
app.add_middleware(AuditMiddleware)

app.include_router(health_router)
app.include_router(search_router)
app.include_router(weather_router)
app.include_router(briefing_router)
app.include_router(library_router)
app.include_router(openai_compat_router, prefix="/v1")
app.include_router(people_router)
app.include_router(calendar_router)
app.include_router(reminders_router)
app.include_router(gmail_router)
app.include_router(observability_router)
