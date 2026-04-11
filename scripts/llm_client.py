"""LLM calls — OpenAI by default; optional Anthropic for CEO (multi-provider bonus)."""
from __future__ import annotations

import json
import os
import re
from typing import Any

from openai import OpenAI


def _extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise ValueError(f"Could not parse JSON from model output: {text[:500]}...")


def call_openai(system_prompt: str, user_prompt: str, *, json_mode: bool = False) -> str:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    client = OpenAI(api_key=api_key)
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            resp = client.chat.completions.create(**kwargs)
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            last_err = e
            if attempt == 0:
                user_prompt = user_prompt + "\n\n(Previous request failed; respond reliably.)"
                continue
            raise last_err from None
    raise last_err  # pragma: no cover


def call_anthropic(system_prompt: str, user_prompt: str) -> str:
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError("anthropic package required for Anthropic provider") from e
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model=model,
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    block = msg.content[0]
    if block.type != "text":
        raise RuntimeError("Unexpected Anthropic response block type")
    return block.text.strip()


def call_llm(
    agent_role: str,
    system_prompt: str,
    user_prompt: str,
    *,
    json_mode: bool = False,
) -> str:
    """
    Route by env: {AGENT}_LLM_PROVIDER or CEO_LLM_PROVIDER pattern.
    agent_role examples: ceo, product, engineer, marketing, qa
    """
    env_key = f"{agent_role.upper()}_LLM_PROVIDER"
    provider = os.environ.get(env_key, os.environ.get("DEFAULT_LLM_PROVIDER", "openai")).lower()
    if provider == "anthropic":
        if json_mode:
            text = call_anthropic(system_prompt, user_prompt + "\n\nRespond with valid JSON only, no markdown.")
            return text
        return call_anthropic(system_prompt, user_prompt)
    if json_mode:
        return call_openai(system_prompt, user_prompt, json_mode=True)
    return call_openai(system_prompt, user_prompt, json_mode=False)


def call_llm_json(agent_role: str, system_prompt: str, user_prompt: str) -> dict[str, Any]:
    text = call_llm(agent_role, system_prompt, user_prompt, json_mode=True)
    return _extract_json_object(text)
