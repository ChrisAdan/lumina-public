"""
Tool registry for Lumina's LLM agent loop.

Each tool is a self-contained spec: name, description, JSON-schema parameters,
and an async handler. The OpenAI-compatible proxy (`routers/openai_compat.py`)
exposes `openai_schemas()` to the model and routes `tool_calls` back through
`execute()`.

Forward-compat note: this shape is intentionally close to LangChain's
`StructuredTool` (name / description / args_schema / coroutine). When we
migrate to LangChain, each `ToolSpec` becomes a `StructuredTool.from_function`
call and the loop in `openai_compat.py` becomes `AgentExecutor.ainvoke`.
"""
from __future__ import annotations

from typing import Any

from services.tool_cache import get_cache as _get_cache
from services.tools._base import Handler, ToolSpec
from services.tools.weather import TOOLS as _weather_tools
from services.tools.search import TOOLS as _search_tools
from services.tools.documents import TOOLS as _document_tools
from services.tools.recipes import TOOLS as _recipe_tools
from services.tools.github import TOOLS as _github_tools
from services.tools.calendar_tools import TOOLS as _calendar_tools
from services.tools.people import TOOLS as _people_tools
from services.tools.movies import TOOLS as _movie_tools
from services.tools.files import TOOLS as _file_tools
from services.tools.code import TOOLS as _code_tools
from services.tools.groceries import TOOLS as _grocery_tools
from services.tools.reminders import TOOLS as _reminder_tools
from services.tools.fitness import TOOLS as _fitness_tools
from services.tools.plants import TOOLS as _plant_tools
from services.tools.gmail_tools import TOOLS as _gmail_tools
from services.tools.system import TOOLS as _system_tools
from services.tools.market import TOOLS as _market_tools
from services.tools.trading import TOOLS as _trading_tools

TOOLS: list[ToolSpec] = (
    _weather_tools
    + _search_tools
    + _document_tools
    + _recipe_tools
    + _github_tools
    + _calendar_tools
    + _people_tools
    + _movie_tools
    + _file_tools
    + _code_tools
    + _grocery_tools
    + _reminder_tools
    + _fitness_tools
    + _plant_tools
    + _gmail_tools
    + _system_tools
    + _market_tools
    + _trading_tools
)

_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in TOOLS}


def openai_schemas(names: list[str] | None = None) -> list[dict]:
    """Return the tool list in OpenAI `tools` parameter format.

    If names is given, only include tools whose name is in the set (order
    preserved from TOOLS registration). Pass None to include all tools.
    """
    pool = TOOLS if names is None else [t for t in TOOLS if t.name in set(names)]
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in pool
    ]


def _coerce_args(spec: ToolSpec, args: dict) -> dict:
    """Coerce stringly-typed args into the schema's declared types.

    LLMs frequently emit `"n": "10"` instead of `"n": 10` even when the schema
    says integer — coerce here so handlers can trust their argument types.
    """
    props = (spec.parameters or {}).get("properties", {}) or {}
    out: dict = {}
    for k, v in (args or {}).items():
        target = (props.get(k) or {}).get("type")
        if target == "integer" and isinstance(v, str):
            try:
                v = int(v)
            except ValueError:
                pass
        elif target == "number" and isinstance(v, str):
            try:
                v = float(v)
            except ValueError:
                pass
        elif target == "boolean" and isinstance(v, str):
            v = v.strip().lower() in ("true", "1", "yes")
        elif target == "string" and isinstance(v, str):
            # Models sometimes leak the surrounding JSON quotes into the value
            v = v.strip().strip('"').strip("'")
        out[k] = v
    return out


async def execute(name: str, args: dict) -> Any:
    """Dispatch a tool call. Errors are returned as JSON, never raised — so the
    model can read them and recover instead of the whole turn 500-ing.

    Read-only tools with a TTL entry in tool_cache are served from cache when
    a fresh result exists; writes and uncached tools always hit the handler.
    """
    spec = _BY_NAME.get(name)
    if spec is None:
        return {"error": f"unknown tool: {name}"}

    cache = _get_cache()
    coerced = _coerce_args(spec, args)

    hit, cached_result = cache.get(name, coerced)
    if hit:
        return cached_result

    try:
        result = await spec.handler(**coerced)
    except TypeError as e:
        return {"error": f"bad arguments for {name}: {e}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

    cache.set(name, coerced, result)
    return result
