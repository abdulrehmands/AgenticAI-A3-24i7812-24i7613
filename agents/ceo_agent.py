"""CEO agent — LLM decomposition, reviews, revision routing, final Slack summary + decision log."""
from __future__ import annotations

import json
import os
from typing import Any

import requests

from scripts.llm_client import call_llm_json
from scripts.slack_util import slack_channel, slack_error_hint, slack_json_headers

# Slack Block Kit limits (chat.postMessage)
_SLACK_HEADER_PLAIN_MAX = 150
_SLACK_SECTION_MRDKWN_MAX = 3000


def _truncate_plain(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _section_mrkdwn(heading: str, body: str) -> str:
    prefix = f"*{heading}*\n"
    combined = prefix + body
    if len(combined) <= _SLACK_SECTION_MRDKWN_MAX:
        return combined
    budget = _SLACK_SECTION_MRDKWN_MAX - len(prefix) - 3
    if budget < 1:
        return _truncate_plain(combined, _SLACK_SECTION_MRDKWN_MAX)
    return prefix + body[:budget] + "..."


def post_slack_ceo_summary(title: str, sections: list[tuple[str, str]]) -> None:
    """Post final CEO summary using Block Kit."""
    token = os.environ["SLACK_BOT_TOKEN"]
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": _truncate_plain(title, _SLACK_HEADER_PLAIN_MAX),
                "emoji": True,
            },
        }
    ]
    for heading, text in sections:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": _section_mrkdwn(heading, text)},
            }
        )
    r = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers=slack_json_headers(token),
        json={"channel": slack_channel(), "blocks": blocks},
        timeout=60,
    )
    data = r.json()
    if not data.get("ok"):
        hint = slack_error_hint(data)
        raise RuntimeError(f"CEO Slack summary failed: {data}{hint}")


def decompose_idea(idea: str, decision_log: list[dict[str, Any]]) -> dict[str, Any]:
    """LLM #1: break startup idea into focused tasks — not hardcoded."""
    system = (
        "You are the CEO of a micro-startup. Given a startup idea, produce strictly valid JSON with keys "
        "product, engineer, marketing — each an object with a single string field 'focus' describing what "
        "that function should deliver (specific, actionable). Do not hardcode generic filler; tailor to the idea."
    )
    user = f"Startup idea:\n{idea}\n"
    out = call_llm_json("ceo", system, user)
    for k in ("product", "engineer", "marketing"):
        if k not in out or "focus" not in out[k]:
            raise ValueError(f"CEO decomposition missing {k}.focus: {out}")
    decision_log.append(
        {
            "decision": "decompose_idea",
            "reasoning": "Generated role-specific task focuses from the idea via LLM.",
            "output": out,
        }
    )
    return out


def review_product_spec(spec: dict[str, Any], decision_log: list[dict[str, Any]]) -> tuple[bool, str]:
    """LLM #2 style: CEO reviews product spec depth (minimum collaboration loop)."""
    system = (
        "You review a product specification. Return strictly valid JSON: "
        "{ \"acceptable\": boolean, \"feedback\": string, \"reasoning\": string }. "
        "Accept only if personas have concrete pain points, features are ordered with clear priorities, "
        "and user stories are testable. If vague, set acceptable false with specific feedback."
    )
    user = f"Product spec JSON:\n{json.dumps(spec, indent=2)}\n"
    res = call_llm_json("ceo", system, user)
    acceptable = bool(res.get("acceptable"))
    feedback = res.get("feedback") or ""
    decision_log.append(
        {
            "decision": "review_product_spec",
            "acceptable": acceptable,
            "reasoning": res.get("reasoning", ""),
            "feedback": feedback,
        }
    )
    return acceptable, feedback


def review_engineer_delivery(
    payload: dict[str, Any],
    decision_log: list[dict[str, Any]],
) -> tuple[bool, str]:
    system = (
        "You are the CEO reviewing engineering delivery. Return JSON "
        "{ \"acceptable\": boolean, \"feedback\": string, \"reasoning\": string }. "
        "Check that PR URL exists, HTML is non-trivial, and aligns with the product value proposition summary."
    )
    user = json.dumps(
        {
            "pr_url": payload.get("pr_url"),
            "issue_url": payload.get("issue_url"),
            "value_proposition": (payload.get("product_spec") or {}).get("value_proposition"),
            "html_preview": (payload.get("html") or "")[:3000],
        },
        indent=2,
    )
    res = call_llm_json("ceo", system, user)
    acceptable = bool(res.get("acceptable"))
    feedback = res.get("feedback") or ""
    decision_log.append(
        {
            "decision": "review_engineer_delivery",
            "acceptable": acceptable,
            "reasoning": res.get("reasoning", ""),
            "feedback": feedback,
        }
    )
    return acceptable, feedback


def review_marketing_delivery(
    copy_payload: dict[str, Any],
    decision_log: list[dict[str, Any]],
) -> tuple[bool, str]:
    system = (
        "You are the CEO reviewing marketing output. Return JSON "
        "{ \"acceptable\": boolean, \"feedback\": string, \"reasoning\": string }. "
        "Tagline must be under 10 words, tone professional, email should have a clear CTA."
    )
    user = json.dumps(copy_payload, indent=2)
    res = call_llm_json("ceo", system, user)
    acceptable = bool(res.get("acceptable"))
    feedback = res.get("feedback") or ""
    decision_log.append(
        {
            "decision": "review_marketing_delivery",
            "acceptable": acceptable,
            "reasoning": res.get("reasoning", ""),
            "feedback": feedback,
        }
    )
    return acceptable, feedback


