"""Triage classifier — model routing and safety exclusions (no extra LLM call).

Tool selection is handled by ChromaDB semantic search in inference/tool_selector.py.
This module is responsible only for:
  1. Routing to the right specialist model (code, reasoning, general).
  2. Injecting code execution tool hints (tied to the same reliable regex as model routing).
  3. Confirming write-action tools on short imperative turns ("yes", "ok", "send it").
  4. Hard-blocking gmail_send when no email address is present in the message.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from configs.app import CODE_FAST_MODEL_TAG, CODE_HEAVY_MODEL_TAG

FAST_MODEL_TAG = "lumina-prod"
REASON_MODEL_TAG = "lumina-prod-30b"

# Tools that require synthesis over retrieved content — route to 30b.
# Checked post-selection in openai_compat.py (after ChromaDB determines which tools fire).
# document_search removed: 4b handles retrieved chunks fine; false-escalation rate too high
# because document_search appears as a semantic neighbor for many unrelated queries.
SYNTHESIS_TOOLS: frozenset[str] = frozenset({"people_lookup"})


@dataclass
class TriageResult:
    intent: str
    confidence: float
    specialist: str
    tool_hints: list[str] = field(default_factory=list)
    exclude_tools: set[str] = field(default_factory=set)

    @classmethod
    def fallback(cls) -> "TriageResult":
        return cls(intent="general", confidence=0.5, specialist=FAST_MODEL_TAG)


# ── Routing patterns (model selection only) ───────────────────────────────────

_RE_CODE_PY  = re.compile(r'\b(python|pandas|numpy|\.py\b|ipynb|pip install)\b', re.I)
_RE_CODE_SQL = re.compile(r'\b(sql|select\b|insert into|schema|query|postgres|psql)\b', re.I)
_RE_REASON   = re.compile(r'\b(analyze|compare|explain why|step.?by.?step|think through)\b', re.I)

# Short confirmations — used to inherit write-action hints from the previous assistant turn.
_RE_SHORT_IMPERATIVE = re.compile(
    r'\b(yes|ok|okay|yeah|yep|confirm|go ahead|send it|do it|fire it|push it|'
    r'push again|try again|send the (reminder|email)|push the (reminder|notification))\b', re.I
)

# Email send detection — used ONLY for the gmail_send safety exclusion.
_RE_GMAIL_SEND = re.compile(
    r'\b(send .{0,25}email|email (to |them |him |her )?about|draft .{0,15}email|'
    r'write .{0,15}email|compose .{0,15}email|reply (to )?(the )?email)\b', re.I
)

# GitHub URL pattern — imported and used directly by openai_compat.py.
_GITHUB_URL_RE = re.compile(r'https?://(?:github\.com|raw\.githubusercontent\.com)/', re.I)


def _triage_classify(messages: list[dict]) -> TriageResult:
    """Classify intent and select specialist model from the last user message.

    Returns empty tool_hints for most turns — ChromaDB semantic search in
    tool_selector.py handles tool selection. Hints are only added for code
    execution tools (tied to the same reliable regex as model routing) and
    write-action confirmation turns ("yes/ok" inheriting from previous turn).
    """
    last_user = ""
    last_assistant = ""
    for m in reversed(messages):
        role = m.get("role")
        if role == "user" and not last_user:
            last_user = (m.get("content") or "").strip()[:500]
        elif role == "assistant" and not last_assistant:
            last_assistant = (m.get("content") or "").strip()[:300]
        if last_user and last_assistant:
            break
    if not last_user:
        return TriageResult.fallback()

    # ── Model routing ─────────────────────────────────────────────────────────
    if _RE_CODE_SQL.search(last_user):
        intent, specialist = "code_sql", CODE_FAST_MODEL_TAG
    elif _RE_CODE_PY.search(last_user):
        intent, specialist = "code_python", CODE_HEAVY_MODEL_TAG
    elif _RE_REASON.search(last_user):
        intent, specialist = "reasoning", REASON_MODEL_TAG
    else:
        intent, specialist = "general", FAST_MODEL_TAG

    # ── Tool hints: code execution only ──────────────────────────────────────
    # Tied directly to the model-routing regexes above — reliable because the
    # same signal that selects the model also predicts the tool.
    tool_hints: list[str] = []
    if intent == "code_python":
        tool_hints.append("run_python")
    if intent == "code_sql":
        tool_hints += ["query_sql", "get_schema"]

    # ── Short confirmation turns ──────────────────────────────────────────────
    # "yes", "ok", "send it" — inherit write-action hints from what the model
    # was proposing in the previous turn. Semantic search alone can't match
    # "yes" to a specific write tool, so we infer from conversation context.
    if len(last_user) <= 80 and _RE_SHORT_IMPERATIVE.search(last_user) and last_assistant:
        asst = last_assistant.lower()
        if ("gmail_send" in asst
                or ("email" in asst and any(w in asst for w in ("send", "draft", "compose", "@", "subject")))):
            tool_hints.append("gmail_send")
        if "people_contact_save" in asst or ("save" in asst and "contact" in asst):
            tool_hints.append("people_contact_save")
        if "reminder_set" in asst or "set this reminder" in asst:
            tool_hints.append("reminder_set")
        if "groceries_add" in asst:
            tool_hints.append("groceries_add")
        if "file_write" in asst:
            tool_hints.append("file_write")

    # ── Safety exclusion ──────────────────────────────────────────────────────
    # Block gmail_send when no address is present — forces people_lookup to
    # surface via semantic selection so the model fetches a real address first.
    exclude: set[str] = set()
    if _RE_GMAIL_SEND.search(last_user) and "@" not in last_user:
        exclude.add("gmail_send")

    return TriageResult(intent=intent, confidence=1.0, specialist=specialist,
                        tool_hints=tool_hints, exclude_tools=exclude)
