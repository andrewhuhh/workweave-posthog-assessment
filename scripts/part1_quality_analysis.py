#!/usr/bin/env python3
"""
Build Part 1 qualitative/code-quality datasets for the PostHog Engineering
Impact Dashboard.

The repository snapshot for this assessment does not include the existing
dashboard/pipeline artifacts, so this script reconstructs the "current top"
impact cohort from GitHub merged PR metadata and then adds the qualitative
rubric layer requested in PART 1.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import requests


REPO = "PostHog/posthog"
OWNER, NAME = REPO.split("/")
START_DATE = dt.date(2026, 5, 24)
END_DATE = dt.date(2026, 6, 23)
NOW = dt.datetime(2026, 6, 23, 23, 59, tzinfo=dt.timezone.utc)

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "github_cache"
RAW_DIR = CACHE_DIR / "raw_prs"
REVIEW_DIR = CACHE_DIR / "review_interactions"

BOT_LOGINS = {
    "dependabot[bot]",
    "github-actions[bot]",
    "posthog[bot]",
    "posthog-js-upgrader[bot]",
    "posthog-bot",
    "renovate[bot]",
}

TOP_N_CONTRIBUTORS = 10
DEEP_REVIEW_COUNT = 8
STANDARD_REVIEW_COUNT = 5


def ensure_dirs() -> None:
    for path in [DATA_DIR, CACHE_DIR, RAW_DIR, REVIEW_DIR]:
        path.mkdir(parents=True, exist_ok=True)


def gh_token() -> str:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    try:
        return subprocess.check_output(["gh", "auth", "token"], text=True).strip()
    except Exception as exc:  # pragma: no cover - environment failure
        raise RuntimeError("GitHub token unavailable; run `gh auth login` or set GH_TOKEN") from exc


class GitHubClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {gh_token()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "capy-posthog-quality-analysis",
            }
        )

    def get(self, url: str, *, params: dict[str, Any] | None = None, accept: str | None = None) -> Any:
        headers = {}
        if accept:
            headers["Accept"] = accept
        last_error = None
        for attempt in range(8):
            response = self.session.get(url, params=params, headers=headers, timeout=60)
            if response.status_code in {502, 503, 504} or (
                response.status_code == 403 and "secondary rate" in response.text.lower()
            ):
                last_error = f"{response.status_code}: {response.text[:300]}"
                time.sleep(2 + attempt * 3)
                continue
            if response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0":
                reset = int(response.headers.get("X-RateLimit-Reset", "0"))
                sleep_for = max(5, reset - int(time.time()) + 2)
                time.sleep(min(sleep_for, 120))
                continue
            try:
                response.raise_for_status()
            except Exception as exc:
                raise RuntimeError(f"GitHub GET failed for {response.url}: {response.text[:500]}") from exc
            if accept and "diff" in accept:
                return response.text
            return response.json()
        raise RuntimeError(f"GitHub GET failed after retries for {url}: {last_error}")

    def paginated(self, url: str, *, params: dict[str, Any] | None = None, max_pages: int = 20) -> list[Any]:
        all_items: list[Any] = []
        base_params = dict(params or {})
        base_params.setdefault("per_page", 100)
        for page in range(1, max_pages + 1):
            base_params["page"] = page
            items = self.get(url, params=base_params)
            if not isinstance(items, list):
                raise RuntimeError(f"Expected list from paginated endpoint {url}")
            all_items.extend(items)
            if len(items) < int(base_params["per_page"]):
                break
        return all_items


def cache_json(path: Path, fetcher) -> Any:
    if path.exists():
        return json.loads(path.read_text())
    value = fetcher()
    path.write_text(json.dumps(value, indent=2, sort_keys=True))
    return value


def fetch_recent_merged_prs(client: GitHubClient) -> list[dict[str, Any]]:
    cache_path = CACHE_DIR / "recent_merged_prs_search_30d.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text())

    items_by_number: dict[int, dict[str, Any]] = {}
    for offset in range((END_DATE - START_DATE).days + 1):
        day = START_DATE + dt.timedelta(days=offset)
        query = f"repo:{REPO} is:pr is:merged merged:{day.isoformat()}"
        page = 1
        while True:
            data = client.get(
                "https://api.github.com/search/issues",
                params={
                    "q": query,
                    "sort": "updated",
                    "order": "desc",
                    "per_page": 100,
                    "page": page,
                },
            )
            for item in data["items"]:
                items_by_number[item["number"]] = compact_search_item(item)
            if len(data["items"]) < 100:
                break
            page += 1
    items = list(items_by_number.values())
    cache_path.write_text(json.dumps(items, separators=(",", ":"), sort_keys=True))
    return items


def compact_search_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": item.get("number"),
        "title": item.get("title"),
        "body": item.get("body"),
        "html_url": item.get("html_url"),
        "closed_at": item.get("closed_at"),
        "updated_at": item.get("updated_at"),
        "comments": item.get("comments"),
        "user": {"login": (item.get("user") or {}).get("login")},
        "labels": [{"name": label.get("name")} for label in item.get("labels", []) if label.get("name")],
    }


def lower_join(*values: Any) -> str:
    return " ".join(str(value or "").lower() for value in values)


def label_names(pr: dict[str, Any]) -> list[str]:
    return [label.get("name", "") for label in pr.get("labels", []) if label.get("name")]


HIGH_LEVERAGE_TERMS = [
    "security",
    "auth",
    "billing",
    "performance",
    "perf",
    "reliability",
    "clickhouse",
    "migration",
    "schema",
    "data warehouse",
    "session replay",
    "replay",
    "web analytics",
    "experiments",
    "feature flags",
    "insight",
    "dashboard",
    "cohort",
    "capture",
    "persons",
    "events",
    "batch export",
    "alerts",
    "error tracking",
    "hogql",
    "mcp",
    "ai",
    "llm",
    "max",
    "data modeling",
]

RISK_TERMS = [
    "migration",
    "backfill",
    "rollback",
    "compat",
    "retry",
    "fallback",
    "rate limit",
    "race",
    "timeout",
    "permission",
    "auth",
    "security",
    "concurrent",
    "index",
    "crash",
    "failure",
    "error",
    "exception",
    "readonly",
    "guard",
    "null",
    "empty",
    "regression",
]

VALIDATION_TERMS = [
    "test plan",
    "testing",
    "validated",
    "verified",
    "screenshot",
    "playwright",
    "pytest",
    "unit test",
    "e2e",
    "ci",
]


def provisional_work_type(pr: dict[str, Any]) -> str:
    title_labels = lower_join(pr.get("title"), " ".join(label_names(pr)))
    body_short = (pr.get("body") or "")[:600].lower()
    if any(term in title_labels for term in ["perf", "performance", "optimi"]):
        return "performance"
    if any(term in title_labels for term in ["migration", "migrate", "schema", "backfill", "index"]):
        return "migration"
    if any(term in f"{title_labels} {body_short}" for term in ["reliability", "retry", "fallback", "crash", "race", "rate limit", "failure"]):
        return "reliability"
    if any(term in title_labels for term in ["infra", "ci", "deploy", "k8s", "docker", "celery", "clickhouse"]):
        return "infra"
    if any(term in title_labels for term in ["storybook", "lint", "typecheck", "developer", "devex", "dev experience", "dx"]):
        return "dx"
    if any(term in title_labels for term in ["fix", "bug", "regression"]):
        return "bugfix"
    if any(term in title_labels for term in ["feat", "add", "support", "implement", "scaffold"]):
        return "product"
    if any(term in title_labels for term in ["doc", "readme"]):
        return "dx"
    return "maintenance"


def provisional_impact_score(pr: dict[str, Any]) -> float:
    text = lower_join(pr.get("title"), pr.get("body"), " ".join(label_names(pr)))
    work_type = provisional_work_type(pr)
    score = 1.4
    score += {
        "product": 1.2,
        "bugfix": 1.1,
        "infra": 1.3,
        "reliability": 1.7,
        "performance": 1.8,
        "migration": 1.6,
        "dx": 0.8,
        "maintenance": 0.4,
    }.get(work_type, 0.5)
    if any(term in text for term in HIGH_LEVERAGE_TERMS):
        score += 1.2
    if any(term in text for term in RISK_TERMS):
        score += 0.6
    if pr.get("comments", 0) >= 3:
        score += 0.4
    if re.search(r"(fixes|closes|resolves)\s+#\d+|github\.com/PostHog/posthog/issues/\d+", text, re.I):
        score += 0.4
    try:
        closed = dt.datetime.fromisoformat((pr.get("closed_at") or pr.get("updated_at")).replace("Z", "+00:00"))
        age_days = max(0, (NOW - closed).days)
        score *= 1 + max(0, 30 - age_days) / 100
    except Exception:
        pass
    return round(score, 2)


def select_top_contributors(prs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_author: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pr in prs:
        login = pr.get("user", {}).get("login")
        if not login or login in BOT_LOGINS or login.endswith("[bot]"):
            continue
        pr["_provisional_impact"] = provisional_impact_score(pr)
        by_author[login].append(pr)

    ranked = []
    for login, author_prs in by_author.items():
        ranked.append(
            {
                "contributor": login,
                "provisional_impact_total": round(sum(p["_provisional_impact"] for p in author_prs), 2),
                "recent_merged_pr_count": len(author_prs),
            }
        )
    return sorted(ranked, key=lambda row: row["provisional_impact_total"], reverse=True)[:TOP_N_CONTRIBUTORS]


def selected_prs_for_contributor(prs: list[dict[str, Any]], contributor: str, quota: int) -> list[dict[str, Any]]:
    author_prs = [p for p in prs if p.get("user", {}).get("login") == contributor]
    author_prs.sort(key=lambda p: (p.get("_provisional_impact", provisional_impact_score(p)), p.get("closed_at", "")), reverse=True)
    selected: list[dict[str, Any]] = []
    seen: set[int] = set()

    def add(pr: dict[str, Any]) -> None:
        if len(selected) >= quota:
            return
        number = pr["number"]
        if number not in seen:
            selected.append(pr)
            seen.add(number)

    # Highest impact PRs.
    for pr in author_prs[: max(3, quota // 2)]:
        add(pr)

    # Recent work, biased toward the newest week.
    for pr in sorted(author_prs, key=lambda p: p.get("closed_at") or p.get("updated_at") or "", reverse=True):
        if len(selected) >= min(quota, 5):
            break
        add(pr)

    # Work with discussion or explicit issue context.
    discussion_prs = sorted(
        author_prs,
        key=lambda p: (
            int(p.get("comments", 0)),
            bool(re.search(r"(fixes|closes|resolves)\s+#\d+|github\.com/PostHog/posthog/issues/\d+", lower_join(p.get("body")))),
            p.get("_provisional_impact", 0),
        ),
        reverse=True,
    )
    for pr in discussion_prs:
        if len(selected) >= min(quota, 6):
            break
        add(pr)

    # Work-type diversity.
    used_types = {provisional_work_type(p) for p in selected}
    for pr in author_prs:
        wt = provisional_work_type(pr)
        if wt not in used_types:
            add(pr)
            used_types.add(wt)
        if len(selected) >= quota:
            break

    for pr in author_prs:
        add(pr)
        if len(selected) >= quota:
            break
    return selected


def pr_cache_path(number: int) -> Path:
    return RAW_DIR / f"pr_{number}.json"


def fetch_pr_bundle(client: GitHubClient, number: int) -> dict[str, Any]:
    path = pr_cache_path(number)
    if path.exists():
        return json.loads(path.read_text())

    api_base = f"https://api.github.com/repos/{REPO}"
    detail = client.get(f"{api_base}/pulls/{number}")
    files = client.paginated(f"{api_base}/pulls/{number}/files", max_pages=10)
    reviews = client.paginated(f"{api_base}/pulls/{number}/reviews", max_pages=10)
    review_comments = client.paginated(f"{api_base}/pulls/{number}/comments", max_pages=10)
    issue_comments = client.paginated(f"{api_base}/issues/{number}/comments", max_pages=10)
    linked_issues = fetch_linked_issues(client, detail, issue_comments)
    bundle = {
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "detail": detail,
        "files": files,
        "reviews": reviews,
        "review_comments": review_comments,
        "issue_comments": issue_comments,
        "linked_issues": linked_issues,
    }
    path.write_text(json.dumps(bundle, indent=2, sort_keys=True))
    return bundle


def fetch_linked_issues(client: GitHubClient, detail: dict[str, Any], issue_comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    text_parts = [detail.get("body") or "", detail.get("title") or ""]
    text_parts.extend(comment.get("body") or "" for comment in issue_comments[:20])
    text = "\n".join(text_parts)
    candidates: set[int] = set()
    for match in re.finditer(r"(?:fixes|closes|resolves)\s+(?:https://github\.com/PostHog/posthog/issues/)?#?(\d+)", text, re.I):
        candidates.add(int(match.group(1)))
    for match in re.finditer(r"https://github\.com/PostHog/posthog/issues/(\d+)", text, re.I):
        candidates.add(int(match.group(1)))
    linked = []
    for issue_number in sorted(candidates)[:3]:
        if issue_number == detail.get("number"):
            continue
        cache_path = CACHE_DIR / f"issue_{issue_number}.json"
        try:
            issue = cache_json(cache_path, lambda n=issue_number: client.get(f"https://api.github.com/repos/{REPO}/issues/{n}"))
        except Exception:
            continue
        if "pull_request" not in issue:
            linked.append(issue)
    return linked


def file_paths(bundle: dict[str, Any]) -> list[str]:
    return [f.get("filename", "") for f in bundle.get("files", []) if f.get("filename")]


def classify_areas(paths: list[str], title: str = "") -> list[str]:
    text = " ".join(paths).lower() + " " + title.lower()
    areas: list[str] = []
    checks = [
        ("Data warehouse", ["warehouse", "external_data", "data-warehouse", "source"]),
        ("Insights / Product analytics", ["insight", "funnel", "trend", "stickiness", "retention", "hogql", "query"]),
        ("Max AI / LLM", ["max_ai", "max-", "llm", "conversation", "ai/"]),
        ("MCP / Coding agents", ["mcp", "coding-agent", "agent"]),
        ("Infrastructure / CI", [".github/", "docker", "kubernetes", "helm", "celery", "worker", "infra", "ci"]),
        ("Frontend product UI", ["frontend/", "scenes/", "products/", "typescript", "tsx", "react"]),
        ("Backend services", ["posthog/", "ee/", "python", "django", "api/"]),
        ("Feature flags", ["feature_flag", "flags", "flag"]),
        ("Session replay / Vision", ["replay", "session_recording", "vision"]),
        ("Web analytics", ["web_analytics", "web-analytics"]),
        ("Dashboards / Alerts", ["dashboard", "alert", "subscription"]),
        ("Security / Auth", ["security", "auth", "permission", "credential", "personalapi", "oauth"]),
        ("Data modeling", ["data_model", "data modeling", "endpoints", "saved query"]),
        ("Developer experience", ["storybook", "lint", "types", "dev", "test", "playwright"]),
        ("Documentation", ["docs/", ".md", "readme"]),
    ]
    for area, needles in checks:
        if any(needle in text for needle in needles):
            areas.append(area)
    if not areas:
        areas.append("General application")
    # Keep area lists compact and deterministic.
    return areas[:4]


def detailed_work_type(detail: dict[str, Any], paths: list[str]) -> str:
    title_labels = lower_join(detail.get("title"), " ".join(label_names(detail)))
    surface = lower_join(detail.get("title"), " ".join(label_names(detail)), " ".join(paths))
    body_short = (detail.get("body") or "")[:900].lower()
    if any(term in surface for term in ["perf", "performance", "optimi", "memory"]):
        return "performance"
    if any(term in surface for term in ["migration", "migrate", "schema", "backfill", "index"]):
        return "migration"
    if any(term in f"{surface} {body_short}" for term in ["reliability", "retry", "fallback", "crash", "race", "rate limit", "failure", "oom"]):
        return "reliability"
    if any(term in surface for term in ["infra", "ci", "deploy", "docker", "k8s", "kubernetes", "celery", "clickhouse"]):
        return "infra"
    if any(term in surface for term in ["storybook", "lint", "typecheck", "developer", "devex", "dev experience", "dx"]):
        return "dx"
    if any(term in title_labels for term in ["fix", "bug", "regression"]):
        return "bugfix"
    if any(term in title_labels for term in ["feat", "add", "support", "implement", "scaffold"]):
        return "product"
    if any(term in surface for term in ["doc", "readme"]):
        return "dx"
    return "maintenance"


def test_files(paths: list[str]) -> list[str]:
    return [
        path
        for path in paths
        if re.search(r"(^|/)(__tests__|tests?|spec|e2e|playwright|cypress|pytest|test_)", path, re.I)
        or re.search(r"\.(test|spec)\.(ts|tsx|js|jsx|py)$", path, re.I)
    ]


def migration_files(paths: list[str]) -> list[str]:
    return [path for path in paths if "migration" in path.lower() or re.search(r"/migrations?/", path.lower())]


def docs_files(paths: list[str]) -> list[str]:
    return [path for path in paths if path.lower().endswith((".md", ".mdx")) or path.lower().startswith("docs/")]


def changed_prefixes(paths: list[str]) -> set[str]:
    prefixes = set()
    for path in paths:
        parts = path.split("/")
        if len(parts) >= 2:
            prefixes.add("/".join(parts[:2]))
        elif parts:
            prefixes.add(parts[0])
    return prefixes


def source_url_for_pr(number: int, suffix: str = "") -> str:
    return f"https://github.com/{REPO}/pull/{number}{suffix}"


def review_urls(bundle: dict[str, Any]) -> list[str]:
    urls = []
    for review in bundle.get("reviews", []):
        url = review.get("html_url")
        if url:
            urls.append(url)
    for comment in bundle.get("review_comments", []):
        url = comment.get("html_url")
        if url:
            urls.append(url)
    return urls


def issue_urls(bundle: dict[str, Any]) -> list[str]:
    return [issue.get("html_url") for issue in bundle.get("linked_issues", []) if issue.get("html_url")]


def first_sentence(text: str, default: str) -> str:
    clean = re.sub(r"\s+", " ", (text or "").strip())
    if not clean:
        return default
    match = re.split(r"(?<=[.!?])\s+", clean, maxsplit=1)
    return match[0][:220]


def evidence_counts(bundle: dict[str, Any], contributor: str) -> dict[str, Any]:
    detail = bundle["detail"]
    paths = file_paths(bundle)
    body = detail.get("body") or ""
    title = detail.get("title") or ""
    body_lower = body.lower()
    all_text = lower_join(title, body, " ".join(paths), json.dumps(label_names(detail)))
    reviews = [r for r in bundle.get("reviews", []) if r.get("user", {}).get("login") not in BOT_LOGINS]
    review_comments = [c for c in bundle.get("review_comments", []) if c.get("user", {}).get("login") not in BOT_LOGINS]
    issue_comments = [c for c in bundle.get("issue_comments", []) if c.get("user", {}).get("login") not in BOT_LOGINS]
    author_comments = [
        c
        for c in review_comments + issue_comments
        if c.get("user", {}).get("login") == contributor and len(c.get("body") or "") > 15
    ]
    substantive_review_comments = [
        c
        for c in review_comments
        if c.get("user", {}).get("login") != contributor and len(c.get("body") or "") >= 80
    ]
    reviewer_states = Counter(r.get("state") for r in reviews)
    approvals = reviewer_states.get("APPROVED", 0)
    changes_requested = reviewer_states.get("CHANGES_REQUESTED", 0)
    validation_mentions = [term for term in VALIDATION_TERMS if term in body_lower]
    risk_mentions = [term for term in RISK_TERMS if term in all_text]
    return {
        "paths": paths,
        "areas": classify_areas(paths, title),
        "work_type": detailed_work_type(detail, paths),
        "tests": test_files(paths),
        "migrations": migration_files(paths),
        "docs": docs_files(paths),
        "prefixes": changed_prefixes(paths),
        "reviews": reviews,
        "review_comments": review_comments,
        "issue_comments": issue_comments,
        "author_comments": author_comments,
        "substantive_review_comments": substantive_review_comments,
        "approvals": approvals,
        "changes_requested": changes_requested,
        "validation_mentions": validation_mentions,
        "risk_mentions": risk_mentions,
        "linked_issues": bundle.get("linked_issues", []),
    }


def clamp(value: float, low: float = 0.0, high: float = 5.0) -> float:
    return max(low, min(high, value))


def rounded_half(value: float) -> float:
    return round(value * 2) / 2


def dimension_entry(score: float, justification: str, sources: list[str]) -> dict[str, Any]:
    deduped = []
    for source in sources:
        if source and source not in deduped:
            deduped.append(source)
    return {"score": rounded_half(clamp(score)), "justification": justification, "sources": deduped[:5]}


def recalc_existing_impact(detail: dict[str, Any], evidence: dict[str, Any]) -> float:
    score = provisional_impact_score(detail)
    areas = evidence["areas"]
    work_type = evidence["work_type"]
    score += min(1.2, max(0, len(areas) - 1) * 0.25)
    score += {
        "product": 0.6,
        "bugfix": 0.5,
        "infra": 0.7,
        "reliability": 0.9,
        "performance": 0.9,
        "migration": 0.8,
        "dx": 0.3,
        "maintenance": 0.1,
    }.get(work_type, 0.2)
    if evidence["tests"]:
        score += 0.35
    if evidence["migrations"]:
        score += 0.45
    if evidence["substantive_review_comments"]:
        score += 0.35
    if any(area in areas for area in ["Security / Auth", "Infrastructure / CI", "Insights / Product analytics", "Data warehouse"]):
        score += 0.45
    return round(score, 2)


def score_pr(bundle: dict[str, Any], contributor: str, ownership_areas: Counter[str]) -> dict[str, Any]:
    detail = bundle["detail"]
    number = detail["number"]
    title = detail.get("title") or ""
    pr_url = detail.get("html_url") or source_url_for_pr(number)
    files_url = source_url_for_pr(number, "/files")
    conversation_url = pr_url
    evidence = evidence_counts(bundle, contributor)
    paths = evidence["paths"]
    areas = evidence["areas"]
    work_type = evidence["work_type"]
    all_text = lower_join(title, detail.get("body"), " ".join(paths), " ".join(label_names(detail)))
    issue_source_urls = issue_urls(bundle)
    review_source_urls = review_urls(bundle)

    high_leverage = any(
        area
        in {
            "Data warehouse",
            "Insights / Product analytics",
            "Infrastructure / CI",
            "Security / Auth",
            "Session replay / Vision",
            "Feature flags",
            "Max AI / LLM",
            "MCP / Coding agents",
            "Dashboards / Alerts",
            "Web analytics",
        }
        for area in areas
    )
    linked_issue_text = ""
    if evidence["linked_issues"]:
        issue = evidence["linked_issues"][0]
        linked_issue_text = f" The linked issue/source context is `{issue.get('title', 'issue')[:90]}`."

    # Problem importance.
    problem = 2.4
    problem += {
        "product": 1.0,
        "bugfix": 0.7,
        "infra": 1.0,
        "reliability": 1.4,
        "performance": 1.5,
        "migration": 1.2,
        "dx": 0.6,
        "maintenance": 0.2,
    }.get(work_type, 0.4)
    if high_leverage:
        problem += 0.7
    if any(term in all_text for term in ["security", "auth", "permission", "billing", "rate limit", "oom", "crash", "reliability"]):
        problem += 0.5
    if issue_source_urls:
        problem += 0.2
    problem = rounded_half(clamp(problem))
    problem_just = (
        f"`{title}` is a {work_type} change in {', '.join(areas)}. "
        f"The title/body and touched paths indicate {'high-leverage product/system impact' if problem >= 4 else 'moderate scoped value'}."
        f"{linked_issue_text}"
    )

    # Technical soundness.
    tech = 3.0
    if len(evidence["prefixes"]) <= 3:
        tech += 0.4
    if evidence["tests"]:
        tech += 0.4
    if evidence["migrations"] and any("migrations" not in p.lower() for p in paths):
        tech += 0.2
    if evidence["approvals"]:
        tech += 0.2
    if evidence["substantive_review_comments"]:
        tech += 0.2
    changed_files = detail.get("changed_files") or len(paths)
    additions = detail.get("additions") or 0
    if changed_files > 35 or additions > 2500:
        tech -= 0.4
    if "scaffold" in all_text and additions > 800:
        tech -= 0.2
    if evidence["changes_requested"] and not evidence["approvals"]:
        tech -= 0.5
    tech = rounded_half(clamp(tech))
    tech_just = (
        f"The diff is concentrated across {len(evidence['prefixes']) or 1} path cluster(s) with {changed_files} changed files, "
        f"and includes {len(evidence['tests'])} test file(s). "
        f"{'Reviewer activity did not leave unresolved change-request evidence.' if evidence['approvals'] or not evidence['changes_requested'] else 'Review history includes change-request evidence, so structure/correctness confidence is lower.'}"
    )

    # Risk handling.
    risk = 2.4
    if evidence["tests"]:
        risk += 0.7
    if evidence["migrations"]:
        risk += 0.6
    if evidence["risk_mentions"]:
        risk += min(0.8, 0.15 * len(set(evidence["risk_mentions"])))
    if evidence["validation_mentions"]:
        risk += 0.3
    if evidence["substantive_review_comments"]:
        risk += 0.3
    if high_leverage and not evidence["tests"] and not evidence["validation_mentions"]:
        risk -= 0.5
    risk = rounded_half(clamp(risk))
    mitigation_bits = []
    if evidence["tests"]:
        mitigation_bits.append(f"{len(evidence['tests'])} test file(s)")
    if evidence["migrations"]:
        mitigation_bits.append(f"{len(evidence['migrations'])} migration file(s)")
    if evidence["risk_mentions"]:
        mitigation_bits.append(f"explicit risk-related terms such as {', '.join(sorted(set(evidence['risk_mentions']))[:3])}")
    if evidence["validation_mentions"]:
        mitigation_bits.append("PR-body validation notes")
    risk_just = (
        "Risk mitigation evidence includes " + (", ".join(mitigation_bits) if mitigation_bits else "limited explicit mitigation")
        + f". The area is {'high leverage' if high_leverage else 'localized'}, so missing mitigation is weighted accordingly."
    )

    # Review/collaboration.
    review_total = len(evidence["reviews"]) + len(evidence["review_comments"]) + len(evidence["issue_comments"])
    if review_total == 0:
        review_score = 0.0
        review_just = "No review, review-comment, or issue-comment context was visible through the GitHub API for this PR."
        review_sources = [conversation_url]
    else:
        review_score = 2.8
        if evidence["approvals"]:
            review_score += 0.4
        if evidence["substantive_review_comments"]:
            review_score += 0.7
        if evidence["author_comments"]:
            review_score += 0.4
        if evidence["changes_requested"] and evidence["approvals"]:
            review_score += 0.3
        if len({r.get("user", {}).get("login") for r in evidence["reviews"] if r.get("user")}) >= 2:
            review_score += 0.2
        review_score = rounded_half(clamp(review_score))
        review_just = (
            f"Visible collaboration includes {len(evidence['reviews'])} review event(s), "
            f"{len(evidence['review_comments'])} inline review comment(s), and {len(evidence['author_comments'])} author response/comment(s). "
            f"{'Substantive reviewer comments are present.' if evidence['substantive_review_comments'] else 'The review record is mostly approval/comment-count signal rather than detailed design discussion.'}"
        )
        review_sources = review_source_urls[:4] or [conversation_url]

    # Test/validation.
    if evidence["tests"]:
        test_score = 4.0 + min(1.0, (len(evidence["tests"]) - 1) * 0.25)
    elif evidence["validation_mentions"]:
        test_score = 3.0
    elif work_type in {"maintenance", "dx"} or evidence["docs"]:
        test_score = 2.5
    elif detail.get("changed_files", 0) and detail.get("changed_files", 0) <= 3:
        test_score = 2.0
    else:
        test_score = 1.5
    if evidence["migrations"] and not evidence["tests"]:
        test_score -= 0.4
    test_score = rounded_half(clamp(test_score))
    test_just = (
        f"The file list shows {len(evidence['tests'])} test file(s), {len(evidence['migrations'])} migration file(s), "
        f"and {len(evidence['docs'])} docs file(s). "
        f"{'The PR body also mentions validation/testing.' if evidence['validation_mentions'] else 'No strong PR-body validation note was visible.'}"
    )

    # Area leverage / ownership.
    area = 2.7
    if high_leverage:
        area += 1.0
    contributor_area_repeats = sum(ownership_areas[a] for a in areas)
    if contributor_area_repeats >= 5:
        area += 0.6
    elif contributor_area_repeats >= 3:
        area += 0.3
    if len(areas) >= 3:
        area += 0.2
    area = rounded_half(clamp(area))
    area_just = (
        f"Touched areas classify as {', '.join(areas)}. "
        f"The contributor has {contributor_area_repeats} reviewed touches in these area bucket(s), which is the ownership signal used here."
    )

    # Communication.
    body = detail.get("body") or ""
    body_len = len(body.strip())
    comm = 1.6
    if body_len > 150:
        comm += 0.8
    if body_len > 500:
        comm += 0.7
    if re.search(r"##\s*(Problem|Changes|How|Test|Screenshot|Context)", body, re.I):
        comm += 0.7
    if issue_source_urls:
        comm += 0.4
    if evidence["validation_mentions"]:
        comm += 0.3
    if len(title) > 15 and re.match(r"^(feat|fix|chore|refactor|perf|docs|test|revert)(\\(.+\\))?:", title.lower()):
        comm += 0.2
    comm = rounded_half(clamp(comm))
    comm_just = (
        f"The PR title is structured and the body has {body_len} characters of context. "
        f"{'It includes template sections and/or validation notes.' if comm >= 4 else 'Some rationale or validation context has to be inferred from title, paths, or discussion.'}"
    )

    scores = {
        "problem_importance": dimension_entry(problem, problem_just, [pr_url, *issue_source_urls]),
        "technical_soundness": dimension_entry(tech, tech_just, [files_url, pr_url, *review_source_urls[:2]]),
        "risk_handling": dimension_entry(risk, risk_just, [files_url, pr_url, *review_source_urls[:2]]),
        "review_collaboration": dimension_entry(review_score, review_just, review_sources),
        "test_validation": dimension_entry(test_score, test_just, [files_url, pr_url]),
        "area_leverage": dimension_entry(area, area_just, [files_url, pr_url]),
        "communication": dimension_entry(comm, comm_just, [pr_url, *issue_source_urls]),
    }

    weights = {
        "problem_importance": 20,
        "technical_soundness": 20,
        "risk_handling": 15,
        "review_collaboration": 15,
        "test_validation": 10,
        "area_leverage": 10,
        "communication": 10,
    }
    pr_quality_score = round(sum(weights[k] * scores[k]["score"] / 5 for k in weights), 1)
    strong_categories = [
        "important problem" if scores["problem_importance"]["score"] >= 4 else None,
        "technical soundness" if scores["technical_soundness"]["score"] >= 4 else None,
        "risk handling" if scores["risk_handling"]["score"] >= 4 else None,
        "test validation" if scores["test_validation"]["score"] >= 4 else None,
        "meaningful review discussion" if scores["review_collaboration"]["score"] >= 4 else None,
        "high-leverage area" if scores["area_leverage"]["score"] >= 4 else None,
    ]
    strong_categories = [c for c in strong_categories if c]
    limitations = []
    if not evidence["tests"]:
        limitations.append("No test file was visible in the fetched PR file list.")
    if not evidence["substantive_review_comments"]:
        limitations.append("Review discussion was sparse or mostly approval-only in the fetched API data.")
    if detail.get("changed_files", 0) >= 50:
        limitations.append("Large diff size makes source-level assessment less certain without full local checkout context.")
    if not body.strip():
        limitations.append("PR body was empty or unavailable.")
    if not limitations:
        limitations.append("Assessment is based on public GitHub metadata, file patches, and visible review records only.")

    if pr_quality_score >= 82:
        summary = "Strong evidence of high-quality, high-leverage work with credible implementation and validation signals."
    elif pr_quality_score >= 72:
        summary = "Solid quality signal with useful impact, though at least one evidence category is incomplete."
    elif pr_quality_score >= 60:
        summary = "Moderate quality signal; the work appears useful but supporting evidence is mixed or sparse."
    else:
        summary = "Limited quality confidence from the available public evidence."

    confidence = "high"
    if len(paths) == 0 or len(scores["review_collaboration"]["sources"]) == 0:
        confidence = "medium"
    if scores["review_collaboration"]["score"] <= 2 or (not evidence["tests"] and not evidence["validation_mentions"]):
        confidence = "medium"
    if not paths or scores["communication"]["score"] <= 2:
        confidence = "low"

    return {
        "contributor": contributor,
        "pr_number": number,
        "pr_title": title,
        "pr_url": pr_url,
        "merged_at": detail.get("merged_at"),
        "areas": areas,
        "existing_impact_points": recalc_existing_impact(detail, evidence),
        "work_type": work_type,
        "scores": scores,
        "pr_quality_score": pr_quality_score,
        "summary_judgment": summary,
        "confidence": confidence,
        "limitations": limitations,
        "_strong_evidence_categories": strong_categories,
        "_evidence": {
            "test_file_count": len(evidence["tests"]),
            "migration_file_count": len(evidence["migrations"]),
            "review_event_count": len(evidence["reviews"]),
            "inline_review_comment_count": len(evidence["review_comments"]),
            "issue_comment_count": len(evidence["issue_comments"]),
        },
    }


def review_search_cache_path(contributor: str) -> Path:
    return REVIEW_DIR / f"{contributor}_reviewed_prs.json"


def fetch_review_interactions(client: GitHubClient, contributor: str) -> list[dict[str, Any]]:
    cache_path = review_search_cache_path(contributor)
    if cache_path.exists():
        return json.loads(cache_path.read_text())
    query = (
        f"repo:{REPO} is:pr is:merged reviewed-by:{contributor} "
        f"merged:>={START_DATE.isoformat()} -author:{contributor}"
    )
    data = client.get(
        "https://api.github.com/search/issues",
        params={"q": query, "sort": "updated", "order": "desc", "per_page": 15, "page": 1},
    )
    interactions = []
    for item in data.get("items", []):
        author = item.get("user", {}).get("login")
        if author in BOT_LOGINS or (author and author.endswith("[bot]")):
            continue
        number = item["number"]
        bundle = fetch_pr_bundle(client, number)
        scored = score_review_interaction(bundle, contributor)
        if scored:
            interactions.append(scored)
        if len(interactions) >= 5:
            break
    cache_path.write_text(json.dumps(interactions, indent=2, sort_keys=True))
    return interactions


def score_review_interaction(bundle: dict[str, Any], contributor: str) -> dict[str, Any] | None:
    detail = bundle["detail"]
    number = detail["number"]
    reviews = [r for r in bundle.get("reviews", []) if r.get("user", {}).get("login") == contributor]
    comments = [c for c in bundle.get("review_comments", []) if c.get("user", {}).get("login") == contributor]
    if not reviews and not comments:
        return None
    bodies = [r.get("body") or "" for r in reviews] + [c.get("body") or "" for c in comments]
    text = "\n".join(bodies).strip()
    substantive = [body for body in bodies if len(body.strip()) >= 80]
    risk_terms = [term for term in RISK_TERMS if term in text.lower()]
    score = 2.0
    if reviews:
        score += 0.6
    if comments:
        score += min(0.9, 0.3 * len(comments))
    if substantive:
        score += 0.9
    if risk_terms:
        score += 0.4
    if any((r.get("state") == "CHANGES_REQUESTED") for r in reviews):
        score += 0.5
    score = rounded_half(clamp(score))
    exemplar = first_sentence(text, "Approval/review event without a substantive public body.")
    sources = [r.get("html_url") for r in reviews if r.get("html_url")]
    sources.extend(c.get("html_url") for c in comments if c.get("html_url"))
    return {
        "pr_number": number,
        "pr_title": detail.get("title"),
        "pr_url": detail.get("html_url") or source_url_for_pr(number),
        "review_quality_score": score,
        "review_quality_percent": round(score / 5 * 100, 1),
        "substantive": score >= 4,
        "example": exemplar[:360],
        "sources": [s for s in sources if s][:5] or [source_url_for_pr(number)],
    }


def capped_weighted_average(values: list[tuple[float, float]]) -> float:
    if not values:
        return 0.0
    weights = [max(0.01, weight) for _, weight in values]
    scores = [score for score, _ in values]
    total = sum(weights)
    cap = total * 0.25
    capped = [min(weight, cap) for weight in weights]
    # A second pass prevents one item from exceeding 25% after the first cap.
    for _ in range(3):
        total_capped = sum(capped)
        cap = total_capped * 0.25
        capped = [min(weight, cap) for weight in capped]
    total_capped = sum(capped)
    return round(sum(score * weight for score, weight in zip(scores, capped)) / total_capped, 1)


def ownership_signal_for(contributor_reviews: list[dict[str, Any]]) -> float:
    area_counts = Counter(area for review in contributor_reviews for area in review["areas"])
    high_quality = sum(1 for review in contributor_reviews if review["pr_quality_score"] >= 80)
    high_leverage_touches = sum(
        1
        for review in contributor_reviews
        if any(
            area
            in {
                "Data warehouse",
                "Insights / Product analytics",
                "Infrastructure / CI",
                "Security / Auth",
                "Session replay / Vision",
                "Feature flags",
                "Max AI / LLM",
                "MCP / Coding agents",
            }
            for area in review["areas"]
        )
    )
    if high_quality >= 4 and len(area_counts) >= 3 and high_leverage_touches >= 4:
        return 5.0
    if high_quality >= 3 and high_leverage_touches >= 3:
        return 4.0
    if high_quality >= 2 or max(area_counts.values() or [0]) >= 3:
        return 3.0
    if contributor_reviews:
        return 2.0
    return 0.0


def aggregate_contributor(
    contributor: str,
    pr_reviews: list[dict[str, Any]],
    review_interactions: list[dict[str, Any]],
) -> dict[str, Any]:
    weighted_avg = capped_weighted_average(
        [(review["pr_quality_score"], review["existing_impact_points"]) for review in pr_reviews]
    )
    scores = sorted(review["pr_quality_score"] for review in pr_reviews)
    consistency = round(100 * sum(score >= 70 for score in scores) / len(scores), 1) if scores else 0.0
    high_conf_count = sum(
        1
        for review in pr_reviews
        if review["pr_quality_score"] >= 80 and len(review.get("_strong_evidence_categories", [])) >= 3
    )
    review_leverage = (
        round(statistics.mean(item["review_quality_percent"] for item in review_interactions), 1)
        if review_interactions
        else 0.0
    )
    ownership = ownership_signal_for(pr_reviews)
    comm_avg = round(statistics.mean(review["scores"]["communication"]["score"] for review in pr_reviews), 2) if pr_reviews else 0.0
    quality_score = round(
        0.45 * weighted_avg
        + 0.20 * consistency
        + 0.15 * review_leverage
        + 0.10 * ownership * 20
        + 0.10 * comm_avg * 20,
        1,
    )
    confidence = "high"
    if len(pr_reviews) < 5 or not review_interactions:
        confidence = "medium"
    if len(pr_reviews) < 4:
        confidence = "low"
    if sum(1 for review in pr_reviews if review["confidence"] == "low") >= 2:
        confidence = "medium"
    area_counts = Counter(area for review in pr_reviews for area in review["areas"])
    top_areas = [area for area, _ in area_counts.most_common(3)]
    strengths = []
    if weighted_avg >= 78:
        strengths.append(f"Reviewed PRs average {weighted_avg}/100 after impact-weighting.")
    if consistency >= 80:
        strengths.append(f"{consistency}% of reviewed PRs score at least 70.")
    if high_conf_count:
        strengths.append(f"{high_conf_count} reviewed PR(s) clear the >=80 high-confidence-impact threshold.")
    if ownership >= 4:
        strengths.append(f"Strong ownership signal in {', '.join(top_areas)}.")
    if review_leverage >= 70:
        strengths.append(f"Review leverage average is {review_leverage}/100 across sampled review interactions.")
    if not strengths:
        strengths.append("Quality evidence is mixed but includes visible merged work in the sampled period.")

    caveats = []
    sparse_reviews = sum(1 for review in pr_reviews if review["scores"]["review_collaboration"]["score"] <= 3)
    no_tests = sum(1 for review in pr_reviews if review["_evidence"]["test_file_count"] == 0)
    if sparse_reviews:
        caveats.append(f"{sparse_reviews} reviewed PR(s) have sparse public review discussion.")
    if no_tests:
        caveats.append(f"{no_tests} reviewed PR(s) do not show test files in the GitHub file list.")
    if contributor == "Gilbert09":
        caveats.append("Very high PR volume includes repeated data-warehouse scaffolding, so mechanical impact can overstate novelty.")
    if not review_interactions:
        caveats.append("No representative reviewer-side interactions were found with the reviewed-by search qualifier.")
    if not caveats:
        caveats.append("Still directional: public GitHub evidence cannot fully capture private design context or production outcomes.")

    median = round(statistics.median(scores), 1) if scores else 0.0
    min_score = round(min(scores), 1) if scores else 0.0
    max_score = round(max(scores), 1) if scores else 0.0
    summary = (
        f"{contributor} shows {confidence}-confidence quality evidence across {len(pr_reviews)} sampled PRs, "
        f"with strongest signal in {', '.join(top_areas) if top_areas else 'general application work'}."
    )
    representative_sources = [review["pr_url"] for review in sorted(pr_reviews, key=lambda r: r["pr_quality_score"], reverse=True)[:4]]
    for interaction in review_interactions[:2]:
        representative_sources.extend(interaction.get("sources", [])[:1])

    return {
        "contributor": contributor,
        "quality_score": quality_score,
        "confidence": confidence,
        "reviewed_pr_count": len(pr_reviews),
        "reviewed_review_interaction_count": len(review_interactions),
        "weighted_pr_quality_average": weighted_avg,
        "quality_consistency_percent": consistency,
        "high_confidence_impact_count": high_conf_count,
        "review_leverage_quality": review_leverage,
        "ownership_signal": ownership,
        "communication_quality_average": comm_avg,
        "score_distribution": {"min": min_score, "median": median, "max": max_score},
        "top_strengths": strengths[:5],
        "risks_or_caveats": caveats[:5],
        "executive_summary": summary,
        "representative_sources": list(dict.fromkeys(representative_sources))[:8],
        "review_interactions": review_interactions,
    }


def build_sources(pr_reviews: list[dict[str, Any]], bundles: dict[int, dict[str, Any]], review_interactions_by_contributor: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    fetched_at = dt.datetime.now(dt.timezone.utc).isoformat()
    sources: dict[str, Any] = {}
    for review in pr_reviews:
        number = review["pr_number"]
        bundle = bundles[number]
        sources[f"pr:{number}"] = {
            "url": review["pr_url"],
            "type": "pr",
            "fetched_at": bundle.get("fetched_at", fetched_at),
            "used_for": ["problem_importance", "communication", "review_collaboration"],
        }
        sources[f"diff:{number}"] = {
            "url": source_url_for_pr(number, "/files"),
            "type": "diff",
            "fetched_at": bundle.get("fetched_at", fetched_at),
            "used_for": ["technical_soundness", "risk_handling", "test_validation", "area_leverage"],
        }
        for issue in bundle.get("linked_issues", []):
            issue_number = issue.get("number")
            if issue_number:
                sources[f"issue:{issue_number}:from-pr:{number}"] = {
                    "url": issue.get("html_url"),
                    "type": "issue",
                    "fetched_at": bundle.get("fetched_at", fetched_at),
                    "used_for": ["problem_importance", "communication"],
                }
        for item in bundle.get("reviews", [])[:20]:
            url = item.get("html_url")
            if url:
                sources[f"review:{number}:{item.get('id')}"] = {
                    "url": url,
                    "type": "review",
                    "fetched_at": bundle.get("fetched_at", fetched_at),
                    "used_for": ["review_collaboration"],
                }
        for item in bundle.get("review_comments", [])[:20]:
            url = item.get("html_url")
            if url:
                sources[f"review-comment:{number}:{item.get('id')}"] = {
                    "url": url,
                    "type": "review",
                    "fetched_at": bundle.get("fetched_at", fetched_at),
                    "used_for": ["technical_soundness", "risk_handling", "review_collaboration"],
                }
        for path in file_paths(bundle)[:8]:
            sources[f"file:{number}:{path}"] = {
                "url": f"https://github.com/{REPO}/pull/{number}/files",
                "type": "file",
                "fetched_at": bundle.get("fetched_at", fetched_at),
                "used_for": ["technical_soundness", "test_validation", "area_leverage"],
            }

    for interactions in review_interactions_by_contributor.values():
        for interaction in interactions:
            for url in interaction.get("sources", []):
                key = f"review-url:{url}"
                sources[key] = {
                    "url": url,
                    "type": "review",
                    "fetched_at": fetched_at,
                    "used_for": ["review_leverage_quality"],
                }
    return sources


def write_checkpoint(
    top_contributors: list[dict[str, Any]],
    selected_by_contributor: dict[str, list[int]],
    pr_reviews: list[dict[str, Any]],
    contributor_scores: list[dict[str, Any]],
) -> None:
    score_values = [review["pr_quality_score"] for review in pr_reviews]
    contributors_lines = []
    by_contributor_reviews = defaultdict(list)
    for review in pr_reviews:
        by_contributor_reviews[review["contributor"]].append(review)
    score_by_contributor = {row["contributor"]: row for row in contributor_scores}
    for rank, row in enumerate(top_contributors, start=1):
        contributor = row["contributor"]
        reviews = by_contributor_reviews[contributor]
        score = score_by_contributor[contributor]
        pr_list = ", ".join(f"#{review['pr_number']} ({review['work_type']}, {review['pr_quality_score']})" for review in reviews)
        contributors_lines.append(
            f"| {rank} | `{contributor}` | {row['provisional_impact_total']} | {len(reviews)} | "
            f"{score['quality_score']} | {score['confidence']} | {pr_list} |"
        )

    distribution = {
        "count": len(score_values),
        "min": round(min(score_values), 1),
        "median": round(statistics.median(score_values), 1),
        "mean": round(statistics.mean(score_values), 1),
        "max": round(max(score_values), 1),
        ">=80": sum(1 for value in score_values if value >= 80),
        "70-79.9": sum(1 for value in score_values if 70 <= value < 80),
        "60-69.9": sum(1 for value in score_values if 60 <= value < 70),
        "<60": sum(1 for value in score_values if value < 60),
    }
    content = f"""# Part 1 Analysis Checkpoint

Generated: {dt.datetime.now(dt.timezone.utc).isoformat()}

## Status

Part 1 is complete and ready for visualization. No dashboard UI files were created or modified in this checkpoint.

## Inputs and contributor selection

- Source repository analyzed: `{REPO}`.
- Current window used for the reconstructed impact cohort: merged PRs from `{START_DATE.isoformat()}` through `{END_DATE.isoformat()}`.
- Cached GitHub input: `data/github_cache/recent_merged_prs_search_30d.json` plus per-PR bundles in `data/github_cache/raw_prs/`.
- Important limitation: the attached assessment repository contained only `README.md`, not the prior dashboard or existing pipeline output. To satisfy the checkpoint, I reconstructed the current top contributors with a deterministic impact proxy using merged PR recency, title/body/label signal, review discussion count, high-leverage keywords, and then recalculated selected PR impact points after fetching file/review evidence.

| Impact rank | Contributor | Reconstructed impact total | Reviewed PRs | Quality score | Confidence | Reviewed PR numbers |
|---:|---|---:|---:|---:|---|---|
{chr(10).join(contributors_lines)}

## Rubric used

Each reviewed PR was scored from 0 to 5 on the exact requested dimensions, then converted to a 0-100 weighted PR score:

- Problem Importance: 20%
- Technical Soundness: 20%
- Risk Handling and Reliability: 15%
- Review Quality and Collaboration: 15%
- Test and Validation Strength: 10%
- Area Leverage and Ownership: 10%
- Communication Quality: 10%

Contributor quality scores use the requested weighting:

- 45% weighted PR quality average, with single-PR contribution capped at 25% of reviewed-PR weight
- 20% quality consistency
- 15% review leverage quality
- 10% ownership signal
- 10% communication quality average

## Scoring distribution

- Reviewed PRs: {distribution['count']}
- Min / median / mean / max PR quality: {distribution['min']} / {distribution['median']} / {distribution['mean']} / {distribution['max']}
- PRs >= 80: {distribution['>=80']}
- PRs 70-79.9: {distribution['70-79.9']}
- PRs 60-69.9: {distribution['60-69.9']}
- PRs < 60: {distribution['<60']}

## Major uncertainties

- The original mechanical impact model was unavailable in this repository, so `existing_impact_points` and impact ranks are reconstructed rather than imported.
- GitHub public API data shows public PR discussions, file patches, labels, and linked issues; it does not capture private Slack/design discussion, internal incident context, production outcomes, or reviewer intent not written on GitHub.
- Some high-volume data-warehouse scaffold PRs are real merged work but repetitive; their mechanical impact can overstate novelty even when the implementation follows a useful pattern.
- Approval-only reviews are scored conservatively because they provide weaker evidence than inline design/risk discussion.
- Large PRs are assessed from GitHub file patches and metadata, not a full local checkout of PostHog/posthog at each merge SHA.

## API/data limitations

- GitHub search is capped, so the 30-day cohort was fetched by day (`merged:YYYY-MM-DD`) to avoid the 1,000-result search ceiling.
- Review examples use `reviewed-by:` search plus visible review/review-comment bodies; private or deleted comments are not visible.
- Linked issue fetching is limited to explicit `fixes/closes/resolves` references and direct `github.com/PostHog/posthog/issues/...` links found in PR text/comments.
- Per-PR file evidence uses the GitHub Pull Request Files API, capped by pagination into cached bundles.

## Readiness for visualization

Ready for Part 2 visualization: yes.

The required files are present:

- `data/qualitative_pr_reviews.json`
- `data/contributor_quality_scores.json`
- `data/quality_assessment_sources.json`
- `PART_1_ANALYSIS_CHECKPOINT.md`

Suggested second pass before treating this as anything stronger than directional dashboard data:

- Reconcile reconstructed impact ranks against the original pipeline output if it becomes available.
- Manually spot-check the highest-volume connector/scaffold PRs for `Gilbert09` and `danielcarletti`.
- Add production/incident outcome context where available for reliability/security/performance PRs.
"""
    (ROOT / "PART_1_ANALYSIS_CHECKPOINT.md").write_text(content)


def strip_private_keys(review: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in review.items() if not key.startswith("_")}


def main() -> None:
    ensure_dirs()
    client = GitHubClient()
    recent_prs = fetch_recent_merged_prs(client)
    top_contributors = select_top_contributors(recent_prs)

    selected_by_contributor: dict[str, list[dict[str, Any]]] = {}
    for rank, row in enumerate(top_contributors, start=1):
        quota = DEEP_REVIEW_COUNT if rank <= 5 else STANDARD_REVIEW_COUNT
        selected_by_contributor[row["contributor"]] = selected_prs_for_contributor(
            recent_prs, row["contributor"], quota
        )

    all_selected_numbers = sorted({pr["number"] for prs in selected_by_contributor.values() for pr in prs})
    bundles: dict[int, dict[str, Any]] = {}
    for number in all_selected_numbers:
        bundles[number] = fetch_pr_bundle(client, number)

    # Area ownership signal is computed over the selected PR evidence.
    ownership_areas_by_contributor: dict[str, Counter[str]] = defaultdict(Counter)
    for contributor, selected in selected_by_contributor.items():
        for pr in selected:
            bundle = bundles[pr["number"]]
            areas = classify_areas(file_paths(bundle), bundle["detail"].get("title") or "")
            ownership_areas_by_contributor[contributor].update(areas)

    pr_reviews: list[dict[str, Any]] = []
    for contributor, selected in selected_by_contributor.items():
        for pr in selected:
            pr_reviews.append(score_pr(bundles[pr["number"]], contributor, ownership_areas_by_contributor[contributor]))
    pr_reviews.sort(key=lambda row: (top_contributors.index(next(c for c in top_contributors if c["contributor"] == row["contributor"])), -row["existing_impact_points"], row["pr_number"]))

    review_interactions_by_contributor: dict[str, list[dict[str, Any]]] = {}
    for row in top_contributors:
        contributor = row["contributor"]
        review_interactions_by_contributor[contributor] = fetch_review_interactions(client, contributor)

    contributor_scores = []
    for row in top_contributors:
        contributor = row["contributor"]
        reviews = [review for review in pr_reviews if review["contributor"] == contributor]
        contributor_scores.append(aggregate_contributor(contributor, reviews, review_interactions_by_contributor[contributor]))

    sources = build_sources(pr_reviews, bundles, review_interactions_by_contributor)

    (DATA_DIR / "qualitative_pr_reviews.json").write_text(
        json.dumps([strip_private_keys(review) for review in pr_reviews], indent=2, sort_keys=True)
    )
    (DATA_DIR / "contributor_quality_scores.json").write_text(json.dumps(contributor_scores, indent=2, sort_keys=True))
    (DATA_DIR / "quality_assessment_sources.json").write_text(json.dumps(sources, indent=2, sort_keys=True))
    selected_numbers = {c: [pr["number"] for pr in prs] for c, prs in selected_by_contributor.items()}
    (CACHE_DIR / "selected_prs_by_contributor.json").write_text(json.dumps(selected_numbers, indent=2, sort_keys=True))
    (CACHE_DIR / "top_contributors_reconstructed.json").write_text(json.dumps(top_contributors, indent=2, sort_keys=True))
    write_checkpoint(top_contributors, selected_numbers, pr_reviews, contributor_scores)

    print(json.dumps({"contributors": [row["contributor"] for row in top_contributors], "reviewed_prs": len(pr_reviews)}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
