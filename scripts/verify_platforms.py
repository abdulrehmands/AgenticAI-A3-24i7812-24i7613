#!/usr/bin/env python3
"""Smoke-test GitHub, Slack, SendGrid, and OpenAI credentials before running agents."""
from __future__ import annotations

import os
import sys

import requests

# Load .env if python-dotenv available
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def check_github() -> bool:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("SKIP GitHub: GITHUB_TOKEN not set")
        return False
    r = requests.get(
        "https://api.github.com/user",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=30,
    )
    ok = r.status_code == 200
    print(f"GitHub API: {'OK' if ok else r.status_code} {r.text[:200] if not ok else r.json().get('login', '')}")
    return ok


def check_slack() -> bool:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        print("SKIP Slack: SLACK_BOT_TOKEN not set")
        return False
    r = requests.post(
        "https://slack.com/api/auth.test",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    data = r.json()
    ok = data.get("ok")
    print(f"Slack auth.test: {'OK' if ok else data}")
    return bool(ok)


def check_sendgrid() -> bool:
    key = os.environ.get("SENDGRID_API_KEY")
    if not key:
        print("SKIP SendGrid: SENDGRID_API_KEY not set")
        return False
    r = requests.get(
        "https://api.sendgrid.com/v3/scopes",
        headers={"Authorization": f"Bearer {key}"},
        timeout=30,
    )
    ok = r.status_code == 200
    print(f"SendGrid scopes: {'OK' if ok else r.status_code}")
    return ok


def check_openai() -> bool:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("SKIP OpenAI: OPENAI_API_KEY not set")
        return False
    r = requests.get(
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {key}"},
        timeout=30,
    )
    ok = r.status_code == 200
    print(f"OpenAI models list: {'OK' if ok else r.status_code}")
    return ok


def main() -> int:
    print("LaunchMind platform checks\n")
    results = [
        check_github(),
        check_slack(),
        check_sendgrid(),
        check_openai(),
    ]
    if not any(results):
        print("\nNo credentials configured. Copy .env.example to .env and fill values.")
        return 1
    print("\nDone. Fix any failures above before running main.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
