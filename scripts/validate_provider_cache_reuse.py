#!/usr/bin/env python3
"""Validate provider-side prompt cache reuse with repeated live calls.

This script is intentionally env-gated and is not a unit-test substitute. It
exits 77 when credentials/config are absent so CI can treat it as an optional
external validation job.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from rivumi.contracts import Message
from rivumi.models import OpenAICompatibleModel, ResponsesModel
from rivumi.prompts import PromptSection, render_prompt_sections

SKIP_EXIT = 77


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


def _provider_model() -> tuple[str, str, str, str | None]:
    provider = _env("RIVUMI_CACHE_VALIDATE_PROVIDER")
    model = _env("RIVUMI_CACHE_VALIDATE_MODEL")
    api_key = _env("RIVUMI_CACHE_VALIDATE_API_KEY")
    base_url = _env("RIVUMI_CACHE_VALIDATE_BASE_URL")
    if not provider or not model or not api_key:
        print(
            "skip: set RIVUMI_CACHE_VALIDATE_PROVIDER, "
            "RIVUMI_CACHE_VALIDATE_MODEL, and RIVUMI_CACHE_VALIDATE_API_KEY",
            file=sys.stderr,
        )
        raise SystemExit(SKIP_EXIT)
    if provider not in {"openai-compatible", "openai-responses"}:
        print("skip: provider must be openai-compatible or openai-responses", file=sys.stderr)
        raise SystemExit(SKIP_EXIT)
    return provider, model, api_key, base_url


async def _run() -> int:
    provider, model_id, api_key, base_url = _provider_model()
    messages = (
        Message(
            role="system",
            content=render_prompt_sections(
                (
                    PromptSection("core", "You are a terse test assistant.", cache_stable=True),
                    PromptSection(
                        "runtime",
                        "Return exactly the requested token.",
                        cache_stable=False,
                    ),
                )
            ),
        ),
        Message(role="user", content="Reply with: cache-ok"),
    )
    model: Any
    if provider == "openai-compatible":
        model = OpenAICompatibleModel(
            model=model_id,
            api_key=api_key,
            base_url=base_url,
            max_tokens=16,
        )
    else:
        model = ResponsesModel(
            model=model_id,
            api_key=api_key,
            base_url=base_url or "https://api.openai.com/v1",
            max_output_tokens=16,
            allow_custom_endpoint=bool(base_url),
        )

    try:
        turns = []
        for index in range(2):
            result = await model.complete(messages)
            trace = model.last_cache_trace
            turns.append(
                {
                    "index": index,
                    "content": result.content,
                    "input_tokens": result.usage.input_tokens,
                    "cached_input_tokens": result.usage.cached_input_tokens,
                    "cache_hit_rate": result.usage.input_cache_hit_rate,
                    "trace": trace.__dict__ if trace is not None else None,
                }
            )
    finally:
        await model.aclose()

    cache_reused = any(turn["cached_input_tokens"] > 0 for turn in turns[1:])
    payload = {
        "provider": provider,
        "model": model_id,
        "cache_reused": cache_reused,
        "turns": turns,
    }
    output_path = _env("RIVUMI_CACHE_VALIDATE_OUTPUT")
    if output_path:
        Path(output_path).write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if cache_reused else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
