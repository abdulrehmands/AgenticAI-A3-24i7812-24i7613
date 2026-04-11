"""Product agent — product spec from CEO task, JSON to Engineer + Marketing, confirmation to CEO."""
from __future__ import annotations

import traceback
from typing import Any

from scripts.llm_client import call_llm_json
from scripts.message_bus import Bus


def _validate_spec(spec: dict[str, Any]) -> None:
    for k in ("value_proposition", "personas", "features", "user_stories"):
        if k not in spec:
            raise ValueError(f"product spec missing {k}")
    if not isinstance(spec["personas"], list) or len(spec["personas"]) < 2:
        raise ValueError("need at least 2 personas")
    for p in spec["personas"]:
        for f in ("name", "role", "pain_point"):
            if f not in p:
                raise ValueError(f"persona missing {f}")
    feats = spec["features"]
    if not isinstance(feats, list) or len(feats) != 5:
        raise ValueError("need exactly 5 features")
    for f in feats:
        for k in ("name", "description", "priority"):
            if k not in f:
                raise ValueError(f"feature missing {k}")
    stories = spec["user_stories"]
    if not isinstance(stories, list) or len(stories) != 3:
        raise ValueError("need exactly 3 user stories")


def handle_product_inbox(bus: Bus) -> dict[str, Any] | None:
    """
    Process all pending messages for the product agent.
    Returns the product_spec dict if work was done, else None.
    """
    incoming = bus.drain_for("product")
    if not incoming:
        return None

    idea: str | None = None
    focus: str | None = None
    revision_feedback: str | None = None
    parent_id: str | None = None

    for m in incoming:
        if m["from_agent"] != "ceo":
            continue
        parent_id = m["message_id"]
        if m["message_type"] == "task":
            idea = m["payload"].get("idea") or idea
            focus = m["payload"].get("focus") or focus
        elif m["message_type"] == "revision_request":
            revision_feedback = m["payload"].get("feedback") or revision_feedback
            idea = m["payload"].get("idea") or idea

    if not idea:
        bus.send(
            "product",
            "ceo",
            "result",
            {"error": "product agent received no idea from CEO", "incoming": len(incoming)},
            parent_message_id=parent_id,
        )
        return None

    system = (
        "You are an expert product manager. Output strictly valid JSON matching the schema. "
        "Priorities: integer 1 = highest through 5 = lowest for five features. "
        "User stories must follow: As a [user], I want to [action] so that [benefit]."
    )
    user = (
        f"Startup idea:\n{idea}\n\nCEO focus for product work:\n{focus or 'Define personas, features, and stories.'}\n"
    )
    if revision_feedback:
        user += f"\nRevise the spec addressing this CEO feedback:\n{revision_feedback}\n"

    user += """
Return a JSON object with exactly these keys:
- value_proposition: string, one sentence
- personas: array of 2-3 objects with name, role, pain_point (strings)
- features: array of exactly 5 objects with name, description, priority (number 1-5, 1=highest)
- user_stories: array of exactly 3 strings in standard user story format
"""

    try:
        spec = call_llm_json("product", system, user)
        _validate_spec(spec)
    except Exception as e:
        tb = traceback.format_exc()
        bus.send(
            "product",
            "ceo",
            "failure",
            {
                "error": str(e),
                "error_type": type(e).__name__,
                "stage": "product_spec",
                "traceback_tail": tb[-800:] if len(tb) > 800 else tb,
            },
            parent_message_id=parent_id,
        )
        print(f"[Product] Reported failure to CEO: {type(e).__name__}: {e}")
        return None

    bus.send(
        "product",
        "engineer",
        "result",
        {"product_spec": spec, "idea": idea},
        parent_message_id=parent_id,
    )
    bus.send(
        "product",
        "marketing",
        "result",
        {"product_spec": spec, "idea": idea},
        parent_message_id=parent_id,
    )
    bus.send(
        "product",
        "ceo",
        "confirmation",
        {
            "status": "spec_ready",
            "summary": spec["value_proposition"][:200],
            "product_spec": spec,
        },
        parent_message_id=parent_id,
    )

    print("[Product] Spec generated and sent to engineer, marketing, and CEO.")
    return spec
