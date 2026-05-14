import asyncio
import logging
import os
import time
from pathlib import Path
from threading import Lock

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from configs.app import (
    CODE_FAST_MODEL_TAG,
    CODE_HEAVY_MODEL_TAG,
    LUMINA_CODE_STRUCTURED_OUTPUT,
    LUMINA_PEOPLE_CONTEXT_ENABLED,
    LUMINA_PRUNE_KEEP_TURNS,
    LUMINA_TRIAGE_ENABLED,
    LUMINA_TOOL_SELECTION_ENABLED,
    LUMINA_TOOL_SELECTION_K,
)
from inference.tool_selector import select_tools as _select_tools
from repos import documents as documents_repo
from services.people_context import extract_names as _extract_people_names, get_people_context
from routers.triage import (
    TriageResult, _triage_classify,
    FAST_MODEL_TAG, REASON_MODEL_TAG, SYNTHESIS_TOOLS, _GITHUB_URL_RE,
)
from routers.tool_loop import (
    _run_tool_loop, _tool_loop_stream, _ollama_url,
    MAX_TOOL_ROUNDS, _tool_result_preview, _tool_args_preview,
)
from services import tools as tool_registry
from services.ollama import ollama_chat as _ollama_chat
from utils.prompts import build_system_prompt, detect_modes

log = logging.getLogger(__name__)

router = APIRouter()

# Hard cap on tokens per generation. Streaming is on for content-mode rounds,
# so the user sees tokens flow in real-time — the cap bounds how long the
# model keeps going, not the spinner time.
DEFAULT_MAX_TOKENS = 1500

# Reasoning models need extra budget because their "thinking" tokens count
# against the cap. Measured: a "thorough sourdough walkthrough" prompt burned
# ~2400 thinking tokens and left only ~330 for the visible answer at 3000.
# 5000 leaves room for both deliberation and a full answer.
REASON_MAX_TOKENS = 5000

# Code models generate longer outputs (full functions/scripts).
CODE_MAX_TOKENS = 3000

def _library_inventory_enabled() -> bool:
    return os.getenv("LUMINA_LIBRARY_INVENTORY_ENABLED", "1").lower() in ("1", "true", "yes")


def _fetch_library_inventory() -> list | None:
    """Pull the document catalog for system-prompt injection. Returns `None`
    on any failure (DB hiccup, repo error) so inference doesn't go down with
    an unrelated dependency. Empty list also returns None so build_system_prompt
    skips the section header rather than rendering an empty list.
    """
    if not _library_inventory_enabled():
        return None
    try:
        docs = documents_repo.list_all()
    except Exception as e:
        log.warning("library inventory fetch failed; skipping injection: %s", e)
        return None
    return docs or None


# TTL cache for library inventory. Avoids a DB hit per request and — more
# importantly — keeps `build_system_prompt` output byte-for-byte identical
# across consecutive turns. Identical system-message text lets Ollama reuse
# its KV cache for the static prefix, so turns 2+ only prefill the new user
# message instead of the full 6k-token prompt. TTL matches the scheduler's
# library sync interval so stale inventory never lasts more than one cycle.
_INVENTORY_CACHE_TTL = float(os.getenv("LUMINA_INVENTORY_CACHE_TTL", "60"))
_inventory_cache: list | None = None
_inventory_cache_ts: float = 0.0
_inventory_cache_lock = Lock()


def _get_cached_inventory() -> list | None:
    global _inventory_cache, _inventory_cache_ts
    now = time.monotonic()
    with _inventory_cache_lock:
        if now - _inventory_cache_ts < _INVENTORY_CACHE_TTL:
            return _inventory_cache
    fresh = _fetch_library_inventory()
    with _inventory_cache_lock:
        _inventory_cache = fresh
        _inventory_cache_ts = time.monotonic()
    return fresh




def _append_turn_directive(messages: list[dict], directive: str) -> list[dict]:
    """Inject a per-turn directive as a late-position system message.

    Previously this mutated the first system message (AGENTS.md + inventory),
    which changed the KV-cacheable prefix on every turn and forced a full
    re-prefill. By appending a NEW system message at the tail instead, the
    static prefix stays identical across turns — Ollama reuses its KV cache
    for those tokens and only prefills the directive + user message (~50-100
    tokens instead of 6k+).
    """
    return list(messages) + [{"role": "system", "content": directive}]


def _append_runtime_context(messages: list[dict], block: str) -> list[dict]:
    """Inject a runtime context block as a late-position system message.

    Same rationale as _append_turn_directive: keep the static system-prompt
    prefix unchanged so Ollama's KV cache reuse kicks in on turn 2+.
    """
    return list(messages) + [{"role": "system", "content": block}]


# Maximum number of non-system turns to forward to Ollama. Older turns in a
# long conversation contribute tokens that grow the prefill linearly; capping
# at HISTORY_WINDOW turns bounds the cold-start cost. System messages are
# always preserved (they carry the base prompt + tool context).
_HISTORY_WINDOW = int(os.getenv("LUMINA_HISTORY_WINDOW", "12"))


def _truncate_history(messages: list[dict]) -> list[dict]:
    """Keep all system messages plus the most recent HISTORY_WINDOW turns.

    A "turn" is one message of role user or assistant. Tool messages are
    counted against the window too (they're part of the same turn's context).
    Returns the original list if it already fits within the window.
    """
    system = [m for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]
    if len(non_system) <= _HISTORY_WINDOW:
        return messages
    truncated = system + non_system[-_HISTORY_WINDOW:]
    dropped = len(non_system) - _HISTORY_WINDOW
    print(f"---- HISTORY_TRUNCATE dropped {dropped} turns (window={_HISTORY_WINDOW})")
    return truncated


async def _prune_history(messages: list[dict]) -> list[dict]:
    """Phase 15.2 — summarize old turns instead of dropping them.

    Keeps the last LUMINA_PRUNE_KEEP_TURNS non-system turns verbatim.
    If the history is longer, sends the older turns to the 4b model for a
    3-sentence summary and injects it as an early system message.

    Falls back to plain truncation if summarization fails or is disabled.
    """
    if LUMINA_PRUNE_KEEP_TURNS <= 0:
        return _truncate_history(messages)

    system_msgs = [m for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]

    if len(non_system) <= LUMINA_PRUNE_KEEP_TURNS:
        return messages

    old_turns = non_system[:-LUMINA_PRUNE_KEEP_TURNS]
    keep_turns = non_system[-LUMINA_PRUNE_KEEP_TURNS:]

    # Build a terse transcript of the old turns for the summarizer
    transcript_parts = []
    for m in old_turns:
        role = m.get("role", "")
        content = (m.get("content") or "")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        if role in ("user", "assistant") and content.strip():
            label = "User" if role == "user" else "Assistant"
            transcript_parts.append(f"{label}: {content.strip()[:300]}")
    transcript = "\n".join(transcript_parts)

    summary = ""
    if transcript:
        try:
            summarizer_msgs = [
                {"role": "system", "content": "You are a concise conversation summarizer."},
                {"role": "user", "content": (
                    "Summarize the following conversation excerpt in 2-3 sentences. "
                    "Focus on facts, decisions, and context that would matter for the next reply. "
                    "Be terse — no preamble.\n\n" + transcript
                )},
            ]
            result = await _ollama_chat(
                messages=summarizer_msgs,
                model=FAST_MODEL_TAG,
                stream=False,
                timeout=20.0,
            )
            summary = (result.get("message", {}).get("content") or "").strip()
        except Exception as exc:
            print(f"---- PRUNE_SUMMARY failed ({exc}), falling back to truncation")

    if summary:
        summary_block = {"role": "system", "content": f"[Earlier conversation summary]: {summary}"}
        pruned = system_msgs + [summary_block] + keep_turns
        print(f"---- HISTORY_PRUNE summarized {len(old_turns)} turns → {len(summary)} chars; keeping {len(keep_turns)}")
        return pruned
    else:
        # Summarization failed — fall back to plain truncation
        return _truncate_history(messages)


def _modes_enabled() -> bool:
    return os.getenv("LUMINA_MODES_ENABLED", "0").lower() in ("1", "true", "yes")


def _resolve_model_override(
    messages: list[dict], default_model: str
) -> tuple[str, list[dict]]:
    """Parse `/reason` or `/fast` on the latest user message to override model.

    Returns (resolved_tag, messages_with_prefix_stripped). If no prefix is
    present on the latest user turn, the default model and original messages
    are returned unchanged. The prefix is stripped so the model only sees the
    real query.

    Routing is strictly user-driven. A learned classifier (qwen2.5:0.5b triage
    model, see runbook phase 14.2) will replace this manual toggle once wired.
    """
    if not messages:
        return default_model, messages

    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") != "user":
            continue
        content = (messages[i].get("content") or "").lstrip()
        lowered = content.lower()
        for prefix, tag in (("/reason", REASON_MODEL_TAG), ("/fast", FAST_MODEL_TAG)):
            if lowered.startswith(prefix + " ") or lowered == prefix:
                stripped = content[len(prefix):].lstrip()
                new_messages = list(messages)
                new_messages[i] = {**messages[i], "content": stripped}
                return tag, new_messages
        break

    return default_model, messages


_SKILLS_DIR = Path(__file__).resolve().parent.parent / "agents" / "skills"

_SKILL_ALIASES: dict[str, str] = {
    "profile": "profile-person",
    "introspect": "introspect-codebase",
    "review": "code-review",
}

# Tools each skill needs — focused hints prevent synthesis escalation on fast skills.
_SKILL_TOOL_HINTS: dict[str, list[str]] = {
    "profile-person":      ["people_lookup", "people_search"],
    "introspect-codebase": ["file_list", "file_read"],
    "code-review":         ["file_read", "file_grep", "document_search"],
}

# Model routing per skill. code-review needs the 30b model to reliably execute
# the multi-step playbook (read → grep → document_search → synthesize). The
# other skills are lighter and stay on the fast model.
_SKILL_MODELS: dict[str, str] = {
    "code-review": REASON_MODEL_TAG,
}

# Minimum number of tool-call rounds required before synthesis is allowed.
# The tool loop holds tool_choice="required" until this many rounds have fired,
# preventing the model from short-circuiting a multi-step playbook.
_SKILL_MIN_TOOL_ROUNDS: dict[str, int] = {
    "code-review": 3,  # file_read → file_grep → document_search → then synthesize
}


def _resolve_skill_command(
    messages: list[dict],
) -> tuple[str | None, str, list[dict]]:
    """Detect /skill [arg] on the latest user message.

    Returns (skill_name, skill_arg, messages_with_prefix_stripped).
    skill_name is None if no recognized skill prefix found.
    The prefix is stripped and arg becomes the message content so the model
    sees the subject (e.g. "Diego") rather than the raw slash command.
    """
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") != "user":
            continue
        content = (messages[i].get("content") or "").lstrip()
        if not content.startswith("/"):
            break
        parts = content.split(None, 2)
        cmd = parts[0][1:].lower()
        skill = _SKILL_ALIASES.get(cmd)
        if skill is None:
            break
        arg = parts[1] if len(parts) > 1 else ""
        new_messages = list(messages)
        new_messages[i] = {**messages[i], "content": arg if arg else content}
        return skill, arg, new_messages
    return None, "", messages


def _load_skill_playbook(name: str, arg: str) -> str | None:
    path = _SKILLS_DIR / f"{name}.md"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    if arg:
        text = text.replace("{{arg}}", arg)
    return text


def _inject_lumina_system(
    messages: list[dict],
    requested_mode: str | list[str] | None = None,
    library_inventory: list | None = None,
) -> list[dict]:
    """Prepend Lumina's system prompt (AGENTS.md base + matched modes).

    If Open WebUI supplied its own system message, merge Lumina's in front of it
    rather than overwriting — user-configured instructions are still respected.

    `library_inventory`: pass-through to `build_system_prompt`. If None, the
    inventory is fetched here.
    """
    user_texts = [m.get("content", "") for m in messages if m.get("role") == "user"]
    last_user = user_texts[-1] if user_texts else ""

    if requested_mode is None:
        modes = detect_modes(last_user) if _modes_enabled() else []
    elif isinstance(requested_mode, str):
        modes = [requested_mode]
    else:
        modes = requested_mode

    inv = library_inventory if library_inventory is not None else _get_cached_inventory()
    lumina_system = build_system_prompt(modes, library_inventory=inv)
    if modes:
        lumina_system += f"\n\nActive mode(s): {', '.join(modes)}"

    out: list[dict] = []
    merged = False
    for m in messages:
        if not merged and m.get("role") == "system":
            out.append({
                "role": "system",
                "content": f"{lumina_system}\n\n{m.get('content', '')}",
            })
            merged = True
        else:
            out.append(m)
    if not merged:
        out.insert(0, {"role": "system", "content": lumina_system})
    return out


def _inject_people_runtime_context(messages: list[dict]) -> list[dict]:
    if not LUMINA_PEOPLE_CONTEXT_ENABLED:
        return messages

    last_user = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user = (m.get("content") or "").strip()
            break
    if not last_user:
        return messages

    block = get_people_context(last_user)
    if not block:
        return messages
    print(f"---- PEOPLE_CONTEXT injected for {', '.join(_extract_people_names(last_user))}")
    return _append_runtime_context(messages, block)


@router.post("/chat/completions")
async def chat_completions(req: Request):
    req_t0 = time.perf_counter()
    body = await req.json()
    # Always stream — better UX regardless of what the client requests.
    client_wants_stream = True

    requested_model = body.get("model") or FAST_MODEL_TAG

    # Phase 14.1: triage classifier path
    # Dev overrides (/reason, /fast) bypass triage and route directly.
    if LUMINA_TRIAGE_ENABLED:
        resolved_model, body["messages"] = _resolve_model_override(
            body.get("messages", []), requested_model
        )
        if resolved_model != requested_model:
            # Dev override present — skip triage, log override
            print(f"---- MODEL OVERRIDE {requested_model} -> {resolved_model} (dev override; triage skipped)")
            body["model"] = resolved_model
            triage = TriageResult(intent="general", confidence=1.0, specialist=resolved_model)
            inventory = await asyncio.to_thread(_get_cached_inventory)
        else:
            triage = _triage_classify(body.get("messages", []))
            inventory = await asyncio.to_thread(_get_cached_inventory)
            print(
                f"---- TRIAGE intent={triage.intent} "
                f"specialist={triage.specialist} "
                f"confidence={triage.confidence:.2f}"
            )
            if triage.specialist != requested_model:
                body["model"] = triage.specialist
                resolved_model = triage.specialist
            else:
                resolved_model = requested_model

        # Fast-path: augment triage with unambiguous URL signals the 4b model
        # misses. GitHub URL reads are not code-generation — force the general
        # model (it has proper instruct + tool-call support) and pin the tool.
        _last_raw = ""
        for _m in reversed(body.get("messages", [])):
            if _m.get("role") == "user":
                _last_raw = (_m.get("content") or "").strip()
                break
        if _GITHUB_URL_RE.search(_last_raw):
            triage = TriageResult(
                intent="general",
                confidence=triage.confidence,
                specialist=FAST_MODEL_TAG,
                tool_hints=["github_read_url"],
            )
            body["model"] = FAST_MODEL_TAG
            resolved_model = FAST_MODEL_TAG
            print("---- TRIAGE_AUGMENT github_read_url (url fast-path → general model)")
    else:
        # Legacy: manual /reason and /fast overrides only
        resolved_model, body["messages"] = _resolve_model_override(
            body.get("messages", []), requested_model
        )
        if resolved_model != requested_model:
            print(f"---- MODEL OVERRIDE {requested_model} -> {resolved_model}")
            body["model"] = resolved_model
        triage = TriageResult(intent="general", confidence=0.5, specialist=resolved_model)
        inventory = await asyncio.to_thread(_get_cached_inventory)
    skill_name, skill_arg, body["messages"] = _resolve_skill_command(body["messages"])
    if skill_name:
        skill_model = _SKILL_MODELS.get(skill_name, FAST_MODEL_TAG)
        skill_hints = _SKILL_TOOL_HINTS.get(skill_name, [])
        triage = TriageResult(
            intent="general",
            confidence=1.0,
            specialist=skill_model,
            tool_hints=skill_hints,
            exclude_tools=triage.exclude_tools,
        )
        body["model"] = skill_model
        resolved_model = skill_model
        min_rounds = _SKILL_MIN_TOOL_ROUNDS.get(skill_name, 1)
        if min_rounds > 1:
            body["_lumina_min_tool_rounds"] = min_rounds
            print(f"---- SKILL_MIN_TOOL_ROUNDS {skill_name} -> {min_rounds}")
        print(f"---- SKILL_MODEL {skill_name} -> {skill_model}")
    body["messages"] = await _prune_history(body["messages"])
    body["messages"] = _inject_lumina_system(
        body["messages"],
        requested_mode=body.pop("lumina_mode", None),
        library_inventory=inventory,
    )
    if skill_name:
        playbook = _load_skill_playbook(skill_name, skill_arg)
        if playbook:
            body["messages"] = _append_turn_directive(body["messages"], playbook)
            print(f"---- SKILL {skill_name} arg={skill_arg!r}")
    body["messages"] = _inject_people_runtime_context(body["messages"])
    if LUMINA_TOOL_SELECTION_ENABLED:
        _last_user = ""
        for _m in reversed(body["messages"]):
            if _m.get("role") == "user":
                _last_user = (_m.get("content") or "").strip()
                break
        _selected = _select_tools(_last_user, triage.tool_hints, k=LUMINA_TOOL_SELECTION_K,
                                  exclude=triage.exclude_tools or None)
        body["tools"] = tool_registry.openai_schemas(names=_selected)
        print(
            f"---- TOOL_SELECTION {len(body['tools'])}/{len(tool_registry.TOOLS)} "
            f"selected={_selected}"
        )

        # Post-selection synthesis escalation: if ChromaDB surfaced a synthesis
        # tool (people_lookup, document_search) and this isn't primarily a write
        # action, escalate to the 30b model which stays on-source.
        _WRITE_TOOLS = {"gmail_send", "file_write", "groceries_add", "plant_fed",
                        "people_contact_save", "reminder_set", "fitness_goal_set", "fitness_plan_save"}
        _selected_set = set(_selected)
        if (not skill_name
                and triage.specialist == FAST_MODEL_TAG
                and any(t in SYNTHESIS_TOOLS for t in _selected_set)
                and not (set(triage.tool_hints) & _WRITE_TOOLS)):
            body["model"] = REASON_MODEL_TAG
            resolved_model = REASON_MODEL_TAG
            triage = TriageResult(intent="synthesis", confidence=triage.confidence,
                                  specialist=REASON_MODEL_TAG, tool_hints=triage.tool_hints,
                                  exclude_tools=triage.exclude_tools)
            print(f"---- SYNTHESIS_ESCALATE {FAST_MODEL_TAG} -> {REASON_MODEL_TAG}")

        # Inject structured synthesis framing when the 30b model is handling a
        # synthesis or reasoning task. Gives the model an explicit task definition
        # and output contract so it doesn't drift into training-data elaboration.
        if resolved_model == REASON_MODEL_TAG and triage.intent in ("synthesis", "reasoning"):
            synthesis_directive = (
                "<synthesis_task>\n"
                "You are synthesizing tool results to answer the user's question.\n"
                "Rules:\n"
                "- Use ONLY facts present in the tool results. Do not add from training data.\n"
                "- If the tool returned nothing, say so plainly.\n"
                "- Lead with a direct answer, then supporting details from the results.\n"
                "- Cite sources inline using the `cite` or `source_url` fields from the results.\n"
                "</synthesis_task>"
            )
            body["messages"] = _append_turn_directive(body["messages"], synthesis_directive)
            print(f"---- SYNTHESIS_DIRECTIVE injected (model={resolved_model}, intent={triage.intent})")
    else:
        body["tools"] = tool_registry.openai_schemas()

    for t in body["tools"]:
        fn = t.get("function", {})
        if not fn.get("name"):
            raise ValueError(f"Invalid tool schema (missing name): {t}")
        if "parameters" not in fn:
            raise ValueError(f"Invalid tool schema (missing parameters): {t}")

    if LUMINA_TRIAGE_ENABLED and triage.tool_hints:
        body["tool_choice"] = "required"
        hint_names = ", ".join(triage.tool_hints)
        turn_nudge = (
            f"Use {hint_names} tool{'s' if len(triage.tool_hints) > 1 else ''}. "
            "Do not answer directly."
        )
        body["messages"] = _append_turn_directive(body["messages"], turn_nudge)
        print(f"---- TRIAGE_HINTS {triage.tool_hints}")

    # Phase 14.5: structured output for code intents (first round only).
    # Gated by LUMINA_CODE_STRUCTURED_OUTPUT; disable if it conflicts with
    # tool calls on your Ollama version.
    # Never combine format="json" with tool_hints — tool_choice="required" and
    # format="json" are mutually exclusive: the model outputs JSON prose instead
    # of a proper tool_call, producing the hallucinatory structured-JSON symptom.
    if (
        LUMINA_TRIAGE_ENABLED
        and LUMINA_CODE_STRUCTURED_OUTPUT
        and triage.intent in ("code_python", "code_sql")
        and not triage.tool_hints
    ):
        body["format"] = "json"
        print(f"---- CODE_STRUCTURED_OUTPUT intent={triage.intent}")

    model_tag = body.get("model", resolved_model)
    if model_tag == REASON_MODEL_TAG:
        token_cap = REASON_MAX_TOKENS
    elif model_tag in (CODE_HEAVY_MODEL_TAG, CODE_FAST_MODEL_TAG):
        token_cap = CODE_MAX_TOKENS
    else:
        token_cap = DEFAULT_MAX_TOKENS
    body.setdefault("max_tokens", token_cap)

    # Disable thinking for the fast and code models — qwen3 think-mode burns
    # tokens on every turn and can bury tool_call intent inside <think> blocks
    # where Ollama discards it. The /reason (30b) model keeps thinking on
    # because that's the whole point of escalating to it.
    if model_tag == REASON_MODEL_TAG:
        body["think"] = True
    else:
        body["think"] = False

    _last_user_log = ""
    for _m in reversed(body["messages"]):
        if _m.get("role") == "user":
            _last_user_log = (_m.get("content") or "")
            break
    print("---- LUMINA /v1 REQUEST ----")
    print("Model:", body.get("model"))
    print("Messages:", len(body["messages"]))
    print("Tools available:", [t["function"]["name"] for t in body["tools"]])
    print("Stream:", client_wants_stream)
    print("---- USER MESSAGE ----")
    print(_last_user_log[:2000])
    print("---- END USER MESSAGE ----")
    print("----------------------------")

    if client_wants_stream:
        return StreamingResponse(
            _tool_loop_stream(body),
            media_type="text/event-stream",
        )

    body["stream"] = False
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=900.0, write=30.0, pool=10.0)) as client:
        result = await _run_tool_loop(client, body)
    total = time.perf_counter() - req_t0
    print(f"---- LUMINA /v1 TOTAL {total:.1f}s")
    return result


@router.get("/models")
async def list_models():
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{_ollama_url()}/v1/models")
        return resp.json()
