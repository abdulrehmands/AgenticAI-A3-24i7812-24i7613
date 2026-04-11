"""QA agent — LLM review of HTML + marketing copy; inline GitHub PR comments; report to CEO."""
from __future__ import annotations

import base64
import os
from typing import Any

import requests

from scripts.llm_client import call_llm_json
from scripts.message_bus import Bus

GITHUB_API = "https://api.github.com"


def _headers() -> dict[str, str]:
    token = os.environ["GITHUB_TOKEN"]
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _pr_head_sha(repo: str, pr_number: int) -> str:
    r = requests.get(
        f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}",
        headers=_headers(),
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["head"]["sha"]


def _fetch_index_html_lines(repo: str, ref: str) -> list[str]:
    r = requests.get(
        f"{GITHUB_API}/repos/{repo}/contents/index.html",
        headers=_headers(),
        params={"ref": ref},
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    raw = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    return raw.splitlines()


def _post_inline_comment(
    repo: str,
    pr_number: int,
    commit_id: str,
    path: str,
    line: int,
    body: str,
) -> None:
    url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/comments"
    payload = {
        "body": body,
        "commit_id": commit_id,
        "path": path,
        "line": line,
        "side": "RIGHT",
    }
    r = requests.post(url, headers=_headers(), json=payload, timeout=60)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Inline comment failed {r.status_code}: {r.text}")


def _review_llm(product_spec: dict[str, Any], html: str, marketing: dict[str, Any]) -> dict[str, Any]:
    system = (
        "You are a strict QA lead. Return valid JSON with keys: "
        "verdict ('pass' or 'fail'), html_issues (array of strings), marketing_issues (array of strings), "
        "inline_comment_1 (short string for GitHub), inline_comment_2 (short string for GitHub). "
        "Fail if headline does not reflect value proposition, if major features are missing from the page, "
        "or if marketing email lacks a clear CTA."
    )
    user = (
        f"Product spec:\n{product_spec}\n\nHTML:\n{html[:8000]}\n\nMarketing copy:\n{marketing}\n"
    )
    return call_llm_json("qa", system, user)


def handle_qa_inbox(bus: Bus) -> dict[str, Any] | None:
    incoming = bus.drain_for("qa")
    if not incoming:
        return None

    merged: dict[str, Any] = {}
    parent_id: str | None = None
    for m in incoming:
        if m["from_agent"] != "ceo":
            continue
        parent_id = m["message_id"]
        pl = m.get("payload") or {}
        merged.update(pl)

    product_spec = merged.get("product_spec")
    html = merged.get("html")
    marketing_copy = merged.get("marketing_copy")
    pr_number = merged.get("pr_number")
    repo = merged.get("repo")

    if not all([product_spec, html, marketing_copy, pr_number, repo]):
        bus.send(
            "qa",
            "ceo",
            "result",
            {"error": "qa missing required fields", "had": list(merged.keys())},
            parent_message_id=parent_id,
        )
        return None

    print("[QA] Running LLM review...")
    review = _review_llm(product_spec, html, marketing_copy)
    verdict = (review.get("verdict") or "fail").lower()
    if verdict not in ("pass", "fail"):
        verdict = "fail"

    head_sha = _pr_head_sha(repo, int(pr_number))
    lines = _fetch_index_html_lines(repo, head_sha)
    n = len(lines)
    if n >= 2:
        pick_lines = [1, min(5, n)]
    elif n == 1:
        pick_lines = [1, 1]
    else:
        pick_lines = [1, 1]

    c1 = review.get("inline_comment_1") or "Check headline alignment with value proposition."
    c2 = review.get("inline_comment_2") or "Verify all prioritized features are represented."
    print("[QA] Posting inline PR comments...")
    _post_inline_comment(repo, int(pr_number), head_sha, "index.html", pick_lines[0], c1)
    _post_inline_comment(repo, int(pr_number), head_sha, "index.html", pick_lines[1], c2)

    report = {
        "verdict": verdict,
        "html_issues": review.get("html_issues") or [],
        "marketing_issues": review.get("marketing_issues") or [],
        "raw_review": review,
    }
    bus.send("qa", "ceo", "result", report, parent_message_id=parent_id)
    print(f"[QA] Report sent to CEO (verdict={verdict}).")
    return report
