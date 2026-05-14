"""
services/ollama.py

Centralised Ollama client. All callers go through here so every inference
call is automatically logged to ollama_inference_logs via repos.inference.

The previous design required callers to pass in a SQLAlchemy session for
logging; if any caller forgot, the row silently vanished. Now the chokepoint
owns the connection (via repos.inference.log_call) so logging is always on.

Usage:
    from services.ollama import ollama_generate
    result = await ollama_generate(
        prompt="...",
        model="llava",
        images=[b64_string],
        tool_context="groceries/receipt",
    )
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from configs.app import OLLAMA_URL
from repos import inference as inference_repo
from schemas.inference import OllamaInferenceLog

logger = logging.getLogger(__name__)

_NS_TO_MS = 1_000_000  # Ollama durations are in nanoseconds


# ── Internals ────────────────────────────────────────────────────────────────

def _ms(ns: int | None) -> int | None:
    return round(ns / _NS_TO_MS) if ns else None


def _build_log(
    *,
    model: str,
    tool_context: str | None,
    prompt_preview: str | None,
    response_body: dict,
    success: bool,
    error_message: str | None,
    vertical: str | None = None,
    request_id: str | None = None,
    cache_hit: bool = False,
    triage_bypassed: bool = False,
) -> OllamaInferenceLog:
    eval_count = response_body.get("eval_count")
    eval_duration_ms = _ms(response_body.get("eval_duration"))
    tps: float | None = None
    if eval_count and eval_duration_ms and eval_duration_ms > 0:
        tps = round(eval_count / (eval_duration_ms / 1000), 2)

    return OllamaInferenceLog(
        model=model,
        request_id=request_id,
        vertical=vertical,
        tool_context=tool_context,
        prompt_preview=prompt_preview,
        cache_hit=cache_hit,
        triage_bypassed=triage_bypassed,
        load_duration_ms=_ms(response_body.get("load_duration")),
        prompt_eval_count=response_body.get("prompt_eval_count"),
        prompt_eval_duration_ms=_ms(response_body.get("prompt_eval_duration")),
        eval_count=eval_count,
        eval_duration_ms=eval_duration_ms,
        total_duration_ms=_ms(response_body.get("total_duration")),
        tokens_per_second=tps,
        done_reason=response_body.get("done_reason"),
        success=success,
        error_message=error_message,
    )


# ── Public API ───────────────────────────────────────────────────────────────

async def ollama_generate(
    *,
    prompt: str,
    model: str,
    tool_context: str | None = None,
    images: list[str] | None = None,
    stream: bool = False,
    timeout: float = 60.0,
    extra: dict[str, Any] | None = None,
    vertical: str | None = None,
    request_id: str | None = None,
) -> dict:
    """Call Ollama /api/generate and log timing metadata via repos.inference."""
    payload: dict[str, Any] = {"model": model, "prompt": prompt, "stream": stream}
    if images:
        payload["images"] = images
    if extra:
        payload.update(extra)

    response_body: dict = {}
    success = True
    error_message: str | None = None

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{OLLAMA_URL}/api/generate", json=payload)
            resp.raise_for_status()
            response_body = resp.json()
    except Exception as exc:
        success = False
        error_message = str(exc)
        inference_repo.log_call(_build_log(
            model=model, tool_context=tool_context, prompt_preview=prompt,
            response_body={}, success=False, error_message=error_message,
            vertical=vertical, request_id=request_id,
        ))
        raise

    inference_repo.log_call(_build_log(
        model=model, tool_context=tool_context, prompt_preview=prompt,
        response_body=response_body, success=success, error_message=error_message,
        vertical=vertical, request_id=request_id,
    ))
    return response_body


async def ollama_chat(
    *,
    messages: list[dict],
    model: str,
    tool_context: str | None = None,
    stream: bool = False,
    timeout: float = 60.0,
    extra: dict[str, Any] | None = None,
    vertical: str | None = None,
    request_id: str | None = None,
) -> dict:
    """Call Ollama /api/chat and log timing metadata via repos.inference."""
    payload: dict[str, Any] = {"model": model, "messages": messages, "stream": stream}
    if extra:
        payload.update(extra)

    prompt_preview = messages[-1].get("content", "") if messages else ""
    response_body: dict = {}
    success = True
    error_message: str | None = None

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{OLLAMA_URL}/api/chat", json=payload)
            resp.raise_for_status()
            response_body = resp.json()
    except Exception as exc:
        success = False
        error_message = str(exc)
        inference_repo.log_call(_build_log(
            model=model, tool_context=tool_context, prompt_preview=prompt_preview,
            response_body={}, success=False, error_message=error_message,
            vertical=vertical, request_id=request_id,
        ))
        raise

    inference_repo.log_call(_build_log(
        model=model, tool_context=tool_context, prompt_preview=prompt_preview,
        response_body=response_body, success=success, error_message=error_message,
        vertical=vertical, request_id=request_id,
    ))
    return response_body
