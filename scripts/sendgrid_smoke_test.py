#!/usr/bin/env python3
"""One-off SendGrid mail send using .env (same vars as marketing_agent)."""
from __future__ import annotations

import os
import sys

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail


def main() -> int:
    key = os.environ.get("SENDGRID_API_KEY")
    from_email = os.environ.get("SENDGRID_FROM_EMAIL")
    if not key or not from_email:
        print("Missing SENDGRID_API_KEY or SENDGRID_FROM_EMAIL in environment / .env")
        return 1
    to_email = os.environ.get("MARKETING_TO_EMAIL") or from_email

    message = Mail(
        from_email=from_email,
        to_emails=to_email,
        subject="LaunchMind SendGrid smoke test",
        html_content="<p>This is a test message from <code>scripts/sendgrid_smoke_test.py</code>.</p>",
    )
    try:
        sg = SendGridAPIClient(key)
        sg.send(message)
    except Exception as e:
        print("Send failed:", type(e).__name__, e)
        body = getattr(e, "body", None)
        if body is not None:
            print("Response body:", body)
        return 1

    print("OK: mail accepted by SendGrid (check inbox/spam for", to_email + ")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
