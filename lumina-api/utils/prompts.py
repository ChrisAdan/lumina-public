import re
from pathlib import Path
from typing import Iterable, Protocol

BASE_PATH = Path(__file__).resolve().parent.parent / "agents"

# Soft cap on how many doc lines we inject into the system prompt. Above this,
# the rest are summarized as `...and N more`. Bounds prompt token growth at
# ~30 lines × ~15 tokens ≈ 450 tokens. Phase 14.5 replaces this with semantic
# top-K retrieval over an embedded catalog.
LIBRARY_INVENTORY_CAP = 30

# Mode → trigger keywords. Single-word stems only; list variants explicitly
# (plurals, -ing forms) so token-set matching stays predictable.
# To add a mode: drop <name>.md into agents/ and add an entry here.
MODE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "chef": (
        "recipe", "recipes", "cook", "cooking", "ingredient", "ingredients",
        "meal", "meals", "dinner", "lunch", "breakfast", "kitchen",
    ),
    "fitness": (
        "workout", "workouts", "fitness", "exercise", "exercises", "gym",
        "lift", "lifting", "cardio", "run", "running", "stretch",
    ),
    "planner": (
        "plan", "plans", "planning", "schedule", "scheduling", "organize",
        "organizing", "itinerary", "agenda", "todo", "calendar",
    ),
    "musician": (
        "bpm", "mix", "mixing", "synth", "song", "songs", "produce",
        "production", "psytrance", "trance", "edm", "daw", "arrangement",
    ),
    "architect": (
        "architect", "architecture", "design", "system", "systems", "schema",
        "tradeoff", "tradeoffs", "refactor", "scalable", "scalability",
        "api", "service", "services", "module", "coupling",
    ),
    "teacher": (
        "explain", "teach", "understand", "understanding", "learning", "learn",
        "tutorial", "concept", "concepts", "beginner", "basics", "overview",
        "definition", "difference", "between", "analogy",
    ),
    "networker": (
        "contact", "contacts", "network", "networking", "outreach",
        "introduce", "introduction", "introductions", "colleague", "connection",
        "connections", "followup", "reach",
    ),
}


def _tokenize(prompt: str) -> set[str]:
    return set(re.findall(r"[a-z]+", prompt.lower()))


def detect_modes(prompt: str, limit: int = 2) -> list[str]:
    """Return modes matched in the prompt, ranked by keyword hit count.

    Caps at `limit` so a noisy prompt doesn't stack every persona.
    """
    tokens = _tokenize(prompt)
    scores: dict[str, int] = {}
    for mode, keywords in MODE_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw in tokens)
        if hits:
            scores[mode] = hits
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [m for m, _ in ranked[:limit]]


def detect_mode(prompt: str) -> str | None:
    """Primary mode only — kept for callers that want a single label."""
    modes = detect_modes(prompt, limit=1)
    return modes[0] if modes else None


def _base_prompt_path() -> Path:
    """Resolve the canonical session-base prompt.

    Prefers repo-level AGENTS.md (mounted into the container at /app/AGENTS.md),
    falls back to agents/base.md for environments where AGENTS.md isn't mounted.
    """
    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent / "AGENTS.md",        # in-container: /app/AGENTS.md (bind-mounted)
        here.parent.parent.parent / "AGENTS.md", # local run: repo-root AGENTS.md
        BASE_PATH / "base.md",                   # legacy fallback
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(
        "no base prompt found; expected AGENTS.md or agents/base.md"
    )


def load_base_prompt() -> str:
    return _base_prompt_path().read_text()


def load_mode_prompt(mode: str) -> str:
    path = BASE_PATH / f"{mode}.md"
    return path.read_text() if path.exists() else ""


class _InventoryItem(Protocol):
    id: str
    title: str
    format: str
    metadata: dict


def _is_daily_note(doc: _InventoryItem) -> bool:
    """Vault note in the Dailies/ directory. Pulled from metadata.vertical
    (set by services.markdown_ingest). Falls back to checking the title shape
    so legacy ingests still get collapsed."""
    meta = getattr(doc, "metadata", None) or {}
    if meta.get("vertical") == "daily":
        return True
    # YYYY-MM-DD title — terse fallback for any pre-vertical-tag rows.
    title = (getattr(doc, "title", "") or "").strip()
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", title))


def _format_library_inventory(docs: Iterable[_InventoryItem]) -> str:
    """Render a `## Library inventory` section the LLM can scan when picking
    `document_search` vs `web_search`. Caps at LIBRARY_INVENTORY_CAP entries
    in iteration order; remainder collapses to `...and N more`.

    Daily notes (vault `Dailies/YYYY-MM-DD.md`) collapse to a single line —
    enumerating them line-by-line would blow the prompt budget after a few
    months of journaling, and the model can still reach them via
    `document_search`.

    Caller passes whatever order they want (typically most-recent first via
    `documents.list_all()` which is `ORDER BY ingested_at DESC`).
    """
    items = list(docs)
    if not items:
        return ""

    dailies = [d for d in items if _is_daily_note(d)]
    others = [d for d in items if not _is_daily_note(d)]

    head = others[:LIBRARY_INVENTORY_CAP]
    overflow = len(others) - len(head)
    def _inventory_line(d: _InventoryItem) -> str:
        doc_id = getattr(d, "id", "")
        if doc_id:
            return f"- {d.title} [{doc_id}] ({d.format})"
        return f"- {d.title} ({d.format})"

    lines = [_inventory_line(d) for d in head]
    if overflow > 0:
        lines.append(f"- ...and {overflow} more")

    if dailies:
        # Iteration order is most-recent first per documents.list_all().
        latest = (getattr(dailies[0], "title", "") or "").strip() or "?"
        lines.append(
            f"- Daily notes ({len(dailies)} entr"
            f"{'y' if len(dailies) == 1 else 'ies'}, latest {latest}) (md)"
        )

    body = "\n".join(lines)
    return (
        "## Library inventory "
        "(call `document_search` when the user's question overlaps these)\n\n"
        f"{body}"
    )


def build_system_prompt(
    modes: str | list[str] | None,
    library_inventory: Iterable[_InventoryItem] | None = None,
) -> str:
    """Compose base + any number of mode prompts. Accepts str, list, or None.

    If `library_inventory` is provided and non-empty, append a `## Library
    inventory` section so the model can connect topic queries to ingested
    docs at tool-pick time.
    """
    from configs.app import USER_NAME, USER_EMAIL
    base = load_base_prompt()
    parts: list[str] = [base]

    # Inject household identity so the model always knows who it's serving.
    if USER_NAME or USER_EMAIL:
        identity_lines = ["## Household identity\n"]
        if USER_NAME:
            identity_lines.append(f"You are serving **{USER_NAME}**. This is the person talking to you.")
        if USER_EMAIL:
            identity_lines.append(f"Their email address is **{USER_EMAIL}**.")
        identity_lines.append(
            "When they say 'I', 'me', or 'my', they mean this person. "
            "Never confuse the user with other people in the people notes."
        )
        parts.append("\n".join(identity_lines))

    if modes:
        if isinstance(modes, str):
            modes = [modes]
        mode_prompts = [p for p in (load_mode_prompt(m) for m in modes) if p]
        if mode_prompts:
            parts.extend(mode_prompts)

    if library_inventory is not None:
        section = _format_library_inventory(library_inventory)
        if section:
            parts.append(section)

    return "\n\n".join(parts)
