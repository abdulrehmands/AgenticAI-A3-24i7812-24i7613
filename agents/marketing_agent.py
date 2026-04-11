"""Marketing agent — LLM copy, SendGrid email, Slack Block Kit launch post."""
from __future__ import annotations

import os
from typing import Any

import requests
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

from scripts.llm_client import call_llm_json
from scripts.message_bus import Bus
from scripts.slack_util import slack_channel, slack_error_hint, slack_json_headers


def _generate_copy(
    product_spec: dict[str, Any],
    idea: str,
    pr_url: str,
    *,
    revision_feedback: str | None = None,
) -> dict[str, Any]:
    system = (
        """
        1. You are a growth marketer. Output strictly valid JSON. 
        2. Tagline must be under 10 words. landing_description is 2-3 sentences. 
        3. Cold_email_subject and cold_email_body are for a single outreach email (body can be plain text or 1-2 lines of HTML-friendly paragraphs). Use our Company name as lunachmind-warriors
        4. Social_twitter, social_linkedin, social_instagram are draft posts.
        5. Social_twitter, social_linkedin, social_instagram are draft posts.
        6. use the general greetings Hey There! or either generate the greeting as it does not looks smapy.
        """

    )
    user = (
        f"Idea:\n{idea}\n\nProduct spec:\n{product_spec}\n\nGitHub PR (mention if useful): {pr_url}\n"
    )
    if revision_feedback:
        user += f"\nCEO revision feedback (address this):\n{revision_feedback}\n"
    user += (
        "Return JSON keys: tagline, landing_description, cold_email_subject, cold_email_body, "
        "social_twitter, social_linkedin, social_instagram."
    )
    return call_llm_json("marketing", system, user)


def _send_email(subject: str, body: str, to_email: str) -> None:
    from_email = os.environ["SENDGRID_FROM_EMAIL"]
    key = os.environ["SENDGRID_API_KEY"]
    message = Mail(
        from_email=from_email,
        to_emails=to_email,
        subject=subject,
        html_content=f"<div>{body.replace(chr(10), '<br/>')}</div>",
    )
    sg = SendGridAPIClient(key)
    try:
        sg.send(message)
    except Exception as e:
        err_body = getattr(e, "body", None)
        if err_body:
            print(f"[Marketing] SendGrid response: {err_body}")
        raise


def _post_slack_blocks(tagline: str, one_line: str, pr_url: str) -> None:
    token = os.environ["SLACK_BOT_TOKEN"]
    channel = slack_channel()
    payload = {
        "channel": channel,
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"New Launch: {tagline}", "emoji": True},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": one_line},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*GitHub PR:* <{pr_url}|View PR>"},
                    {"type": "mrkdwn", "text": "*Status:* Ready for review"},
                ],
            },
        ],
    }
    r = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers=slack_json_headers(token),
        json=payload,
        timeout=60,
    )
    data = r.json()
    if not data.get("ok"):
        hint = slack_error_hint(data)
        raise RuntimeError(f"Slack post failed: {data}{hint}")


def handle_marketing_inbox(bus: Bus) -> dict[str, Any] | None:
    queued = bus.peek("marketing")
    if not queued:
        return None

    product_spec: dict[str, Any] | None = None
    idea: str | None = None
    pr_url: str | None = None
    marketing_focus: str | None = None
    revision_feedback: str | None = None
    parent_id: str | None = None

    for m in queued:
        pl = m.get("payload") or {}
        if m["from_agent"] == "product" and "product_spec" in pl:
            product_spec = pl["product_spec"]
            idea = pl.get("idea") or idea
            parent_id = m["message_id"]
        if m["from_agent"] == "ceo":
            parent_id = m["message_id"]
            if m["message_type"] == "task":
                pr_url = pl.get("pr_url") or pr_url
                marketing_focus = pl.get("focus") or marketing_focus
                idea = pl.get("idea") or idea
            if m["message_type"] == "revision_request":
                product_spec = pl.get("product_spec") or product_spec
                pr_url = pl.get("pr_url") or pr_url
                idea = pl.get("idea") or idea
                revision_feedback = pl.get("feedback") or revision_feedback

    if not product_spec or not idea:
        return None
    if not pr_url:
        print("[Marketing] Waiting for pr_url from CEO before email/Slack...")
        return None

    bus.drain_for("marketing")

    print("[Marketing] Generating copy...")
    copy_out = _generate_copy(
        product_spec,
        idea + (f"\nFocus: {marketing_focus}" if marketing_focus else ""),
        pr_url,
        revision_feedback=revision_feedback,
    )
    tagline = copy_out.get("tagline") or "Your next favorite product"
    one_line = copy_out.get("landing_description") or tagline
    subject = copy_out.get("cold_email_subject") or "Quick intro"
    body = copy_out.get("cold_email_body") or one_line

    to_email = os.environ.get("MARKETING_TO_EMAIL") or os.environ["SENDGRID_FROM_EMAIL"]
    print("[Marketing] Sending email...")
    _send_email(subject, body, to_email)

    print("[Marketing] Posting Slack Block Kit message...")
    _post_slack_blocks(tagline, one_line, pr_url)

    payload = {
        "tagline": tagline,
        "landing_description": one_line,
        "cold_email_subject": subject,
        "cold_email_body": body,
        "social_twitter": copy_out.get("social_twitter", ""),
        "social_linkedin": copy_out.get("social_linkedin", ""),
        "social_instagram": copy_out.get("social_instagram", ""),
        "pr_url": pr_url,
    }
    bus.send(
        "marketing",
        "ceo",
        "result",
        payload,
        parent_message_id=parent_id,
    )
    print("[Marketing] Done — email sent, Slack posted, result sent to CEO.")
    return payload
