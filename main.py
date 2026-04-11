#!/usr/bin/env python3
"""
LaunchMind — single entry point: CEO-orchestrated multi-agent startup run.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from agents.engineer_agent import handle_engineer_inbox
from agents.marketing_agent import handle_marketing_inbox
from agents.product_agent import handle_product_inbox
from agents.qa_agent import handle_qa_inbox
from agents.ceo_agent import (
    decompose_idea,
    post_slack_ceo_summary,
    review_engineer_delivery,
    review_marketing_delivery,
    review_product_spec,
)
from scripts.message_bus import get_message_bus


def _idea() -> str:
    if len(sys.argv) > 1:
        return " ".join(sys.argv[1:]).strip()
    env = os.environ.get("STARTUP_IDEA")
    if env:
        return env.strip()
    return (
        "A tool that auto-generates cold emails for sales teams based on LinkedIn profiles: "
        "it uses role, company, and profile signals to draft personalized first-touch outreach "
        "so reps save time and avoid generic blast copy."
    )


def _print_bus(bus: Any, label: str) -> None:
    print(f"\n--- {label} ---\n{bus.pretty_log()}\n")


def _enable_qa() -> bool:
    return os.environ.get("ENABLE_QA", "true").lower() in ("1", "true", "yes")


def _max_ceo_attempts(env_key: str, default: int) -> int:
    """Max Product/Engineer/Marketing passes (initial + revisions). Must be >= 1."""
    raw = os.environ.get(env_key, str(default))
    try:
        n = int(raw.strip())
    except ValueError:
        return default
    return max(1, n)


def run() -> None:
    idea = _idea()
    bus = get_message_bus()
    decision_log: list[dict[str, Any]] = []

    print("=" * 60)
    print("LaunchMind — startup idea:")
    print(idea)
    print("=" * 60)

    # --- CEO: decompose (LLM #1) ---
    task_focus = decompose_idea(idea, decision_log)
    print("[CEO] Task decomposition:", task_focus)

    max_product = _max_ceo_attempts("LAUNCHMIND_MAX_PRODUCT_ATTEMPTS", 4)
    max_engineer = _max_ceo_attempts("LAUNCHMIND_MAX_ENGINEER_ATTEMPTS", 5)
    max_marketing = _max_ceo_attempts("LAUNCHMIND_MAX_MARKETING_ATTEMPTS", 3)
    print(
        f"[CEO] Review attempt limits: product={max_product}, engineer={max_engineer}, marketing={max_marketing}"
    )

    # --- Product loop (CEO review = collaboration loop #1) ---
    product_spec = None
    for attempt in range(max_product):
        if attempt == 0:
            bus.send(
                "ceo",
                "product",
                "task",
                {
                    "idea": idea,
                    "focus": task_focus["product"]["focus"],
                },
            )
            _print_bus(bus, "After CEO→Product task")
        handle_product_inbox(bus)

        fail = bus.pop_matching("ceo", from_agent="product", message_type="failure")
        if fail:
            pl = fail["payload"]
            print(
                f"[CEO] Agent failure (product): {pl.get('error_type', 'Error')}: {pl.get('error', '')}"
            )
            decision_log.append(
                {
                    "event": "agent_failure",
                    "agent": "product",
                    "error_type": pl.get("error_type"),
                    "error": pl.get("error"),
                    "stage": pl.get("stage"),
                    "message_id": fail.get("message_id"),
                }
            )
            if attempt + 1 >= max_product:
                raise RuntimeError(
                    "Product agent exhausted attempts after failure(s); see decision_log / CEO inbox"
                )
            bus.send(
                "ceo",
                "product",
                "task",
                {
                    "idea": idea,
                    "focus": task_focus["product"]["focus"],
                },
            )
            _print_bus(bus, "CEO->Product retry after failure")
            continue

        conf = bus.pop_matching("ceo", from_agent="product", message_type="confirmation")
        if not conf:
            raise RuntimeError("Expected product confirmation to CEO")
        product_spec = conf["payload"]["product_spec"]
        ok, fb = review_product_spec(product_spec, decision_log)
        print(f"[CEO] Product spec review acceptable={ok}")
        if ok:
            break
        bus.send(
            "ceo",
            "product",
            "revision_request",
            {"feedback": fb, "idea": idea},
            parent_message_id=conf["message_id"],
        )
        _print_bus(bus, "CEO revision_request → Product")
    else:
        raise RuntimeError("Product spec never accepted")

    # --- Engineer loop (CEO review; optional QA revision) ---
    eng_payload = None
    for attempt in range(max_engineer):
        if attempt == 0:
            bus.send(
                "ceo",
                "engineer",
                "task",
                {"idea": idea, "focus": task_focus["engineer"]["focus"]},
            )
            _print_bus(bus, "CEO→Engineer task")
        handle_engineer_inbox(bus)

        res = bus.pop_matching("ceo", from_agent="engineer", message_type="result")
        if not res or res["payload"].get("error"):
            raise RuntimeError(f"Engineer failed: {res}")
        eng_payload = res["payload"]
        ok, fb = review_engineer_delivery(eng_payload, decision_log)
        print(f"[CEO] Engineer review acceptable={ok}")
        if ok:
            break
        bus.send(
            "ceo",
            "engineer",
            "revision_request",
            {
                "feedback": fb,
                "existing_github": {
                    "branch": eng_payload.get("branch"),
                    "pr_url": eng_payload.get("pr_url"),
                    "pr_number": eng_payload.get("pr_number"),
                    "issue_url": eng_payload.get("issue_url"),
                    "repo": eng_payload.get("repo"),
                },
                "product_spec": eng_payload.get("product_spec"),
                "idea": eng_payload.get("idea", idea),
            },
            parent_message_id=res["message_id"],
        )
    else:
        raise RuntimeError("Engineer delivery never accepted")

    pr_url = eng_payload["pr_url"]

    # --- Marketing (requires Product message already in marketing inbox + CEO pr_url task) ---
    bus.send(
        "ceo",
        "marketing",
        "task",
        {
            "idea": idea,
            "focus": task_focus["marketing"]["focus"],
            "pr_url": pr_url,
        },
    )
    _print_bus(bus, "CEO→Marketing task (with pr_url)")
    if handle_marketing_inbox(bus) is None:
        raise RuntimeError("Marketing did not complete (missing spec or pr_url)")

    mres = bus.pop_matching("ceo", from_agent="marketing", message_type="result")
    if not mres:
        raise RuntimeError("Expected marketing result to CEO")
    marketing_copy = mres["payload"]

    for attempt in range(max_marketing):
        ok, fb = review_marketing_delivery(marketing_copy, decision_log)
        print(f"[CEO] Marketing review acceptable={ok}")
        if ok:
            break
        bus.send(
            "ceo",
            "marketing",
            "revision_request",
            {
                "feedback": fb,
                "product_spec": product_spec,
                "idea": idea,
                "pr_url": pr_url,
            },
            parent_message_id=mres["message_id"],
        )
        handle_marketing_inbox(bus)
        mres = bus.pop_matching("ceo", from_agent="marketing", message_type="result")
        if not mres:
            raise RuntimeError("Missing marketing result after revision")
        marketing_copy = mres["payload"]
    else:
        raise RuntimeError("Marketing never accepted")

    # --- QA (optional / 3-person) ---
    if _enable_qa():
        bus.send(
            "ceo",
            "qa",
            "task",
            {
                "product_spec": product_spec,
                "html": eng_payload.get("html", ""),
                "marketing_copy": marketing_copy,
                "pr_url": pr_url,
                "pr_number": eng_payload.get("pr_number"),
                "repo": eng_payload.get("repo"),
            },
        )
        _print_bus(bus, "CEO→QA task")
        handle_qa_inbox(bus)
        qa_msg = bus.pop_matching("ceo", from_agent="qa", message_type="result")
        if qa_msg and not qa_msg["payload"].get("error"):
            verdict = (qa_msg["payload"].get("verdict") or "fail").lower()
            print(f"[CEO] QA verdict={verdict}")
            if verdict == "fail":
                bus.send(
                    "ceo",
                    "engineer",
                    "revision_request",
                    {
                        "feedback": "QA: " + "; ".join(qa_msg["payload"].get("html_issues") or ["fix HTML gaps"]),
                        "existing_github": {
                            "branch": eng_payload.get("branch"),
                            "pr_url": eng_payload.get("pr_url"),
                            "pr_number": eng_payload.get("pr_number"),
                            "issue_url": eng_payload.get("issue_url"),
                            "repo": eng_payload.get("repo"),
                        },
                        "product_spec": product_spec,
                        "idea": idea,
                    },
                )
                handle_engineer_inbox(bus)
                eng2 = bus.pop_matching("ceo", from_agent="engineer", message_type="result")
                if eng2:
                    eng_payload = eng2["payload"]
                    pr_url = eng_payload.get("pr_url", pr_url)

    # --- CEO final Slack summary ---
    sections = [
        ("Idea", idea[:500]),
        ("Value proposition", product_spec.get("value_proposition", "")),
        ("Engineering", f"PR: {pr_url}"),
        ("Marketing tagline", marketing_copy.get("tagline", "")),
        ("Decisions", json.dumps(decision_log, indent=2, default=str)),
    ]
    post_slack_ceo_summary("LaunchMind — CEO final summary", sections)
    print("[CEO] Posted final summary to Slack.")

    print("\n" + "=" * 60)
    print("CEO SENT / RECEIVED (full JSON for each message touching CEO)")
    print("=" * 60)
    for m in bus.history_for_agent("ceo"):
        print(json.dumps(m, indent=2))
        print("---")

    print("\n" + "=" * 60)
    print("DECISION LOG (JSON-serializable)")
    print("=" * 60)
    print(json.dumps(decision_log, indent=2))
    _print_bus(bus, "FULL MESSAGE HISTORY")


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        print("FATAL:", e, file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)
