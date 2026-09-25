"""Data for the chat's repository panel: the file tree, recent commits, and
a short summary of what the repository is. All read-only, and all fetched
with the signed-in user's token, so GitHub decides what they can see."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

import github_tools
import llm
import repo_index
from db import repos as store

# The tree viewer shows everything, but a huge monorepo shouldn't ship
# hundreds of thousands of paths to the browser.
MAX_TREE_ENTRIES = 20000
RECENT_COMMITS = 8
# Ahead/behind costs one compare request per branch, so only this many
# branches get one; the rest are still listed.
MAX_COMPARED_BRANCHES = 15
OPEN_PULLS = 15
TOP_CONTRIBUTORS = 12
OPEN_ISSUES = 15
# Enough recent runs to find the latest one of each workflow.
RECENT_WORKFLOW_RUNS = 40
README_CHARS = 6000
# What a repository *is* rarely changes commit to commit; one model call
# per repo every few hours is plenty.
SUMMARY_TTL_SECONDS = 6 * 3600

_summaries: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()

SUMMARY_PROMPT = (
    "You describe GitHub repositories for a sidebar card. From the repository's metadata, README "
    "excerpt, and file overview, write what the project is and does, in 2 to 4 plain sentences "
    "(no markdown, no emoji, no marketing tone). If the README is missing or empty, infer from the "
    "files and say what it appears to be. Also list up to 6 main languages, frameworks, or tools "
    "it uses, as short names. Finally, suggest 4 questions a developer new to this repository "
    "might ask about it, each specific to this project (name real files, folders, or features "
    "you can see) and under 90 characters."
)

SUMMARY_SCHEMA = {
    "title": "repo_summary",
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "stack": {"type": "array", "items": {"type": "string"}},
        "questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "stack", "questions"],
}


def tree(token, owner, repo) -> dict:
    index = repo_index.ensure_index(token, owner, repo)
    rows = store.all_entries(index["tree_id"], MAX_TREE_ENTRIES)
    return {
        "repository": index["repository"],
        "branch": index["branch"],
        "commit_sha": index["commit_sha"],
        "file_count": index["file_count"],
        "truncated": index["truncated"] or index["file_count"] > MAX_TREE_ENTRIES,
        "entries": [{"path": p, "type": t, "size": s} for p, t, s in rows],
    }


def commits(token, owner, repo) -> dict:
    return {"commits": github_tools.list_commits(token, owner, repo, per_page=RECENT_COMMITS)}


def branches(token, owner, repo) -> dict:
    """Every branch, the default first, each with how far it is ahead of and
    behind the default branch."""
    default = github_tools._get(token, f"/repos/{owner}/{repo}").json()["default_branch"]
    data = github_tools._get(token, f"/repos/{owner}/{repo}/branches", params={"per_page": 100}).json()
    others = [b for b in data if b["name"] != default]

    def compare(name):
        try:
            c = github_tools._get(token, f"/repos/{owner}/{repo}/compare/{default}...{name}").json()
            return c.get("ahead_by"), c.get("behind_by")
        except requests.HTTPError:
            return None, None  # e.g. unrelated histories

    compared = others[:MAX_COMPARED_BRANCHES]
    with ThreadPoolExecutor(max_workers=6) as pool:
        counts = list(pool.map(compare, [b["name"] for b in compared]))
    counts += [(None, None)] * (len(others) - len(compared))

    base = f"https://github.com/{owner}/{repo}/tree/"
    default_protected = any(b["name"] == default and b.get("protected") for b in data)
    rows = [{"name": default, "default": True, "protected": default_protected, "ahead": 0, "behind": 0, "url": base + default}]
    rows += [
        {"name": b["name"], "default": False, "protected": b.get("protected", False), "ahead": a, "behind": bh, "url": base + b["name"]}
        for b, (a, bh) in zip(others, counts)
    ]
    return {"default_branch": default, "branches": rows}


def pulls(token, owner, repo) -> dict:
    data = github_tools._get(
        token,
        f"/repos/{owner}/{repo}/pulls",
        params={"state": "open", "sort": "updated", "direction": "desc", "per_page": OPEN_PULLS},
    ).json()
    return {
        "pulls": [
            {
                "number": p["number"],
                "title": p["title"],
                "author": p["user"]["login"],
                "avatar_url": p["user"]["avatar_url"],
                "draft": p.get("draft", False),
                "head": p["head"]["ref"],
                "base": p["base"]["ref"],
                "updated_at": p["updated_at"],
                "url": p["html_url"],
            }
            for p in data
        ]
    }


def contributors(token, owner, repo) -> dict:
    res = github_tools._get(token, f"/repos/{owner}/{repo}/contributors", params={"per_page": TOP_CONTRIBUTORS})
    # GitHub answers 204 with no body while it computes stats for a new repo.
    data = res.json() if res.content else []
    return {
        "contributors": [
            {"login": c["login"], "avatar_url": c["avatar_url"], "contributions": c["contributions"], "url": c["html_url"]}
            for c in data
            if c.get("type") != "Anonymous" and c.get("login")
        ]
    }


def issues(token, owner, repo) -> dict:
    data = github_tools._get(
        token,
        f"/repos/{owner}/{repo}/issues",
        params={"state": "open", "sort": "updated", "direction": "desc", "per_page": OPEN_ISSUES * 2},
    ).json()
    # GitHub lists pull requests as issues too; those have their own widget.
    only_issues = [i for i in data if "pull_request" not in i][:OPEN_ISSUES]
    return {
        "issues": [
            {
                "number": i["number"],
                "title": i["title"],
                "author": i["user"]["login"],
                "avatar_url": i["user"]["avatar_url"],
                "labels": [{"name": lb["name"], "color": lb.get("color") or ""} for lb in i.get("labels", [])][:3],
                "comments": i.get("comments", 0),
                "updated_at": i["updated_at"],
                "url": i["html_url"],
            }
            for i in only_issues
        ]
    }


def ci(token, owner, repo) -> dict:
    """The latest GitHub Actions run of each workflow."""
    data = github_tools._get(
        token, f"/repos/{owner}/{repo}/actions/runs", params={"per_page": RECENT_WORKFLOW_RUNS}
    ).json()
    latest = {}
    for run in data.get("workflow_runs", []):
        latest.setdefault(run["workflow_id"], run)
    return {
        "workflows": [
            {
                "name": r.get("name") or "Workflow",
                "status": r["status"],
                "conclusion": r.get("conclusion"),
                "branch": r.get("head_branch"),
                "updated_at": r["updated_at"],
                "url": r["html_url"],
            }
            for r in latest.values()
        ]
    }


def release(token, owner, repo) -> dict:
    """The latest release, and how many commits the default branch has
    gained since it."""
    res = requests.get(
        f"{github_tools.GITHUB_API}/repos/{owner}/{repo}/releases/latest",
        headers=github_tools._headers(token),
        timeout=15,
    )
    if res.status_code == 404:
        return {"release": None}
    github_tools._check(res)
    r = res.json()
    default = github_tools._get(token, f"/repos/{owner}/{repo}").json()["default_branch"]
    try:
        compare = github_tools._get(token, f"/repos/{owner}/{repo}/compare/{r['tag_name']}...{default}")
        since = compare.json().get("ahead_by")
    except requests.HTTPError:
        since = None
    return {
        "release": {
            "tag": r["tag_name"],
            "name": r.get("name") or r["tag_name"],
            "published_at": r.get("published_at"),
            "prerelease": r.get("prerelease", False),
            "commits_since": since,
            "default_branch": default,
            "url": r["html_url"],
        }
    }


def languages(token, owner, repo) -> dict:
    data = github_tools._get(token, f"/repos/{owner}/{repo}/languages").json()
    total = sum(data.values()) or 1
    return {
        "languages": [
            {"name": name, "percent": round(size * 100 / total, 1)}
            for name, size in sorted(data.items(), key=lambda kv: -kv[1])
        ]
    }


def summary(token, owner, repo) -> dict:
    # Always ask GitHub first: it proves this user can see the repo before
    # anything cached for it is handed back.
    info = github_tools.get_repository(token, owner, repo)
    key = info["full_name"].lower()
    with _lock:
        cached = _summaries.get(key)
    if cached and time.time() - cached[0] < SUMMARY_TTL_SECONDS:
        return cached[1]

    readme = github_tools.get_readme(token, owner, repo)
    overview = repo_index.overview(token, owner, repo) or {}
    facts = {k: info[k] for k in ("full_name", "description", "language", "topics")}
    context = (
        f"REPOSITORY: {facts}\n\n"
        f"TOP-LEVEL ENTRIES: {overview.get('top_level', [])[:60]}\n"
        f"FILE TYPES: {overview.get('file_types', {})}\n\n"
        "README:\n"
        + ((readme.get("content") or "")[:README_CHARS] if readme.get("found") else "(no README)")
    )
    raw = llm.complete_json(
        [{"role": "system", "content": SUMMARY_PROMPT}, {"role": "user", "content": context}],
        schema=SUMMARY_SCHEMA,
    )
    result = {
        "summary": llm.strip_emoji(str(raw.get("summary", ""))).strip(),
        "stack": [str(s).strip() for s in raw.get("stack", []) if str(s).strip()][:6],
        "questions": [str(q).strip() for q in raw.get("questions", []) if str(q).strip()][:4],
        "description": info["description"],
        "language": info["language"],
        "stars": info["stars"],
        "forks": info["forks"],
        "open_issues": info["open_issues"],
        "url": info["url"],
    }
    with _lock:
        _summaries[key] = (time.time(), result)
    return result
