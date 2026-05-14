"""
routers/tool_loop.py — multi-round tool loop for the Lumina inference path.

Contains the streaming and non-streaming tool loops plus the Lever A/B
content-filtering helpers the streaming path depends on. Extracted from
openai_compat.py to keep that file focused on the FastAPI endpoint glue.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import AsyncGenerator

import httpx

from services import tools as tool_registry

# Cap how many tool round-trips to run for a single user turn.
MAX_TOOL_ROUNDS = 5

# ── Lever A — URL post-stripping ──────────────────────────────────────────────
# _MD_LINK matches a complete [text](url) construct (https?:// or /-rooted).
# _LINK_TAIL catches any partial-link tail at end-of-buffer so a link split
# across SSE chunks isn't leaked before we can rewrite it. Three tails we have
# to recognise:
#   `[lab`         — bracket open, no content yet
#   `[lab]`        — bracket closed, no paren yet
#   `[lab](http...`  — open paren, URL not closed yet
# Only `https?://` and `/`-rooted URLs are stripped — `[ref]` style markdown
# (no parens) and non-web schemes (`mailto:`, etc.) are left untouched.
_MD_LINK = re.compile(r"\[([^\]]+)\]\((?:https?://|/)[^)]*\)")
_LINK_TAIL = re.compile(r"\[[^\]]*(\](\([^)]*)?)?$")

# Lever B — CSS block stripping. The 4b model occasionally emits CSS rule
# blocks (.class-name { ... }) when formatting structured responses. Lines
# matching a CSS selector opening are dropped along with everything until the
# closing `}`. State is carried across SSE chunk boundaries via _in_css_block.
_CSS_OPEN = re.compile(r"^\s*[.#]?[a-zA-Z][a-zA-Z0-9_-]*\s*\{")
_CSS_CLOSE = re.compile(r"^\s*\}\s*$")


def _strip_css_blocks(text: str, in_css: bool) -> tuple[str, bool]:
    """Remove CSS rule blocks from streamed text line-by-line.

    `in_css` carries block state across SSE chunk boundaries.
    Returns (cleaned_text, updated_in_css).
    """
    out_lines: list[str] = []
    for line in text.split("\n"):
        if in_css:
            if _CSS_CLOSE.match(line):
                in_css = False
        else:
            if _CSS_OPEN.match(line):
                in_css = True
            else:
                out_lines.append(line)
    return "\n".join(out_lines), in_css


_SENSITIVE_RESULT_KEYS = {
    "content", "documents", "document", "hits", "results", "events", "files",
    "recipes", "emails", "body", "html",
}
_SENSITIVE_ARG_KEYS = {
    "content", "body", "html", "messages", "prompt", "raw_text",
}


def _strip_links_streaming(buffer: str) -> tuple[str, str]:
    """Strip `[text](url)` constructs from `buffer`. Hold back any trailing
    partial-link tail so a link split across SSE chunks doesn't leak before
    we can rewrite it. Returns (safe_to_emit, residual_to_carry).
    """
    cleaned = _MD_LINK.sub(r"\1", buffer)
    m = _LINK_TAIL.search(cleaned)
    if m:
        return cleaned[: m.start()], cleaned[m.start() :]
    return cleaned, ""


def _tool_result_preview(result: object) -> str:
    """Small, non-content-bearing preview for server logs."""
    if isinstance(result, dict):
        preview: dict[str, object] = {}
        for key, value in result.items():
            if key in _SENSITIVE_RESULT_KEYS:
                if isinstance(value, list):
                    preview[f"{key}_count"] = len(value)
                elif isinstance(value, str):
                    preview[f"{key}_chars"] = len(value)
                elif value is not None:
                    preview[f"{key}_present"] = True
                continue
            preview[key] = value
        return str(preview)[:200]
    if isinstance(result, list):
        return f"list(len={len(result)})"
    return str(result)[:200]


def _tool_args_preview(args: object) -> str:
    """Small, redacted preview for tool-call arguments."""
    if isinstance(args, dict):
        preview: dict[str, object] = {}
        for key, value in args.items():
            if key in _SENSITIVE_ARG_KEYS and isinstance(value, str):
                preview[f"{key}_chars"] = len(value)
                continue
            if key in _SENSITIVE_ARG_KEYS and isinstance(value, list):
                preview[f"{key}_count"] = len(value)
                continue
            preview[key] = value
        return str(preview)[:200]
    return str(args)[:200]


def _strip_urls_enabled() -> bool:
    return os.getenv("LUMINA_STRIP_URLS", "1").lower() in ("1", "true", "yes")


def _ollama_url(model_tag: str = "") -> str:
    """Return the Ollama base URL for the given model tag.

    Falls through: per-model env override → OLLAMA_URL default.
    Override vars: OLLAMA_URL_FAST, OLLAMA_URL_REASON, OLLAMA_URL_CODE.
    """
    from configs.app import (
        OLLAMA_URL, OLLAMA_URL_FAST, OLLAMA_URL_REASON, OLLAMA_URL_CODE,
        CODE_HEAVY_MODEL_TAG, CODE_FAST_MODEL_TAG,
    )
    # Import here to avoid circular deps; triage constants aren't in configs.app
    from routers.triage import FAST_MODEL_TAG, REASON_MODEL_TAG
    if model_tag in (CODE_HEAVY_MODEL_TAG, CODE_FAST_MODEL_TAG):
        return OLLAMA_URL_CODE
    if model_tag == REASON_MODEL_TAG:
        return OLLAMA_URL_REASON
    if model_tag == FAST_MODEL_TAG:
        return OLLAMA_URL_FAST
    # Unknown/unset model — fall back to default
    return OLLAMA_URL


# ── Tool result trimming ──────────────────────────────────────────────────────
# Cap individual string values and list lengths before JSON-serialising tool
# results into the message context. Without this, file_read (100k chars) or
# multi-hit document_search can consume the entire 32k context window in one
# tool round, leaving no room for the model's answer.

_MAX_RESULT_STR_CHARS = 8_000   # per string value inside the result dict
_MAX_RESULT_LIST_ITEMS = 20     # per list value inside the result dict


def _trim_tool_result(result: object) -> object:
    """Recursively cap large strings and lists in a tool result.

    Operates before json.dumps so the model sees a valid, complete (but
    shorter) JSON object rather than a truncated string mid-key.
    """
    if isinstance(result, str):
        if len(result) > _MAX_RESULT_STR_CHARS:
            omitted = len(result) - _MAX_RESULT_STR_CHARS
            return result[:_MAX_RESULT_STR_CHARS] + f"\n[...{omitted} chars omitted]"
        return result
    if isinstance(result, list):
        trimmed = [_trim_tool_result(item) for item in result[:_MAX_RESULT_LIST_ITEMS]]
        if len(result) > _MAX_RESULT_LIST_ITEMS:
            trimmed.append({"_omitted": len(result) - _MAX_RESULT_LIST_ITEMS})
        return trimmed
    if isinstance(result, dict):
        return {k: _trim_tool_result(v) for k, v in result.items()}
    return result


# ── Parallel tool dispatch ────────────────────────────────────────────────────

async def _dispatch_tool_calls(
    round_idx: int,
    tool_calls: list[dict],
) -> list[tuple[dict, str, object]]:
    """Parse, log, and execute all tool calls in a round in parallel.

    Returns (tc, name, result) tuples in the original tool_calls order so
    tool_call_id pairing is correct when appending tool result messages.
    """
    parsed: list[tuple[dict, str, dict]] = []
    for tc in tool_calls:
        fn = tc.get("function", {}) or {}
        name = fn.get("name", "")
        raw_args = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError:
            args = {}
        print(f"---- TOOL CALL [{round_idx}] {name} {_tool_args_preview(args)}")
        parsed.append((tc, name, args))

    t0 = time.perf_counter()
    results = await asyncio.gather(
        *[tool_registry.execute(name, args) for _, name, args in parsed],
        return_exceptions=True,
    )
    elapsed = time.perf_counter() - t0

    if len(parsed) > 1:
        print(f"---- TOOL BATCH [{round_idx}] {len(parsed)} tools in {elapsed:.2f}s (parallel)")

    out: list[tuple[dict, str, object]] = []
    for (tc, name, _), result in zip(parsed, results):
        if isinstance(result, BaseException):
            result = {"error": f"{type(result).__name__}: {result}"}
        trimmed = _trim_tool_result(result)
        if len(parsed) == 1:
            print(f"---- TOOL RESULT [{round_idx}] {name} ({elapsed:.2f}s) -> {_tool_result_preview(result)}")
        else:
            print(f"---- TOOL RESULT [{round_idx}] {name} -> {_tool_result_preview(result)}")
        out.append((tc, name, trimmed))
    return out


# ── Response-complete notifications ───────────────────────────────────────────

async def _notify_done(total_s: float, tool_rounds: int) -> None:
    """Fire an ntfy push when a response completes, if it was worth waiting for."""
    from configs.app import LUMINA_NOTIFY_MIN_SECONDS
    from services.ntfy import send as _ntfy_send
    if total_s < LUMINA_NOTIFY_MIN_SECONDS and tool_rounds == 0:
        return
    msg = f"Took {round(total_s)}s"
    if tool_rounds:
        msg += f" · {tool_rounds} tool round{'s' if tool_rounds != 1 else ''}"
    asyncio.create_task(_ntfy_send(msg, title="Lumina", tags=["robot"]))


# ── Tool loops ────────────────────────────────────────────────────────────────

async def _run_tool_loop(client: httpx.AsyncClient, body: dict) -> dict:
    """Drive the chat ↔ tool loop until the model returns a final answer."""
    url = f"{_ollama_url(body.get('model', ''))}/v1/chat/completions"
    last_response: dict = {}
    req_t0 = time.perf_counter()
    # Skills may require multiple mandatory tool rounds before synthesis.
    # Pop before first Ollama call so it never reaches the model.
    min_tool_rounds: int = body.pop("_lumina_min_tool_rounds", 1)
    tool_rounds_done = 0

    for round_idx in range(MAX_TOOL_ROUNDS):
        print(f"---- LLM ROUND {round_idx} start (msgs={len(body['messages'])})")
        t0 = time.perf_counter()
        resp = await client.post(url, json=body)
        # format="json" is for the first round only (Phase 14.5 structured output).
        body.pop("format", None)
        resp.raise_for_status()
        last_response = resp.json()
        elapsed = time.perf_counter() - t0

        usage = last_response.get("usage") or {}
        pt = usage.get("prompt_tokens", 0)
        ct = usage.get("completion_tokens", 0)
        tps = ct / elapsed if elapsed > 0 and ct else 0.0
        print(
            f"---- LLM ROUND {round_idx} done in {elapsed:.1f}s  "
            f"prompt={pt}  completion={ct}  (~{tps:.1f} t/s)"
        )

        choice = last_response.get("choices", [{}])[0]
        msg = choice.get("message", {}) or {}
        tool_calls = msg.get("tool_calls") or []

        if not tool_calls:
            content = (msg.get("content") or "").strip()
            reasoning = (msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
            print(
                f"---- LLM ROUND {round_idx} no tool call; "
                f"msg_keys={sorted(msg.keys())}  "
                f"content[:200]={content[:200]!r}  "
                f"reasoning[:200]={reasoning[:200]!r}"
            )
            await _notify_done(time.perf_counter() - req_t0, tool_rounds_done)
            return last_response

        # Append the assistant's tool-call message verbatim, then execute all
        # calls in parallel and append results in order.
        body["messages"].append(msg)
        tool_rounds_done += 1

        # Drop forced tool_choice only after the minimum required tool rounds
        # have completed. Skills set min_tool_rounds > 1 to ensure multi-step
        # playbooks aren't short-circuited after the first tool call.
        if tool_rounds_done >= min_tool_rounds:
            body.pop("tool_choice", None)

        for tc, name, trimmed in await _dispatch_tool_calls(round_idx, tool_calls):
            body["messages"].append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "name": name,
                "content": json.dumps(trimmed, default=str),
            })

    # Hit the round cap — return whatever the model last said.
    print(f"---- TOOL LOOP HIT MAX_ROUNDS ({MAX_TOOL_ROUNDS})")
    await _notify_done(time.perf_counter() - req_t0, tool_rounds_done)
    return last_response


async def _tool_loop_stream(body: dict) -> AsyncGenerator[bytes, None]:
    """Streaming tool loop. Yields SSE-formatted bytes for a StreamingResponse.

    Per round, streams from Ollama. Content and reasoning chunks both forward
    to the client — reasoning is repackaged as content so UIs render the
    model's thinking in real-time (useful for qwen3 thinking variants that
    emit a reasoning phase before the answer). Tool-call chunks are buffered;
    at round end, accumulated tool_calls are executed and the loop continues.
    """
    url = f"{_ollama_url(body.get('model', ''))}/v1/chat/completions"
    req_t0 = time.perf_counter()

    # Skills may require multiple mandatory tool rounds before synthesis.
    min_tool_rounds: int = body.pop("_lumina_min_tool_rounds", 1)
    tool_rounds_done = 0

    # Per-request state for Lever A (URL post-stripping). The flag flips on
    # the first round that emits tool_calls and stays True for the rest of
    # the request — any URL the model produces after a tool call is treated
    # as trusted citation material. The residual carries a partial `[...`
    # across SSE chunks so a link split mid-token isn't leaked.
    strip_urls_active = _strip_urls_enabled()
    tool_called_in_request = False
    link_residual = ""
    in_css_block = False

    # connect/write are short; read covers time-to-first-token which can exceed
    # 5 minutes on the 30b model processing a large multi-round skill context.
    _stream_timeout = httpx.Timeout(connect=10.0, read=900.0, write=30.0, pool=10.0)
    async with httpx.AsyncClient(timeout=_stream_timeout) as client:
        for round_idx in range(MAX_TOOL_ROUNDS):
            print(f"---- LLM ROUND {round_idx} start (stream; msgs={len(body['messages'])})")
            t0 = time.perf_counter()

            # include_usage asks Ollama to emit a final usage chunk; without
            # it, streaming rounds log prompt=0/completion=0 and we lose all
            # token telemetry on the path Open WebUI actually uses.
            body_stream = {
                **body,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            # format="json" is first-round only (Phase 14.5); remove from body
            # so subsequent rounds are free-form content.
            body.pop("format", None)

            content_parts: list[str] = []
            tool_calls_accum: dict[int, dict] = {}
            forward_mode = False
            first_decided = False
            usage: dict = {}

            async with client.stream("POST", url, json=body_stream) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    if chunk.get("usage"):
                        usage = chunk["usage"]

                    choices = chunk.get("choices") or []
                    if not choices:
                        if forward_mode:
                            yield f"data: {data}\n\n".encode()
                        continue

                    delta = (choices[0] or {}).get("delta") or {}
                    reasoning_delta = (
                        delta.get("reasoning")
                        or delta.get("reasoning_content")
                        or ""
                    )

                    if not first_decided:
                        if delta.get("tool_calls"):
                            forward_mode = False
                            first_decided = True
                        elif delta.get("content"):
                            # Only commit to content mode on real content, not
                            # reasoning-only deltas — otherwise the round is
                            # pre-committed to content mode even when tool_calls
                            # arrive later (after the <think> block). Reasoning
                            # is still streamed below when forward_mode is True.
                            forward_mode = True
                            first_decided = True

                    if delta.get("content"):
                        content_parts.append(delta["content"])

                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        slot = tool_calls_accum.setdefault(
                            idx,
                            {"id": "", "type": "function",
                             "function": {"name": "", "arguments": ""}},
                        )
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["function"]["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["function"]["arguments"] += fn["arguments"]

                    # Stream reasoning deltas during the undecided phase to keep the
                    # HTTP connection alive. Without this, qwen3's think phase emits
                    # nothing to the client, causing Open WebUI to stall and only show
                    # the response after a page refresh.
                    if reasoning_delta and not first_decided:
                        synth = {
                            **chunk,
                            "choices": [{
                                "index": 0,
                                "delta": {"content": reasoning_delta},
                                "finish_reason": None,
                            }],
                        }
                        yield f"data: {json.dumps(synth)}\n\n".encode()

                    if forward_mode:
                        raw_content = delta.get("content") or ""
                        repackage_reasoning = bool(
                            reasoning_delta and not raw_content
                        )
                        text_for_client = (
                            reasoning_delta if repackage_reasoning else raw_content
                        )

                        # Lever B: strip CSS blocks unconditionally — the
                        # model should never emit raw CSS in any round.
                        mutated_for_strip = False
                        if text_for_client:
                            text_for_client, in_css_block = _strip_css_blocks(
                                text_for_client, in_css_block
                            )
                            if not text_for_client and in_css_block is not False:
                                mutated_for_strip = True

                        # Lever A: on no-tool rounds, run the about-to-emit
                        # text through the link stripper. Once any tool has
                        # fired in this request, citations come from real
                        # tool output — pass through unmodified.
                        if (
                            strip_urls_active
                            and not tool_called_in_request
                            and text_for_client
                        ):
                            link_residual += text_for_client
                            text_for_client, link_residual = (
                                _strip_links_streaming(link_residual)
                            )
                            mutated_for_strip = True

                        if mutated_for_strip or repackage_reasoning:
                            # Build a synthesized chunk so we can mutate
                            # `delta.content` without disturbing the rest of
                            # the OpenAI-compat envelope. Skip emit if the
                            # text was held entirely in residual — we'll
                            # flush at end-of-round.
                            if text_for_client:
                                synth_choices = []
                                for c in choices:
                                    sd = dict(c.get("delta") or {})
                                    sd["content"] = text_for_client
                                    if repackage_reasoning:
                                        sd.pop("reasoning", None)
                                        sd.pop("reasoning_content", None)
                                    synth_choices.append({**c, "delta": sd})
                                synth = {**chunk, "choices": synth_choices}
                                yield f"data: {json.dumps(synth)}\n\n".encode()
                        else:
                            yield f"data: {data}\n\n".encode()

            # Flush any text held in the link-stripper residual. Happens
            # before the tool-call branch so a half-link at end-of-stream
            # isn't silently dropped if the model truncated.
            if forward_mode and link_residual:
                flush_payload = {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": link_residual},
                            "finish_reason": None,
                        }
                    ]
                }
                yield f"data: {json.dumps(flush_payload)}\n\n".encode()
                link_residual = ""

            elapsed = time.perf_counter() - t0
            pt = usage.get("prompt_tokens", 0)
            ct = usage.get("completion_tokens", 0)
            tps = ct / elapsed if elapsed > 0 and ct else 0.0
            tool_calls = list(tool_calls_accum.values())
            round_mode = "tools" if tool_calls else ("content" if forward_mode else "empty")
            print(
                f"---- LLM ROUND {round_idx} done in {elapsed:.1f}s  "
                f"prompt={pt}  completion={ct}  (~{tps:.1f} t/s)  "
                f"mode={round_mode}"
            )

            if tool_calls:
                # Citations from tool results are trusted for the remainder
                # of this request — turn off URL stripping so the model can
                # cite real `web_search` / `fetch_url` URLs in its answer.
                tool_called_in_request = True
                link_residual = ""
                in_css_block = False  # reset CSS state between rounds
                tool_rounds_done += 1

                # Drop forced tool_choice only after the minimum required tool
                # rounds have completed. Skills set min_tool_rounds > 1.
                if tool_rounds_done >= min_tool_rounds:
                    body.pop("tool_choice", None)

                body["messages"].append({
                    "role": "assistant",
                    "content": "".join(content_parts),
                    "tool_calls": tool_calls,
                })

                for tc, name, trimmed in await _dispatch_tool_calls(round_idx, tool_calls):
                    body["messages"].append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "name": name,
                        "content": json.dumps(trimmed, default=str),
                    })
                continue

            if forward_mode:
                yield b"data: [DONE]\n\n"
                total = time.perf_counter() - req_t0
                print(f"---- LUMINA /v1 TOTAL {total:.1f}s (streamed)")
                await _notify_done(total, tool_rounds_done)
                return

            content = "".join(content_parts).strip()
            print(
                f"---- LLM ROUND {round_idx} empty round; "
                f"content[:200]={content[:200]!r}"
            )
            yield b"data: [DONE]\n\n"
            await _notify_done(time.perf_counter() - req_t0, tool_rounds_done)
            return

        print(f"---- TOOL LOOP HIT MAX_ROUNDS ({MAX_TOOL_ROUNDS})")
        yield b"data: [DONE]\n\n"
        await _notify_done(time.perf_counter() - req_t0, tool_rounds_done)
