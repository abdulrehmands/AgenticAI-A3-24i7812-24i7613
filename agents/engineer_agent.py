"""Engineer agent — HTML landing page + GitHub issue, branch, commit, PR."""
from __future__ import annotations

import base64
import os
import re
from typing import Any

import requests

from scripts.llm_client import call_llm, call_llm_json
from scripts.message_bus import Bus

GITHUB_API = "https://api.github.com"
AUTHOR_NAME = "EngineerAgent"
AUTHOR_EMAIL = "agent@launchmind.ai"
ISSUE_TITLE = "Initial landing page"


def _headers() -> dict[str, str]:
    token = os.environ["GITHUB_TOKEN"]
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _repo() -> str:
    repo = os.environ.get("GITHUB_REPO")
    if not repo or "/" not in repo:
        raise RuntimeError("GITHUB_REPO must be set as owner/name")
    return repo


def _branch_name() -> str:
    return os.environ.get("ENGINEER_BRANCH", "agent-landing-page")


def _generate_html(product_spec: dict[str, Any], idea: str) -> str:
    system = (
        "You are an expert front-end developer. Produce a single complete HTML5 document "
        "with embedded CSS in <style>. Include headline, subheadline, a features section "
        "reflecting the product spec, and a visible call-to-action button. No external assets."
    )
    user = f"Startup idea:\n{idea}\n\nProduct spec JSON:\n{product_spec}\n\nOutput only raw HTML, no markdown fences."
    return call_llm("engineer", system, user)


def _generate_issue_body(product_spec: dict[str, Any], idea: str) -> str:
    system = "You write concise GitHub issue descriptions in Markdown."
    user = f"Issue title: {ISSUE_TITLE}\n\nIdea:\n{idea}\n\nSpec summary:\n{product_spec}\n\nWrite a 2-4 paragraph issue body."
    return call_llm("engineer", system, user)


def _generate_pr_content(product_spec: dict[str, Any], html_preview: str) -> dict[str, str]:
    system = "Return strictly valid JSON with keys title (string) and body (markdown string) for a GitHub pull request."
    user = (
        f"Product value proposition: {product_spec.get('value_proposition', '')}\n"
        f"Landing page first 500 chars of HTML:\n{html_preview[:500]}\n"
    )
    return call_llm_json("engineer", system, user)


def _get_main_sha(repo: str) -> str:
    default_branch = os.environ.get("GITHUB_DEFAULT_BRANCH", "main")
    r = requests.get(
        f"{GITHUB_API}/repos/{repo}/git/ref/heads/{default_branch}",
        headers=_headers(),
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["object"]["sha"]


def _create_branch(repo: str, branch: str, sha: str) -> None:
    r = requests.post(
        f"{GITHUB_API}/repos/{repo}/git/refs",
        headers=_headers(),
        json={"ref": f"refs/heads/{branch}", "sha": sha},
        timeout=60,
    )
    if r.status_code == 422 and "already exists" in r.text:
        return
    r.raise_for_status()


def _create_issue(repo: str, body: str) -> dict[str, Any]:
    r = requests.post(
        f"{GITHUB_API}/repos/{repo}/issues",
        headers=_headers(),
        json={"title": ISSUE_TITLE, "body": body},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def _get_file_sha(repo: str, branch: str, path: str) -> str | None:
    r = requests.get(
        f"{GITHUB_API}/repos/{repo}/contents/{path}",
        headers=_headers(),
        params={"ref": branch},
        timeout=60,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json().get("sha")


def _put_index_html(
    repo: str,
    branch: str,
    html: str,
    message: str,
    *,
    file_sha: str | None = None,
) -> dict[str, Any]:
    content = base64.b64encode(html.encode("utf-8")).decode("ascii")
    payload: dict[str, Any] = {
        "message": message,
        "content": content,
        "branch": branch,
        "author": {"name": AUTHOR_NAME, "email": AUTHOR_EMAIL},
        "committer": {"name": AUTHOR_NAME, "email": AUTHOR_EMAIL},
    }
    if file_sha:
        payload["sha"] = file_sha
    r = requests.put(
        f"{GITHUB_API}/repos/{repo}/contents/index.html",
        headers=_headers(),
        json=payload,
        timeout=120,
    )
    r.raise_for_status()
    return r.json()


def _open_pr_for_head(repo: str, branch: str, base: str) -> dict[str, Any] | None:
    """Return an open PR from branch -> base if one exists (GitHub head filter is owner:branch)."""
    owner = repo.split("/")[0]
    r = requests.get(
        f"{GITHUB_API}/repos/{repo}/pulls",
        headers=_headers(),
        params={"head": f"{owner}:{branch}", "state": "open", "base": base, "per_page": 10},
        timeout=60,
    )
    r.raise_for_status()
    for pr in r.json():
        if pr.get("base", {}).get("ref") == base and pr.get("head", {}).get("ref") == branch:
            return pr
    return None


def _create_pr(repo: str, branch: str, title: str, body: str, base: str) -> dict[str, Any]:
    r = requests.post(
        f"{GITHUB_API}/repos/{repo}/pulls",
        headers=_headers(),
        json={"title": title, "body": body, "head": branch, "base": base},
        timeout=60,
    )
    if r.status_code == 422:
        try:
            data = r.json()
        except Exception:
            r.raise_for_status()
        err_blob = " ".join(
            str(e.get("message", "")) if isinstance(e, dict) else str(e)
            for e in (data.get("errors") or [])
        )
        combined = f"{data.get('message', '')} {err_blob}".lower()
        if "already exists" in combined:
            existing = _open_pr_for_head(repo, branch, base)
            if existing:
                return existing
        msg = f"GitHub PR create failed (422): {data.get('message')} — {data.get('errors', data)}"
        raise requests.HTTPError(msg, response=r) from None
    r.raise_for_status()
    return r.json()


def _parse_pr_number(pr_url: str) -> int:
    m = re.search(r"/pull/(\d+)", pr_url)
    if not m:
        raise ValueError(f"Cannot parse PR number from {pr_url}")
    return int(m.group(1))


def handle_engineer_inbox(bus: Bus) -> dict[str, Any] | None:
    incoming = bus.drain_for("engineer")
    if not incoming:
        return None

    product_spec: dict[str, Any] | None = None
    idea: str | None = None
    ceo_focus: str | None = None
    revision_feedback: str | None = None
    existing: dict[str, Any] = {}
    parent_id: str | None = None
    revision_requested = False

    for m in incoming:
        pl = m.get("payload") or {}
        if m["from_agent"] == "product" and "product_spec" in pl:
            product_spec = pl["product_spec"]
            idea = pl.get("idea") or idea
            parent_id = m["message_id"]
        if m["from_agent"] == "ceo":
            parent_id = m["message_id"]
            if m["message_type"] == "task":
                ceo_focus = pl.get("focus") or ceo_focus
                idea = pl.get("idea") or idea
            elif m["message_type"] == "revision_request":
                revision_requested = True
                revision_feedback = pl.get("feedback") or "Revise the landing page per review."
                existing = pl.get("existing_github", {}) or {}
                product_spec = pl.get("product_spec") or product_spec
                idea = pl.get("idea") or idea

    if not product_spec or not idea:
        bus.send(
            "engineer",
            "ceo",
            "result",
            {"error": "engineer missing product_spec or idea", "received": len(incoming)},
            parent_message_id=parent_id,
        )
        return None

    repo = _repo()
    base_branch = os.environ.get("GITHUB_DEFAULT_BRANCH", "main")
    branch = existing.get("branch") or _branch_name()
    is_revision = revision_requested and bool(existing.get("branch"))

    print(f"[Engineer] Generating HTML (revision={is_revision})...")
    extra = ""
    if ceo_focus:
        extra += f"\n\nEngineer focus: {ceo_focus}"
    if is_revision and revision_feedback:
        extra += f"\n\nRevision feedback:\n{revision_feedback}"
    html = _generate_html(product_spec, idea + extra)
    if "<html" not in html.lower():
        html = f"<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\"><title>Launch</title></head><body>{html}</body></html>"

    issue_url = existing.get("issue_url")
    if not issue_url:
        issue_body = _generate_issue_body(product_spec, idea)
        issue = _create_issue(repo, issue_body)
        issue_url = issue["html_url"]
        print(f"[Engineer] Created issue: {issue_url}")

    if not is_revision:
        main_sha = _get_main_sha(repo)
        _create_branch(repo, branch, main_sha)
        print(f"[Engineer] Branch {branch} ready.")

    file_sha = _get_file_sha(repo, branch, "index.html")
    commit_msg = "Revise landing page per CEO/QA" if is_revision else "Add landing page"
    _put_index_html(repo, branch, html, commit_msg, file_sha=file_sha)
    print("[Engineer] Committed index.html")

    pr_data = _generate_pr_content(product_spec, html)
    pr_title = pr_data.get("title") or "Initial landing page"
    pr_body = pr_data.get("body") or "Automated landing page from LaunchMind Engineer agent."

    pr_url = existing.get("pr_url")
    pr_number: int | None = existing.get("pr_number")
    if not pr_url:
        pr = _create_pr(repo, branch, pr_title, pr_body, base_branch)
        pr_url = pr["html_url"]
        pr_number = pr["number"]
        print(f"[Engineer] Opened PR: {pr_url}")
    else:
        print(f"[Engineer] Updated existing PR: {pr_url}")

    if pr_number is None:
        pr_number = _parse_pr_number(pr_url)

    bus.send(
        "engineer",
        "ceo",
        "result",
        {
            "issue_url": issue_url,
            "pr_url": pr_url,
            "pr_number": pr_number,
            "branch": branch,
            "repo": repo,
            "html": html,
            "product_spec": product_spec,
            "idea": idea,
        },
        parent_message_id=parent_id,
    )
    return {
        "issue_url": issue_url,
        "pr_url": pr_url,
        "pr_number": pr_number,
        "branch": branch,
        "repo": repo,
        "html": html,
    }
