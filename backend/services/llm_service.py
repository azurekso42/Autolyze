"""
LLM Service
Connects to LM Studio's OpenAI-compatible endpoint.

Endpoint : http://localhost:1234/v1/chat/completions
Model    : qwen2.5-14b-instruct-uncensored
"""

from __future__ import annotations

import json
import re
import os

import httpx

from models.schemas import LLMResponse
from utils.logging_utils import get_logger

logger = get_logger(__name__)

LM_STUDIO_URL   = os.getenv("LM_STUDIO_URL", "http://localhost:1234/v1/chat/completions")
# Must match an id from GET /v1/models exactly. LM Studio does NOT error on an
# unknown id - it silently routes to some other loaded model, so a typo here
# shows up as unexplained output rather than a clear failure.
#
# An instruct model is deliberate: qwen/qwen3.5-9b is also available, but it is
# a reasoning model that spends most of its token budget thinking, making it far
# slower and prone to truncating mid-JSON. qwen2.5-14b answers this prompt in
# ~217 tokens and a few seconds.
DEFAULT_MODEL   = os.getenv("LM_STUDIO_MODEL", "qwen2.5-14b-instruct")
REQUEST_TIMEOUT = 180


async def call_llm(
    prompt: str,
    temperature: float = 0.0,
    max_tokens: int = 2048,
    system_prompt: str = (
        "You are a precise data analysis assistant. "
        "Always respond with valid JSON only. "
        "Never include markdown, code fences, or explanation."
    ),
) -> LLMResponse:
    payload = _build_payload(prompt, system_prompt, temperature, max_tokens)
    logger.debug("Sending request to LM Studio. Prompt length: %d chars.", len(prompt))

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.post(LM_STUDIO_URL, json=payload)
            response.raise_for_status()
    except httpx.ConnectError:
        raise RuntimeError(
            "Cannot reach LM Studio at http://localhost:1234. "
            "Make sure LM Studio is running and the server is started."
        )
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"LM Studio returned HTTP {exc.response.status_code}: {exc.response.text}"
        )
    except httpx.TimeoutException:
        raise RuntimeError(
            f"LM Studio did not respond within {REQUEST_TIMEOUT}s."
        )

    return _parse_response(response.json())


def extract_json_from_llm(raw: str) -> dict:
    """
    Extract and parse a JSON object from LLM output.

    Tries multiple repair strategies in order:
      1. Strip markdown fences, parse as-is
      2. Extract first { ... } block, parse
      3. Fix trailing commas, parse
      4. Fix literal newlines inside strings, parse
      5. LLM omitted opening brace - prepend { and parse
    """
    if not raw or not raw.strip():
        raise ValueError("LLM returned empty content.")

    # Strategy 1: strip fences and try direct parse
    candidate = _strip_fences(raw)
    result = _try_parse(candidate)
    if result is not None:
        return result

    # Strategy 2: find first { ... } block anywhere in text
    brace_match = re.search(r"\{.*\}", raw, re.DOTALL)
    if brace_match:
        candidate = brace_match.group(0)
        result = _try_parse(candidate)
        if result is not None:
            return result

        logger.warning(
            "JSON block found but all repair attempts failed.\n"
            "Fragment (first 400 chars):\n%.400s", candidate
        )
        raise ValueError(
            "Found JSON-like block but could not parse it.\n"
            "Fragment:\n" + candidate[:300]
        )

    # Strategy 3: LLM omitted the opening brace
    # e.g. output starts with:  \n  "insights": [...], ...
    stripped = raw.strip()
    looks_like_json_body = (
        stripped.startswith('"') or
        (stripped.startswith('\n') and '": ' in stripped) or
        re.match(r'^[\s\n]*"[a-zA-Z]', stripped)
    )
    if looks_like_json_body:
        candidate = stripped if stripped.startswith("{") else "{" + stripped
        if not candidate.rstrip().endswith("}"):
            candidate = candidate.rstrip() + "\n}"
        result = _try_parse(candidate)
        if result is not None:
            return result

    logger.warning(
        "No JSON object found in LLM output.\n"
        "Raw (first 500 chars):\n%.500s", raw
    )
    raise ValueError(
        "No JSON object found in LLM output.\n"
        "Raw response:\n" + raw[:400]
    )


def _try_parse(candidate: str) -> dict | None:
    """
    Try to parse JSON with progressive repairs.
    Returns parsed dict or None if all attempts fail.
    """
    # Attempt 1: raw
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # Attempt 2: fix trailing commas
    try:
        return json.loads(_fix_trailing_commas(candidate))
    except json.JSONDecodeError:
        pass

    # Attempt 3: fix literal newlines inside strings + trailing commas
    try:
        return json.loads(_fix_trailing_commas(_fix_newlines_in_strings(candidate)))
    except json.JSONDecodeError:
        pass

    return None


def _strip_fences(text: str) -> str:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1)
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]) if len(lines) > 2 else text
    return text


def _fix_trailing_commas(text: str) -> str:
    return re.sub(r",\s*([}\]])", r"\1", text)


def _fix_newlines_in_strings(text: str) -> str:
    """
    Escape literal newlines that appear inside JSON string values.
    LLMs sometimes emit multi-line strings without escaping the newline.
    """
    result = []
    in_string = False
    escaped = False

    for ch in text:
        if escaped:
            result.append(ch)
            escaped = False
        elif ch == "\\":
            result.append(ch)
            escaped = True
        elif ch == '"':
            result.append(ch)
            in_string = not in_string
        elif ch == "\n" and in_string:
            result.append("\\n")
        elif ch == "\r" and in_string:
            pass
        else:
            result.append(ch)

    return "".join(result)


def _build_payload(prompt, system_prompt, temperature, max_tokens):
    return {
        "model": DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens":  max_tokens,
        "stream":      False,
    }


def _parse_response(body: dict) -> LLMResponse:
    try:
        choice  = body["choices"][0]
        content = choice["message"]["content"]
        usage   = body.get("usage", {})

        # Truncation is otherwise invisible: the caller just sees malformed
        # JSON and blames the model's formatting rather than the token cap.
        if choice.get("finish_reason") == "length":
            logger.warning(
                "LM Studio hit the token cap (completion_tokens=%s) - the reply "
                "is truncated and will not parse as JSON. Raise max_tokens or "
                "use a non-reasoning model.",
                usage.get("completion_tokens", "?"),
            )

        served = body.get("model")
        if served and served != DEFAULT_MODEL:
            logger.warning(
                "LM Studio served '%s' but '%s' was requested - check that "
                "LM_STUDIO_MODEL matches an id from GET /v1/models.",
                served, DEFAULT_MODEL,
            )

        return LLMResponse(
            content=content,
            model=body.get("model", DEFAULT_MODEL),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )
    except (KeyError, IndexError) as exc:
        raise RuntimeError(
            f"Unexpected LM Studio response format: {exc}\nBody: {body}"
        )
