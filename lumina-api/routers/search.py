"""
routers/search.py  (replaces original)
SearXNG proxy with:
  - Result truncation for LLM context efficiency
  - Rate limiting via slowapi (10 req/min per IP)
  - ?raw=true escape hatch for debugging

Rate limiter is initialized in main.py and passed via app.state.limiter.
Add to main.py:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.util import get_remote_address
    from slowapi.errors import RateLimitExceeded

    limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
"""
import httpx
from fastapi import APIRouter, Query, Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from configs.app import SEARXNG_URL

router = APIRouter(prefix="/search", tags=["search"])

# Local limiter reference — the actual limiter lives on app.state
limiter = Limiter(key_func=get_remote_address)

# Truncation settings
MAX_RESULTS      = 4
MAX_TITLE_CHARS  = 80
MAX_SNIPPET_CHARS = 200
MAX_URL_CHARS    = 100


def _truncate(text: str | None, limit: int) -> str:
    if not text:
        return ""
    text = text.strip()
    return text[:limit] + "…" if len(text) > limit else text


def _trim_results(raw: dict) -> dict:
    """
    Distill raw SearXNG JSON to a compact list the model can reason about.
    Strips everything except title, url, snippet. Caps at MAX_RESULTS.
    """
    results = raw.get("results", [])[:MAX_RESULTS]
    trimmed = [
        {
            "title":   _truncate(r.get("title"),   MAX_TITLE_CHARS),
            "url":     _truncate(r.get("url"),      MAX_URL_CHARS),
            "snippet": _truncate(r.get("content"),  MAX_SNIPPET_CHARS),
        }
        for r in results
    ]
    return {
        "query":        raw.get("query", ""),
        "result_count": len(trimmed),
        "results":      trimmed,
    }


@router.get("/")
@limiter.limit("10/minute")
async def search(
    request: Request,          # required by slowapi
    q: str = Query(..., description="Search query"),
    raw: bool = Query(default=False, description="Return full SearXNG payload (debug only)"),
):
    """
    Proxy a search query to SearXNG.
    Returns a trimmed payload by default (max 4 results, short snippets).
    Pass ?raw=true to get the full SearXNG response for debugging.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{SEARXNG_URL}/search",
                params={"q": q, "format": "json"},
            )
            resp.raise_for_status()
            payload = resp.json()

        return payload if raw else _trim_results(payload)

    except Exception as e:
        return {"error": str(e)}
