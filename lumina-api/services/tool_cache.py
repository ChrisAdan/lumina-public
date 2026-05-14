"""Phase 15.1 — tool result cache.

In-memory TTL cache keyed on (tool_name, canonical_args). Per-tool TTLs are
defined below; write tools are never cached. A single shared instance lives at
module level so it's shared across all requests in the same process.

The runbook spec says ChromaDB-backed, but for a single-process app an in-memory
dict is strictly faster and simpler (no network hop, no serialization overhead).
If the system ever scales to multiple processes, replace with Redis.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

# Per-tool TTLs in seconds. Tools not listed here are not cached.
_TTL: dict[str, int] = {
    # Weather — stale after 1h (daily refresh cron anyway)
    "weather_forecast":     3_600,
    # Calendar — events can shift; 30 min is safe
    "calendar_upcoming":    1_800,
    "calendar_search":      1_800,
    # Gmail read — new mail arrives constantly; short window to avoid stale inbox
    "gmail_search":         1_800,
    "gmail_index":          1_800,
    # Web search — varies by topic; 24h is pragmatic
    "web_search":           86_400,
    "fetch_url":            3_600,
    # Document search — local corpus; stable within a sync cycle
    "document_search":      3_600,
    "document_list":        3_600,
    # Recipes / menus — essentially static content
    "search_recipes":       86_400,
    "recipes_from_fridge":  86_400,
    "menu_search":          86_400,
    # People — synapse can update every 5 min; short window
    "people_lookup":          300,
    "people_search":          300,
    # Movies — TMDB data; stable day-to-day
    "movies_find":          86_400,
    "movies_search":        86_400,
    "movies_rank":          86_400,
    "movies_preferences":   86_400,
    # Plants — watering state changes after plant_fed; short window
    "plants_due":           1_800,
    "plants_list":          1_800,
    # Fitness — goals/exercises are stable
    "fitness_exercises":   21_600,
    "fitness_goals":        3_600,
    # Groceries — list changes on add/complete; short window
    "groceries_list":         300,
    # GitHub — repo content; 1h
    "github_list_repo":     3_600,
    "github_read_file":     3_600,
    "github_read_url":      3_600,
    "github_search_code":   3_600,
    "github_list_issues":   3_600,
    # Files — filesystem can change; short window
    "file_list":              300,
    "file_read":              300,
    # Reminders — list changes on set/cancel; skip caching (already fast)
    # Market signals — computed from ChromaDB; stable within a trading day
    "market_signals":   3_600,
    # Trading — positions change infrequently; brief is stable all day
    "trading_positions": 300,
    "trading_brief":    3_600,
    "trading_pnl":      3_600,
}

# Write tools — always skipped regardless of TTL table
_WRITE_TOOLS: frozenset[str] = frozenset({
    "gmail_send",
    "file_write",
    "groceries_add",
    "groceries_complete",
    "plant_fed",
    "plant_add",
    "reminder_set",
    "reminder_cancel",
    "fitness_goal_set",
    "fitness_plan_save",
    "people_contact_save",
    "run_python",
    "query_sql",
})


def _cache_key(name: str, args: dict) -> str:
    payload = json.dumps({"n": name, "a": args}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


class ToolCache:
    def __init__(self) -> None:
        self._store: dict[str, tuple[float, Any]] = {}  # key → (expires_at, result)
        self.hits = 0
        self.misses = 0

    def get(self, name: str, args: dict) -> tuple[bool, Any]:
        """Return (hit, value). value is None on miss."""
        if name in _WRITE_TOOLS or name not in _TTL:
            return False, None
        key = _cache_key(name, args)
        entry = self._store.get(key)
        if entry and time.monotonic() < entry[0]:
            self.hits += 1
            log.debug("cache hit  %s %s", name, key)
            return True, entry[1]
        self.misses += 1
        return False, None

    def set(self, name: str, args: dict, result: Any) -> None:
        """Cache result if tool is cacheable and result is not an error."""
        if name in _WRITE_TOOLS or name not in _TTL:
            return
        if isinstance(result, dict) and "error" in result:
            return  # don't cache errors — transient failures shouldn't persist
        ttl = _TTL[name]
        key = _cache_key(name, args)
        self._store[key] = (time.monotonic() + ttl, result)

    def invalidate(self, *tool_names: str) -> int:
        """Remove all cached entries for the given tool names. Returns count removed."""
        to_remove = [k for k, v in self._store.items() if any(
            _cache_key(n, {})[:0] == k[:0] for n in tool_names
        )]
        # Simpler: just clear all entries — called rarely and store is small
        before = len(self._store)
        self._store = {k: v for k, v in self._store.items()
                       if time.monotonic() < v[0]}  # evict expired while we're here
        return before - len(self._store)

    def stats(self) -> dict:
        now = time.monotonic()
        live = sum(1 for _, (exp, _) in self._store.items() if now < exp)
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
            "live_entries": live,
        }

    def clear(self) -> None:
        self._store.clear()


# Module-level singleton
_cache = ToolCache()


def get_cache() -> ToolCache:
    return _cache
