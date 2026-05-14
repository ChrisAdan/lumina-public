"""
Tool registry — postgres-backed source of truth for which capabilities Lumina
exposes to the LLM.

`register_tools()` upserts tool metadata to postgres (idempotent, called from
the FastAPI lifespan). `embed_tools()` follows it: each tool description is
hashed and compared against the stored `embed_hash` column; only changed or new
descriptions are re-embedded into the ChromaDB `tools` collection. The selector
(`inference.tool_selector`) queries that collection at request time instead of
running BM25 over the full description text.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Optional

import chromadb
from chromadb.config import Settings
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from configs.app import CHROMA_HOST, CHROMA_PORT
from db.postgres import engine

log = logging.getLogger(__name__)


class ToolRegistry:
    def register(
        self,
        tool_id: str,
        description: str,
        router: str,
        summary_url: Optional[str] = None,
        params: Optional[dict] = None,
        cost_class: str = "free",
    ) -> None:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO tools (id, router, summary_url, description, params, cost_class, enabled)
                    VALUES (:id, :router, :summary_url, :description, CAST(:params AS JSONB), :cost_class, TRUE)
                    ON CONFLICT (id) DO UPDATE SET
                        router      = EXCLUDED.router,
                        summary_url = EXCLUDED.summary_url,
                        description = EXCLUDED.description,
                        params      = EXCLUDED.params,
                        cost_class  = EXCLUDED.cost_class,
                        updated_at  = NOW()
                    """
                ),
                {
                    "id": tool_id,
                    "router": router,
                    "summary_url": summary_url,
                    "description": description,
                    "params": json.dumps(params or {}),
                    "cost_class": cost_class,
                },
            )

    def all_enabled(self) -> list[dict]:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, router, summary_url, description "
                    "FROM tools WHERE enabled ORDER BY id"
                )
            ).mappings().all()
        return [dict(r) for r in rows]


tool_registry = ToolRegistry()


def _register_tools_inner() -> None:
    tool_registry.register(
        tool_id="weather_forecast",
        description="Daily weather forecast (1-7 days) for home or any named location.",
        router="/weather/",
        summary_url="/weather/today?summary=true",
    )
    tool_registry.register(
        tool_id="web_search",
        description="Public web search via SearXNG. Returns title/url/snippet lists.",
        router="/search/",
        cost_class="external_api",
    )
    tool_registry.register(
        tool_id="fetch_url",
        description="Fetch a web page and return its visible text. Use after web_search.",
        router="/search/fetch",
        cost_class="external_api",
    )
    tool_registry.register(
        tool_id="github_list_repo",
        description=(
            "List files and directories in an EXTERNAL GitHub repository path (github.com URLs). "
            "For Lumina's own source code use file_list('/app') instead — not this tool."
        ),
        router="github://repo/list",
        cost_class="external_api",
        params={
            "capability": "read",
            "surface": "repo_tree"
        },
    )
    tool_registry.register(
        tool_id="github_read_file",
        description=(
            "Read a text file from an EXTERNAL GitHub repository (github.com URLs). "
            "For Lumina's own source code use file_read('/app/...') instead — not this tool."
        ),
        router="github://repo/read",
        cost_class="external_api",
        params={
            "capability": "read",
            "surface": "file_contents"
        },
    )
    tool_registry.register(
        tool_id="github_search_code",
        description=(
            "Search for code across an EXTERNAL GitHub repository (github.com) using keywords. "
            "For Lumina's own source code use file_list('/app') + file_read instead — not this tool."
        ),
        router="github://repo/search",
        cost_class="external_api",
        params={
            "capability": "read",
            "surface": "code_search"
        },
    )
    tool_registry.register(
        tool_id="github_tree_recursive",
        description=(
            "Get a recursive directory tree for an EXTERNAL GitHub repo (github.com URLs). "
            "For Lumina's own source code use file_list('/app') instead — not this tool."
        ),
        router="github://repo/tree",
        cost_class="external_api",
        params={"capability": "read", "surface": "tree"},
    )
    tool_registry.register(
        tool_id="github_read_url",
        description=(
            "Read or explore a github.com or raw.githubusercontent.com URL directly. "
            "Use whenever the user provides a GitHub URL."
        ),
        router="github://url",
        cost_class="external_api",
        params={"capability": "read", "surface": "url"},
    )
    tool_registry.register(
        tool_id="document_search",
        description=(
            "Semantic search over Lumina's local document library "
            "(manuals, reference docs, ingested PDFs/EPUBs). Returns excerpts with citations."
        ),
        router="/library/",
        summary_url="/library/list?summary=true",
    )
    tool_registry.register(
        tool_id="document_list",
        description=(
            "Catalog of every document currently in the library — id, title, "
            "page/chapter count, chunk count. Call when the user asks what's available."
        ),
        router="/library/list",
        summary_url="/library/list?summary=true",
    )
    tool_registry.register(
        tool_id="menu_search",
        description=(
            "One-shot menu builder: searches the household's favorite recipe sites "
            "(read from Lumina/Food/Recipes.md), ranks deterministically, "
            "extracts top recipes via Spoonacular, saves locally."
        ),
        router="/recipes/",
        cost_class="external_api",
    )
    tool_registry.register(
        tool_id="search_recipes",
        description=(
            "Search a live recipe database (Spoonacular) by cuisine, query, "
            "ingredients, diet, or meal type."
        ),
        router="/recipes/search",
        summary_url="/recipes/?summary=true",
        cost_class="external_api",
    )
    tool_registry.register(
        tool_id="recipes_from_fridge",
        description=(
            "Find recipes the user can make from a list of ingredients on hand."
        ),
        router="/recipes/from-fridge",
        cost_class="external_api",
    )
    tool_registry.register(
        tool_id="calendar_upcoming",
        description=(
            "List upcoming events from the user's synced Google calendars "
            "(read from a local cache refreshed hourly)."
        ),
        router="/calendar/upcoming",
    )
    tool_registry.register(
        tool_id="calendar_search",
        description=(
            "Substring search over cached Google calendar events "
            "(summary, location, description)."
        ),
        router="/calendar/search",
    )
    tool_registry.register(
        tool_id="people_contact_save",
        description=(
            "Save or update contact information (email, phone) for a known person in their synapse file. "
            "Use when the user says 'Sabrina's email is X' or 'save John's phone number'. "
            "Requires explicit confirmation before writing."
        ),
        router="/people/contact",
        cost_class="write",
    )
    tool_registry.register(
        tool_id="people_lookup",
        description=(
            "Look up a specific known person from the local people notes. "
            "Use when the user names someone directly and wants reminders, "
            "background, preferences, or household context about them."
        ),
        router="/people/query",
        summary_url="/people/list?summary=true",
    )
    tool_registry.register(
        tool_id="people_search",
        description=(
            "Semantic search across all local people notes. Use for broad "
            "questions about relationships, history, responsibilities, or "
            "who is connected to a topic."
        ),
        router="/people/query",
        summary_url="/people/list?summary=true",
    )
    tool_registry.register(
        tool_id="movies_find",
        description="Fuzzy-look-up a movie on the user's curated watchlist by title.",
        router="/movies/find",
    )
    tool_registry.register(
        tool_id="movies_rank",
        description="Rank the user's curated movie list by rating, popularity, year, or recently added.",
        router="/movies/rank",
    )
    tool_registry.register(
        tool_id="movies_search",
        description=(
            "Search the user's curated movie list or get TMDB recommendations seeded by it. "
            "scope='upstream' for 'find me something new to watch'."
        ),
        router="/movies/search",
    )
    tool_registry.register(
        tool_id="movies_preferences",
        description="Aggregate genre/decade/director signals from the user's curated movie list.",
        router="/movies/preferences",
    )
    tool_registry.register(
        tool_id="file_list",
        description=(
            "List .txt/.md/.json/.log files in the sandboxed file workspace."
        ),
        router="/app/files",
    )
    tool_registry.register(
        tool_id="file_read",
        description=(
            "Read a .txt/.md/.json/.log file from the sandboxed file workspace."
        ),
        router="/app/files",
    )
    tool_registry.register(
        tool_id="file_write",
        description=(
            "Write a .txt/.md/.json/.log file in the sandboxed file workspace. "
            "Requires user confirmation per AGENTS.md write-safety rules."
        ),
        router="/app/files",
    )
    tool_registry.register(
        tool_id="run_python",
        description=(
            "Execute Python code in a sandboxed container (no network, 15s timeout). "
            "Returns stdout, stderr, and exit_code. Use for data analysis, "
            "calculations, pandas operations, and script validation."
        ),
        router="python-runner://exec",
    )
    tool_registry.register(
        tool_id="get_schema",
        description=(
            "Introspect Lumina's Postgres public schema. Returns all tables with "
            "column names and data types. Call before writing any SQL query."
        ),
        router="postgres://schema",
    )
    tool_registry.register(
        tool_id="query_sql",
        description=(
            "Execute a SELECT query against Lumina's Postgres (100-row cap). "
            "SELECT and CTEs only — writes require explicit user confirmation."
        ),
        router="postgres://query",
    )
    tool_registry.register(
        tool_id="groceries_list",
        description="Return the current household shopping list (all pending grocery items).",
        router="/groceries/",
        summary_url="/groceries/?summary=true",
    )
    tool_registry.register(
        tool_id="groceries_add",
        description="Add an item to the household shopping list. Requires write confirmation.",
        router="/groceries/",
        cost_class="write",
    )
    tool_registry.register(
        tool_id="groceries_complete",
        description="Mark a grocery item as purchased by ID. Requires write confirmation.",
        router="/groceries/",
        cost_class="write",
    )
    tool_registry.register(
        tool_id="fitness_exercises",
        description=(
            "Search the exercise library by muscle group, equipment, category, or keyword. "
            "Returns exercise IDs, names, and animated GIF previews. "
            "Use for workout planning, exercise lookup, and training suggestions."
        ),
        router="/fitness/",
        cost_class="free",
    )
    tool_registry.register(
        tool_id="fitness_goals",
        description=(
            "List the user's active strength goals with target weight, reps, and deadlines."
        ),
        router="/fitness/",
        cost_class="free",
    )
    tool_registry.register(
        tool_id="fitness_goal_set",
        description=(
            "Create a new strength goal for an exercise. "
            "Requires explicit user confirmation before writing."
        ),
        router="/fitness/",
        cost_class="write",
    )
    tool_registry.register(
        tool_id="fitness_plan_save",
        description=(
            "Save a named workout plan with exercises, sets, and reps. "
            "Call fitness_exercises first to get exercise IDs. "
            "Requires explicit user confirmation before writing."
        ),
        router="/fitness/",
        cost_class="write",
    )

    tool_registry.register(
        tool_id="plants_due",
        description="List all household plants due or overdue for feeding today.",
        router="/plants/due-feeding",
        cost_class="free",
    )
    tool_registry.register(
        tool_id="plants_list",
        description="List all active household plants with location and next feeding date.",
        router="/plants/",
        cost_class="free",
    )
    tool_registry.register(
        tool_id="plant_fed",
        description="Record that a plant was fed today and set its next feeding date. Requires confirmation.",
        router="/plants/",
        cost_class="write",
    )
    tool_registry.register(
        tool_id="plant_add",
        description="Add a new plant to the household inventory. Requires confirmation.",
        router="/plants/",
        cost_class="write",
    )

    tool_registry.register(
        tool_id="reminder_list",
        description="List all active reminders (one-shot and recurring).",
        router="/reminders/",
        cost_class="free",
    )
    tool_registry.register(
        tool_id="reminder_set",
        description=(
            "Set a reminder. One-shot ('remind me to X in 30 minutes') or "
            "recurring ('remind me to drink water every day at 9am'). "
            "Sends a push notification via ntfy. Requires explicit confirmation."
        ),
        router="/reminders/",
        cost_class="write",
    )
    tool_registry.register(
        tool_id="reminder_cancel",
        description="Cancel an active reminder by ID. Requires explicit confirmation.",
        router="/reminders/",
        cost_class="write",
    )

    tool_registry.register(
        tool_id="gmail_search",
        description=(
            "Semantic search over the user's synced Gmail inbox. "
            "Use for questions like 'what did the contractor say about the bathroom?' "
            "or 'find emails about my Amazon order'. Searches embedded content, not live API."
        ),
        router="/gmail/search",
        cost_class="free",
    )
    tool_registry.register(
        tool_id="gmail_index",
        description=(
            "List the most recent emails from the user's synced Gmail inbox. "
            "Use to answer 'what emails have I received recently?' or browse recent messages."
        ),
        router="/gmail/index",
        cost_class="free",
    )
    tool_registry.register(
        tool_id="gmail_send",
        description=(
            "Send a plain-text email from the user's Gmail account. "
            "Requires explicit user confirmation — state recipient, subject, and full body "
            "and wait for explicit approval before calling."
        ),
        router="/gmail/send",
        cost_class="write",
    )
    tool_registry.register(
        tool_id="system_stats",
        description=(
            "Report Lumina's internal performance stats: tool cache hit rate and live entries. "
            "Use when the user asks how the system is performing or wants a status report."
        ),
        router="internal://system_stats",
        cost_class="free",
    )
    tool_registry.register(
        tool_id="inference_stats",
        description=(
            "Report LLM inference performance: avg tokens/s, p95 latency, cold load rate. "
            "Use when the user asks about token speed, response latency, or model throughput."
        ),
        router="/observability/ollama/summary",
        cost_class="free",
    )


def register_tools() -> None:
    """Idempotent — safe to call on every startup. Add new tools here as
    verticals come online (Phase 11+).

    Retries on OperationalError so a brief postgres-startup window during
    co-boot (e.g. host reboot) doesn't kill the FastAPI lifespan.
    """
    attempts = 10
    delay = 1.0
    for attempt in range(1, attempts + 1):
        try:
            _register_tools_inner()
            return
        except OperationalError as e:
            if attempt == attempts:
                raise
            log.warning(
                "register_tools: postgres not ready (attempt %d/%d): %s",
                attempt, attempts, e.orig if hasattr(e, "orig") else e,
            )
            time.sleep(delay)
            delay = min(delay * 1.5, 5.0)


def embed_tools(tools: list) -> int:
    """Embed tool descriptions into ChromaDB 'tools' collection.

    Compares a short MD5 hash of each description against the stored
    `embed_hash` in postgres. Only (re)embeds tools whose description has
    changed since the last startup — typically 0 re-embeds after the first run.

    Returns the number of tools that were (re)embedded.
    Failures are logged and never raised — a stale embedding is recoverable,
    a crashed lifespan is not.
    """
    try:
        client = chromadb.HttpClient(
            host=CHROMA_HOST,
            port=int(CHROMA_PORT),
            settings=Settings(anonymized_telemetry=False),
        )
        collection = client.get_or_create_collection(
            name="tools",
            metadata={"hnsw:space": "cosine"},
        )
    except Exception as e:
        print(f"---- TOOL_EMBED ChromaDB unavailable, skipping: {e}")
        return 0

    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT id, embed_hash FROM tools")).mappings().all()
        stored_hashes: dict[str, str | None] = {r["id"]: r["embed_hash"] for r in rows}
    except Exception as e:
        print(f"---- TOOL_EMBED could not read hashes from postgres: {e}")
        stored_hashes = {}

    embedded = 0
    for tool in tools:
        desc = tool.description
        new_hash = hashlib.md5(desc.encode()).hexdigest()[:16]

        if stored_hashes.get(tool.name) == new_hash:
            continue

        try:
            collection.upsert(
                ids=[tool.name],
                documents=[desc],
                metadatas=[{"tool_id": tool.name}],
            )
            with engine.begin() as conn:
                conn.execute(
                    text("UPDATE tools SET embed_hash = :h WHERE id = :id"),
                    {"h": new_hash, "id": tool.name},
                )
            embedded += 1
            print(f"---- TOOL_EMBED '{tool.name}' (hash={new_hash})")
        except Exception as e:
            print(f"---- TOOL_EMBED error '{tool.name}': {e}")

    print(f"---- TOOL_EMBED done: {embedded}/{len(tools)} (re)embedded")
    return embedded
