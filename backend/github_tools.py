import base64
import re

import requests
from ddgs import DDGS

GITHUB_API = "https://api.github.com"

# Heuristic, regex-based function/class detectors per file extension. Not a
# real parser, but enough to build a cheap outline (names + line numbers)
# without shipping full file contents to the model.
FUNCTION_PATTERNS = {
    "py": [
        (re.compile(r"^\s*(?:async\s+)?def\s+(?P<name>\w+)\s*\("), "function"),
        (re.compile(r"^\s*class\s+(?P<name>\w+)"), "class"),
    ],
    "js": [
        (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(?P<name>\w+)\s*\("), "function"),
        (re.compile(r"^\s*(?:export\s+)?const\s+(?P<name>\w+)\s*=\s*(?:async\s*)?\("), "function"),
        (re.compile(r"^\s*(?:export\s+)?class\s+(?P<name>\w+)"), "class"),
    ],
    "java": [
        (
            re.compile(r"^\s*(?:public|private|protected|static|final|\s)*[\w<>\[\],\s]+\s+(?P<name>\w+)\s*\([^;{}]*\)\s*\{"),
            "method",
        ),
        (re.compile(r"^\s*(?:public|private|protected)?\s*(?:static\s+)?class\s+(?P<name>\w+)"), "class"),
    ],
    "go": [
        (re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?(?P<name>\w+)\s*\("), "function"),
    ],
}
FUNCTION_PATTERNS["jsx"] = FUNCTION_PATTERNS["js"]
FUNCTION_PATTERNS["ts"] = FUNCTION_PATTERNS["js"]
FUNCTION_PATTERNS["tsx"] = FUNCTION_PATTERNS["js"]

READ_TOOLS = {
    "list_commits",
    "get_commit",
    "get_file_contents",
    "get_readme",
    "get_repo_tree",
    "get_file_outline",
    "get_function_source",
    "list_branches",
    "list_issues",
    "list_pull_requests",
    "get_pull_request",
    "get_pull_request_diff",
    "search_code",
    "list_releases",
    "get_me",
    "get_user",
    "list_user_repos",
    "get_repository",
    "list_organizations",
    "search_repositories",
    "search_issues",
    "search_users",
    "get_issue",
    "list_issue_comments",
    "get_pull_request_files",
    "list_pull_request_reviews",
    "list_pull_request_commits",
    "compare_commits",
    "list_tags",
    "get_latest_release",
    "list_contributors",
    "list_forks",
    "list_stargazers",
    "list_workflows",
    "list_workflow_runs",
    "web_search",
}

WRITE_TOOLS = {
    "create_branch",
    "create_or_update_file",
    "delete_file",
    "create_pull_request",
    "merge_pull_request",
    "create_issue",
    "add_issue_comment",
    "fork_repository",
    "create_repository",
    "star_repository",
    "unstar_repository",
    "update_issue",
    "update_pull_request",
    "submit_pull_request_review",
    "request_pull_request_reviewers",
    "create_release",
    "delete_branch",
    "add_collaborator",
}


def _headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get(token, path, params=None, accept=None):
    headers = _headers(token)
    if accept:
        headers["Accept"] = accept
    res = requests.get(f"{GITHUB_API}{path}", headers=headers, params=params, timeout=15)
    res.raise_for_status()
    return res


def list_commits(token, owner, repo, branch=None, path=None, per_page=10):
    params = {"per_page": per_page}
    if branch:
        params["sha"] = branch
    if path:
        params["path"] = path
    data = _get(token, f"/repos/{owner}/{repo}/commits", params=params).json()
    return [
        {
            "sha": c["sha"][:7],
            "message": c["commit"]["message"].split("\n")[0],
            "author": c["commit"]["author"]["name"],
            "date": c["commit"]["author"]["date"],
            "url": c["html_url"],
        }
        for c in data
    ]


def get_commit(token, owner, repo, sha):
    data = _get(token, f"/repos/{owner}/{repo}/commits/{sha}").json()
    return {
        "sha": data["sha"],
        "message": data["commit"]["message"],
        "author": data["commit"]["author"]["name"],
        "date": data["commit"]["author"]["date"],
        "stats": data.get("stats"),
        "files": [
            {"filename": f["filename"], "status": f["status"], "changes": f["changes"]}
            for f in data.get("files", [])[:20]
        ],
        "url": data["html_url"],
    }


def _fetch_raw_file(token, owner, repo, path, ref=None):
    """Fetch a file's full decoded text, uncapped. Returns None if path is a
    directory. Internal helper for tools that need to scan a whole file
    locally (outline extraction, function lookup) without shipping the whole
    thing to the model."""
    params = {"ref": ref} if ref else None
    data = _get(token, f"/repos/{owner}/{repo}/contents/{path}", params=params).json()
    if isinstance(data, list):
        return None
    if data.get("encoding") == "base64" and data.get("content"):
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    return ""


def get_file_contents(token, owner, repo, path, ref=None):
    params = {"ref": ref} if ref else None
    data = _get(token, f"/repos/{owner}/{repo}/contents/{path}", params=params).json()
    if isinstance(data, list):
        return {"type": "dir", "entries": [{"name": e["name"], "type": e["type"], "path": e["path"]} for e in data]}
    content = ""
    if data.get("encoding") == "base64" and data.get("content"):
        content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    return {"type": "file", "path": data["path"], "sha": data["sha"], "content": content[:8000]}


def get_readme(token, owner, repo, ref=None):
    """Get a repository's README content. Resolves the actual filename/casing
    for you (README.md, Readme, readme.rst, ...) — use this instead of
    guessing a path with get_file_contents when asked to describe a project."""
    params = {"ref": ref} if ref else None
    res = requests.get(
        f"{GITHUB_API}/repos/{owner}/{repo}/readme", headers=_headers(token), params=params, timeout=15
    )
    if res.status_code == 404:
        return {"found": False}
    res.raise_for_status()
    data = res.json()
    content = ""
    if data.get("encoding") == "base64" and data.get("content"):
        content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    return {"found": True, "path": data.get("path"), "content": content[:8000], "url": data.get("html_url")}


def get_repo_tree(token, owner, repo, ref=None):
    """Get the full recursive list of file paths in a repository (names and
    types only, no content). Use this to see a project's structure when the
    README is missing or too thin to answer the question, before deciding
    which specific files are worth inspecting."""
    branch = ref or _default_branch(token, owner, repo)
    data = _get(token, f"/repos/{owner}/{repo}/git/trees/{branch}", params={"recursive": "1"}).json()
    tree = data.get("tree", [])
    limit = 300
    files = [
        {"path": item["path"], "type": "dir" if item["type"] == "tree" else "file", "size": item.get("size")}
        for item in tree[:limit]
    ]
    return {
        "truncated": bool(data.get("truncated")) or len(tree) > limit,
        "total_entries": len(tree),
        "files": files,
    }


def get_file_outline(token, owner, repo, path, ref=None):
    """Get the function/class names and line numbers defined in one source
    file, WITHOUT returning the full file body. Use this to understand what
    a file does cheaply, instead of get_file_contents, when exploring a
    project. If the user later asks about one specific function, follow up
    with get_function_source for just that one."""
    text = _fetch_raw_file(token, owner, repo, path, ref=ref)
    if text is None:
        return {"type": "dir", "path": path}

    lines = text.split("\n")
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    patterns = FUNCTION_PATTERNS.get(ext)

    if not patterns:
        return {
            "path": path,
            "language": ext or "unknown",
            "outline_supported": False,
            "total_lines": len(lines),
            "preview": text[:800],
        }

    symbols = []
    for i, line in enumerate(lines, start=1):
        for pattern, kind in patterns:
            m = pattern.match(line)
            if m:
                symbols.append({"name": m.group("name"), "kind": kind, "line": i, "signature": line.strip()[:160]})
                break

    return {"path": path, "language": ext, "outline_supported": True, "total_lines": len(lines), "symbols": symbols}


def get_function_source(token, owner, repo, path, function_name, ref=None):
    """Get the exact source of one function, method, or class from a file by
    name, without reading the rest of the file. Use this after
    get_file_outline has shown you what a file contains and you need the
    real implementation of one specific symbol (e.g. the user asks how a
    function works)."""
    text = _fetch_raw_file(token, owner, repo, path, ref=ref)
    if text is None:
        return {"error": f"{path} is a directory, not a file."}

    lines = text.split("\n")
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    patterns = FUNCTION_PATTERNS.get(ext)
    if not patterns:
        return {"error": f"Outline extraction isn't supported for .{ext} files; use get_file_contents instead."}

    start = None
    start_indent = 0
    for i, line in enumerate(lines):
        for pattern, _kind in patterns:
            m = pattern.match(line)
            if m and m.group("name") == function_name:
                start = i
                start_indent = len(line) - len(line.lstrip())
                break
        if start is not None:
            break

    if start is None:
        return {"error": f"No function, method, or class named '{function_name}' found in {path}."}

    if ext == "py":
        end = len(lines)
        for j in range(start + 1, len(lines)):
            stripped = lines[j].strip()
            if stripped and (len(lines[j]) - len(lines[j].lstrip())) <= start_indent:
                end = j
                break
        snippet = "\n".join(lines[start:end])
    else:
        end = start
        depth = 0
        opened = False
        for j in range(start, len(lines)):
            depth += lines[j].count("{") - lines[j].count("}")
            if "{" in lines[j]:
                opened = True
            end = j
            if opened and depth <= 0:
                break
        snippet = "\n".join(lines[start : end + 1])

    return {"path": path, "name": function_name, "start_line": start + 1, "content": snippet[:4000]}


def list_branches(token, owner, repo):
    data = _get(token, f"/repos/{owner}/{repo}/branches", params={"per_page": 50}).json()
    return [{"name": b["name"], "protected": b.get("protected", False)} for b in data]


def list_issues(token, owner, repo, state="open"):
    data = _get(token, f"/repos/{owner}/{repo}/issues", params={"state": state, "per_page": 20}).json()
    return [
        {"number": i["number"], "title": i["title"], "state": i["state"], "url": i["html_url"], "user": i["user"]["login"]}
        for i in data
        if "pull_request" not in i
    ]


def list_pull_requests(token, owner, repo, state="open"):
    data = _get(token, f"/repos/{owner}/{repo}/pulls", params={"state": state, "per_page": 20}).json()
    return [
        {
            "number": p["number"],
            "title": p["title"],
            "state": p["state"],
            "head": p["head"]["ref"],
            "base": p["base"]["ref"],
            "url": p["html_url"],
        }
        for p in data
    ]


def get_pull_request(token, owner, repo, pull_number):
    p = _get(token, f"/repos/{owner}/{repo}/pulls/{pull_number}").json()
    return {
        "number": p["number"],
        "title": p["title"],
        "state": p["state"],
        "body": (p.get("body") or "")[:2000],
        "head": p["head"]["ref"],
        "base": p["base"]["ref"],
        "additions": p.get("additions"),
        "deletions": p.get("deletions"),
        "changed_files": p.get("changed_files"),
        "url": p["html_url"],
    }


def get_pull_request_diff(token, owner, repo, pull_number):
    res = _get(token, f"/repos/{owner}/{repo}/pulls/{pull_number}", accept="application/vnd.github.v3.diff")
    return {"diff": res.text[:10000]}


def search_code(token, query, owner=None, repo=None):
    q = query
    if owner and repo:
        q += f" repo:{owner}/{repo}"
    data = _get(token, "/search/code", params={"q": q, "per_page": 15}).json()
    return [
        {"path": item["path"], "repo": item["repository"]["full_name"], "url": item["html_url"]}
        for item in data.get("items", [])
    ]


def list_releases(token, owner, repo):
    data = _get(token, f"/repos/{owner}/{repo}/releases", params={"per_page": 10}).json()
    return [
        {"tag": r["tag_name"], "name": r["name"], "published_at": r["published_at"], "url": r["html_url"]}
        for r in data
    ]


def _user_summary(u):
    return {
        "login": u.get("login"),
        "name": u.get("name"),
        "bio": u.get("bio"),
        "company": u.get("company"),
        "location": u.get("location"),
        "email": u.get("email"),
        "public_repos": u.get("public_repos"),
        "followers": u.get("followers"),
        "following": u.get("following"),
        "created_at": u.get("created_at"),
        "avatar_url": u.get("avatar_url"),
        "url": u.get("html_url"),
    }


def get_me(token):
    """The authenticated user's own GitHub profile. Use this whenever the user
    refers to themselves ('my profile', 'my repos', 'mine') instead of asking
    for a username."""
    return _user_summary(_get(token, "/user").json())


def get_user(token, username):
    """A public GitHub profile for a specific username (not the current user)."""
    return _user_summary(_get(token, f"/users/{username}").json())


def list_user_repos(token, username=None, per_page=100):
    """Repos owned by a user. Omit username to list the authenticated user's
    own repos ('my repos')."""
    path = f"/users/{username}/repos" if username else "/user/repos"
    data = _get(token, path, params={"per_page": per_page, "sort": "updated"}).json()
    return [
        {
            "full_name": r["full_name"],
            "description": r.get("description"),
            "private": r["private"],
            "stars": r.get("stargazers_count"),
            "language": r.get("language"),
            "updated_at": r.get("updated_at"),
            "url": r["html_url"],
        }
        for r in data
    ]


def get_repository(token, owner, repo):
    r = _get(token, f"/repos/{owner}/{repo}").json()
    return {
        "full_name": r["full_name"],
        "description": r.get("description"),
        "private": r["private"],
        "default_branch": r.get("default_branch"),
        "stars": r.get("stargazers_count"),
        "forks": r.get("forks_count"),
        "open_issues": r.get("open_issues_count"),
        "language": r.get("language"),
        "topics": r.get("topics", []),
        "created_at": r.get("created_at"),
        "updated_at": r.get("updated_at"),
        "url": r["html_url"],
    }


def list_organizations(token):
    """Organizations the authenticated user belongs to."""
    data = _get(token, "/user/orgs").json()
    return [{"login": o["login"], "description": o.get("description")} for o in data]


def search_repositories(token, query, per_page=15):
    data = _get(token, "/search/repositories", params={"q": query, "per_page": per_page}).json()
    return [
        {
            "full_name": item["full_name"],
            "description": item.get("description"),
            "stars": item.get("stargazers_count"),
            "url": item["html_url"],
        }
        for item in data.get("items", [])
    ]


def search_issues(token, query, per_page=15):
    data = _get(token, "/search/issues", params={"q": query, "per_page": per_page}).json()
    return [
        {
            "number": item["number"],
            "title": item["title"],
            "state": item["state"],
            "is_pull_request": "pull_request" in item,
            "url": item["html_url"],
        }
        for item in data.get("items", [])
    ]


def search_users(token, query, per_page=15):
    data = _get(token, "/search/users", params={"q": query, "per_page": per_page}).json()
    return [{"login": item["login"], "type": item.get("type"), "url": item["html_url"]} for item in data.get("items", [])]


def web_search(token, query, max_results=5):
    """Search the public web via DuckDuckGo. Doesn't touch GitHub at all
    (token is unused, kept only for the run_tool(name, token, **args)
    calling convention every tool shares) — this is for things no GitHub
    tool can answer: general docs, error messages, library/tech questions,
    or anything outside the selected repo, its issues/PRs, or its users."""
    try:
        results = DDGS().text(query, max_results=max_results)
    except Exception as exc:
        return {"error": f"Web search failed: {exc}"}
    return [
        {"title": r.get("title"), "url": r.get("href"), "snippet": r.get("body")}
        for r in results
    ]


def get_issue(token, owner, repo, issue_number):
    i = _get(token, f"/repos/{owner}/{repo}/issues/{issue_number}").json()
    return {
        "number": i["number"],
        "title": i["title"],
        "state": i["state"],
        "body": (i.get("body") or "")[:2000],
        "user": i["user"]["login"],
        "labels": [label["name"] for label in i.get("labels", [])],
        "comments": i.get("comments"),
        "url": i["html_url"],
    }


def list_issue_comments(token, owner, repo, issue_number):
    data = _get(token, f"/repos/{owner}/{repo}/issues/{issue_number}/comments").json()
    return [{"user": c["user"]["login"], "body": c["body"][:1000], "created_at": c["created_at"]} for c in data]


def get_pull_request_files(token, owner, repo, pull_number):
    data = _get(token, f"/repos/{owner}/{repo}/pulls/{pull_number}/files", params={"per_page": 50}).json()
    return [
        {"filename": f["filename"], "status": f["status"], "additions": f["additions"], "deletions": f["deletions"]}
        for f in data
    ]


def list_pull_request_reviews(token, owner, repo, pull_number):
    data = _get(token, f"/repos/{owner}/{repo}/pulls/{pull_number}/reviews").json()
    return [
        {"user": r["user"]["login"], "state": r["state"], "body": (r.get("body") or "")[:500]}
        for r in data
    ]


def list_pull_request_commits(token, owner, repo, pull_number):
    data = _get(token, f"/repos/{owner}/{repo}/pulls/{pull_number}/commits", params={"per_page": 50}).json()
    return [
        {"sha": c["sha"][:7], "message": c["commit"]["message"].split("\n")[0], "author": c["commit"]["author"]["name"]}
        for c in data
    ]


def compare_commits(token, owner, repo, base, head):
    data = _get(token, f"/repos/{owner}/{repo}/compare/{base}...{head}").json()
    return {
        "status": data.get("status"),
        "ahead_by": data.get("ahead_by"),
        "behind_by": data.get("behind_by"),
        "total_commits": data.get("total_commits"),
        "files": [{"filename": f["filename"], "status": f["status"]} for f in data.get("files", [])[:30]],
        "url": data.get("html_url"),
    }


def list_tags(token, owner, repo):
    data = _get(token, f"/repos/{owner}/{repo}/tags", params={"per_page": 30}).json()
    return [{"name": t["name"], "sha": t["commit"]["sha"][:7]} for t in data]


def get_latest_release(token, owner, repo):
    r = _get(token, f"/repos/{owner}/{repo}/releases/latest").json()
    return {"tag": r["tag_name"], "name": r["name"], "published_at": r["published_at"], "url": r["html_url"]}


def list_contributors(token, owner, repo):
    data = _get(token, f"/repos/{owner}/{repo}/contributors", params={"per_page": 30}).json()
    return [{"login": c["login"], "contributions": c["contributions"]} for c in data]


def list_forks(token, owner, repo):
    data = _get(token, f"/repos/{owner}/{repo}/forks", params={"per_page": 20}).json()
    return [{"full_name": f["full_name"], "stars": f.get("stargazers_count"), "url": f["html_url"]} for f in data]


def list_stargazers(token, owner, repo):
    data = _get(token, f"/repos/{owner}/{repo}/stargazers", params={"per_page": 30}).json()
    return [{"login": s["login"], "url": s["html_url"]} for s in data]


def list_workflows(token, owner, repo):
    data = _get(token, f"/repos/{owner}/{repo}/actions/workflows").json()
    return [{"id": w["id"], "name": w["name"], "state": w["state"]} for w in data.get("workflows", [])]


def list_workflow_runs(token, owner, repo, workflow_id=None, per_page=15):
    path = (
        f"/repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs"
        if workflow_id
        else f"/repos/{owner}/{repo}/actions/runs"
    )
    data = _get(token, path, params={"per_page": per_page}).json()
    return [
        {
            "id": r["id"],
            "name": r.get("name"),
            "status": r["status"],
            "conclusion": r.get("conclusion"),
            "branch": r.get("head_branch"),
            "url": r["html_url"],
        }
        for r in data.get("workflow_runs", [])
    ]


def _default_branch(token, owner, repo):
    return _get(token, f"/repos/{owner}/{repo}").json()["default_branch"]


def _file_sha(token, owner, repo, path, ref=None):
    params = {"ref": ref} if ref else None
    res = requests.get(
        f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}",
        headers=_headers(token),
        params=params,
        timeout=15,
    )
    if res.status_code == 404:
        return None
    res.raise_for_status()
    data = res.json()
    return data.get("sha") if isinstance(data, dict) else None


def create_branch(token, owner, repo, branch, from_branch=None):
    base = from_branch or _default_branch(token, owner, repo)
    ref_data = _get(token, f"/repos/{owner}/{repo}/git/ref/heads/{base}").json()
    sha = ref_data["object"]["sha"]
    res = requests.post(
        f"{GITHUB_API}/repos/{owner}/{repo}/git/refs",
        headers=_headers(token),
        json={"ref": f"refs/heads/{branch}", "sha": sha},
        timeout=15,
    )
    res.raise_for_status()
    return {"branch": branch, "from": base, "sha": sha}


def create_or_update_file(token, owner, repo, path, content, message, branch=None):
    sha = _file_sha(token, owner, repo, path, ref=branch)
    body = {"message": message, "content": base64.b64encode(content.encode()).decode()}
    if branch:
        body["branch"] = branch
    if sha:
        body["sha"] = sha
    res = requests.put(
        f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}", headers=_headers(token), json=body, timeout=15
    )
    res.raise_for_status()
    data = res.json()
    return {"path": path, "commit_sha": data["commit"]["sha"], "url": data["content"]["html_url"]}


def delete_file(token, owner, repo, path, message, branch=None):
    sha = _file_sha(token, owner, repo, path, ref=branch)
    if not sha:
        raise ValueError(f"File not found: {path}")
    body = {"message": message, "sha": sha}
    if branch:
        body["branch"] = branch
    res = requests.delete(
        f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}", headers=_headers(token), json=body, timeout=15
    )
    res.raise_for_status()
    return {"path": path, "deleted": True}


def create_pull_request(token, owner, repo, title, head, base, body=None):
    res = requests.post(
        f"{GITHUB_API}/repos/{owner}/{repo}/pulls",
        headers=_headers(token),
        json={"title": title, "head": head, "base": base, "body": body or ""},
        timeout=15,
    )
    res.raise_for_status()
    p = res.json()
    return {"number": p["number"], "url": p["html_url"]}


def merge_pull_request(token, owner, repo, pull_number, merge_method="merge"):
    res = requests.put(
        f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pull_number}/merge",
        headers=_headers(token),
        json={"merge_method": merge_method},
        timeout=15,
    )
    res.raise_for_status()
    return res.json()


def create_issue(token, owner, repo, title, body=None):
    res = requests.post(
        f"{GITHUB_API}/repos/{owner}/{repo}/issues",
        headers=_headers(token),
        json={"title": title, "body": body or ""},
        timeout=15,
    )
    res.raise_for_status()
    i = res.json()
    return {"number": i["number"], "url": i["html_url"]}


def add_issue_comment(token, owner, repo, issue_number, body):
    res = requests.post(
        f"{GITHUB_API}/repos/{owner}/{repo}/issues/{issue_number}/comments",
        headers=_headers(token),
        json={"body": body},
        timeout=15,
    )
    res.raise_for_status()
    c = res.json()
    return {"url": c["html_url"]}


def fork_repository(token, owner, repo):
    res = requests.post(f"{GITHUB_API}/repos/{owner}/{repo}/forks", headers=_headers(token), timeout=15)
    res.raise_for_status()
    f = res.json()
    return {"full_name": f["full_name"], "url": f["html_url"]}


def create_repository(token, name, description=None, private=False):
    res = requests.post(
        f"{GITHUB_API}/user/repos",
        headers=_headers(token),
        json={"name": name, "description": description or "", "private": private},
        timeout=15,
    )
    res.raise_for_status()
    r = res.json()
    return {"full_name": r["full_name"], "url": r["html_url"]}


def star_repository(token, owner, repo):
    res = requests.put(f"{GITHUB_API}/user/starred/{owner}/{repo}", headers=_headers(token), timeout=15)
    res.raise_for_status()
    return {"starred": f"{owner}/{repo}"}


def unstar_repository(token, owner, repo):
    res = requests.delete(f"{GITHUB_API}/user/starred/{owner}/{repo}", headers=_headers(token), timeout=15)
    res.raise_for_status()
    return {"unstarred": f"{owner}/{repo}"}


def update_issue(token, owner, repo, issue_number, title=None, body=None, state=None):
    payload = {k: v for k, v in {"title": title, "body": body, "state": state}.items() if v is not None}
    res = requests.patch(
        f"{GITHUB_API}/repos/{owner}/{repo}/issues/{issue_number}", headers=_headers(token), json=payload, timeout=15
    )
    res.raise_for_status()
    i = res.json()
    return {"number": i["number"], "state": i["state"], "url": i["html_url"]}


def update_pull_request(token, owner, repo, pull_number, title=None, body=None, state=None, base=None):
    payload = {
        k: v for k, v in {"title": title, "body": body, "state": state, "base": base}.items() if v is not None
    }
    res = requests.patch(
        f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pull_number}", headers=_headers(token), json=payload, timeout=15
    )
    res.raise_for_status()
    p = res.json()
    return {"number": p["number"], "state": p["state"], "url": p["html_url"]}


def submit_pull_request_review(token, owner, repo, pull_number, event, body=None):
    """event must be one of APPROVE, REQUEST_CHANGES, COMMENT."""
    res = requests.post(
        f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pull_number}/reviews",
        headers=_headers(token),
        json={"event": event, "body": body or ""},
        timeout=15,
    )
    res.raise_for_status()
    r = res.json()
    return {"id": r["id"], "state": r["state"]}


def request_pull_request_reviewers(token, owner, repo, pull_number, reviewers):
    res = requests.post(
        f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pull_number}/requested_reviewers",
        headers=_headers(token),
        json={"reviewers": reviewers},
        timeout=15,
    )
    res.raise_for_status()
    return {"requested_reviewers": reviewers}


def create_release(token, owner, repo, tag_name, name=None, body=None, draft=False, prerelease=False):
    res = requests.post(
        f"{GITHUB_API}/repos/{owner}/{repo}/releases",
        headers=_headers(token),
        json={
            "tag_name": tag_name,
            "name": name or tag_name,
            "body": body or "",
            "draft": draft,
            "prerelease": prerelease,
        },
        timeout=15,
    )
    res.raise_for_status()
    r = res.json()
    return {"tag": r["tag_name"], "url": r["html_url"]}


def delete_branch(token, owner, repo, branch):
    res = requests.delete(
        f"{GITHUB_API}/repos/{owner}/{repo}/git/refs/heads/{branch}", headers=_headers(token), timeout=15
    )
    res.raise_for_status()
    return {"branch": branch, "deleted": True}


def add_collaborator(token, owner, repo, username, permission="push"):
    res = requests.put(
        f"{GITHUB_API}/repos/{owner}/{repo}/collaborators/{username}",
        headers=_headers(token),
        json={"permission": permission},
        timeout=15,
    )
    res.raise_for_status()
    return {"invited": username, "permission": permission}


TOOL_FUNCTIONS = {
    "list_commits": list_commits,
    "get_commit": get_commit,
    "get_file_contents": get_file_contents,
    "get_readme": get_readme,
    "get_repo_tree": get_repo_tree,
    "get_file_outline": get_file_outline,
    "get_function_source": get_function_source,
    "list_branches": list_branches,
    "list_issues": list_issues,
    "list_pull_requests": list_pull_requests,
    "get_pull_request": get_pull_request,
    "get_pull_request_diff": get_pull_request_diff,
    "search_code": search_code,
    "list_releases": list_releases,
    "get_me": get_me,
    "get_user": get_user,
    "list_user_repos": list_user_repos,
    "get_repository": get_repository,
    "list_organizations": list_organizations,
    "search_repositories": search_repositories,
    "search_issues": search_issues,
    "search_users": search_users,
    "get_issue": get_issue,
    "list_issue_comments": list_issue_comments,
    "get_pull_request_files": get_pull_request_files,
    "list_pull_request_reviews": list_pull_request_reviews,
    "list_pull_request_commits": list_pull_request_commits,
    "compare_commits": compare_commits,
    "list_tags": list_tags,
    "get_latest_release": get_latest_release,
    "list_contributors": list_contributors,
    "list_forks": list_forks,
    "list_stargazers": list_stargazers,
    "list_workflows": list_workflows,
    "list_workflow_runs": list_workflow_runs,
    "web_search": web_search,
    "create_branch": create_branch,
    "create_or_update_file": create_or_update_file,
    "delete_file": delete_file,
    "create_pull_request": create_pull_request,
    "merge_pull_request": merge_pull_request,
    "create_issue": create_issue,
    "add_issue_comment": add_issue_comment,
    "fork_repository": fork_repository,
    "create_repository": create_repository,
    "star_repository": star_repository,
    "unstar_repository": unstar_repository,
    "update_issue": update_issue,
    "update_pull_request": update_pull_request,
    "submit_pull_request_review": submit_pull_request_review,
    "request_pull_request_reviewers": request_pull_request_reviewers,
    "create_release": create_release,
    "delete_branch": delete_branch,
    "add_collaborator": add_collaborator,
}


def run_tool(name, token, arguments):
    return TOOL_FUNCTIONS[name](token, **arguments)


def _prop(type_="string", description=""):
    return {"type": type_, "description": description}


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_commits",
            "description": "List recent commits on a branch, optionally filtered to a file path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": _prop(description="Repository owner"),
                    "repo": _prop(description="Repository name"),
                    "branch": _prop(description="Branch name, defaults to the repo's default branch"),
                    "path": _prop(description="Only show commits touching this file path"),
                    "per_page": _prop("integer", "How many commits to return (default 10)"),
                },
                "required": ["owner", "repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_commit",
            "description": "Get full details of a single commit, including changed files and stats.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": _prop(),
                    "repo": _prop(),
                    "sha": _prop(description="Commit SHA (full or short)"),
                },
                "required": ["owner", "repo", "sha"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_file_contents",
            "description": "Read a file's contents (or list a directory) at a given path and ref.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": _prop(),
                    "repo": _prop(),
                    "path": _prop(description="File or directory path in the repo"),
                    "ref": _prop(description="Branch, tag, or commit SHA; defaults to the default branch"),
                },
                "required": ["owner", "repo", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_readme",
            "description": (
                "Get a repository's README content. Automatically resolves the actual "
                "filename and casing — use this instead of guessing a path with "
                "get_file_contents whenever asked to describe, summarize, or explain a project."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": _prop(),
                    "repo": _prop(),
                    "ref": _prop(description="Branch, tag, or commit SHA; defaults to the default branch"),
                },
                "required": ["owner", "repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_repo_tree",
            "description": (
                "Get the full recursive list of file paths in a repository (names and "
                "types only, no content). Use this to see the project's structure when "
                "the README is missing or too thin to answer the question."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": _prop(),
                    "repo": _prop(),
                    "ref": _prop(description="Branch, tag, or commit SHA; defaults to the default branch"),
                },
                "required": ["owner", "repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_file_outline",
            "description": (
                "Get the function/class names and line numbers defined in one source "
                "file, without returning the full file body. Use this instead of "
                "get_file_contents to cheaply understand what a file does. Follow up "
                "with get_function_source for one specific symbol if needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": _prop(),
                    "repo": _prop(),
                    "path": _prop(description="File path in the repo"),
                    "ref": _prop(description="Branch, tag, or commit SHA; defaults to the default branch"),
                },
                "required": ["owner", "repo", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_function_source",
            "description": (
                "Get the exact source of one function, method, or class from a file "
                "by name, without reading the rest of the file. Use this after "
                "get_file_outline has shown you what a file contains and the user "
                "asks about one specific function's implementation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": _prop(),
                    "repo": _prop(),
                    "path": _prop(description="File path in the repo"),
                    "function_name": _prop(description="Name of the function, method, or class to extract"),
                    "ref": _prop(description="Branch, tag, or commit SHA; defaults to the default branch"),
                },
                "required": ["owner", "repo", "path", "function_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_branches",
            "description": "List branches in a repository.",
            "parameters": {
                "type": "object",
                "properties": {"owner": _prop(), "repo": _prop()},
                "required": ["owner", "repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_issues",
            "description": "List issues in a repository (excludes pull requests).",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": _prop(),
                    "repo": _prop(),
                    "state": _prop(description="open, closed, or all (default open)"),
                },
                "required": ["owner", "repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_pull_requests",
            "description": "List pull requests in a repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": _prop(),
                    "repo": _prop(),
                    "state": _prop(description="open, closed, or all (default open)"),
                },
                "required": ["owner", "repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pull_request",
            "description": "Get details of a single pull request.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": _prop(),
                    "repo": _prop(),
                    "pull_number": _prop("integer", "Pull request number"),
                },
                "required": ["owner", "repo", "pull_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pull_request_diff",
            "description": "Get the unified diff for a pull request.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": _prop(),
                    "repo": _prop(),
                    "pull_number": _prop("integer", "Pull request number"),
                },
                "required": ["owner", "repo", "pull_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Search code across GitHub, optionally scoped to one repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": _prop(description="Search query"),
                    "owner": _prop(description="Optional repository owner to scope the search"),
                    "repo": _prop(description="Optional repository name to scope the search"),
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_releases",
            "description": "List releases for a repository.",
            "parameters": {
                "type": "object",
                "properties": {"owner": _prop(), "repo": _prop()},
                "required": ["owner", "repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_branch",
            "description": "Create a new branch from an existing one. This modifies the repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": _prop(),
                    "repo": _prop(),
                    "branch": _prop(description="Name of the new branch"),
                    "from_branch": _prop(description="Branch to create from; defaults to the default branch"),
                },
                "required": ["owner", "repo", "branch"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_or_update_file",
            "description": "Create a new file or update an existing file's contents. This modifies the repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": _prop(),
                    "repo": _prop(),
                    "path": _prop(description="File path to write"),
                    "content": _prop(description="Full new file content (plain text)"),
                    "message": _prop(description="Commit message"),
                    "branch": _prop(description="Branch to commit to; defaults to the default branch"),
                },
                "required": ["owner", "repo", "path", "content", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a file from the repository. This modifies the repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": _prop(),
                    "repo": _prop(),
                    "path": _prop(description="File path to delete"),
                    "message": _prop(description="Commit message"),
                    "branch": _prop(description="Branch to commit to; defaults to the default branch"),
                },
                "required": ["owner", "repo", "path", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_pull_request",
            "description": "Open a new pull request. This modifies the repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": _prop(),
                    "repo": _prop(),
                    "title": _prop(),
                    "head": _prop(description="Branch containing the changes"),
                    "base": _prop(description="Branch to merge into"),
                    "body": _prop(description="Pull request description"),
                },
                "required": ["owner", "repo", "title", "head", "base"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "merge_pull_request",
            "description": "Merge an open pull request. This modifies the repository and cannot be undone from the chat.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": _prop(),
                    "repo": _prop(),
                    "pull_number": _prop("integer", "Pull request number"),
                    "merge_method": _prop(description="merge, squash, or rebase (default merge)"),
                },
                "required": ["owner", "repo", "pull_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_issue",
            "description": "Open a new issue. This modifies the repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": _prop(),
                    "repo": _prop(),
                    "title": _prop(),
                    "body": _prop(description="Issue description"),
                },
                "required": ["owner", "repo", "title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_issue_comment",
            "description": "Add a comment to an issue or pull request. This modifies the repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": _prop(),
                    "repo": _prop(),
                    "issue_number": _prop("integer", "Issue or pull request number"),
                    "body": _prop(description="Comment text"),
                },
                "required": ["owner", "repo", "issue_number", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_me",
            "description": (
                "Get the signed-in user's own GitHub profile. Use this whenever the user refers to "
                "themselves ('my profile', 'my repos', 'mine', 'I') instead of asking for a username."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_user",
            "description": "Get a public GitHub profile for a specific username (not the current user).",
            "parameters": {
                "type": "object",
                "properties": {"username": _prop(description="GitHub username")},
                "required": ["username"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_user_repos",
            "description": "List repositories owned by a user. Omit username for the signed-in user's own repos.",
            "parameters": {
                "type": "object",
                "properties": {
                    "username": _prop(description="Omit to list the signed-in user's own repos"),
                    "per_page": _prop("integer", "How many to return (default 100, GitHub's max per page)"),
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_repository",
            "description": "Get metadata about a repository: description, stars, forks, default branch, topics.",
            "parameters": {
                "type": "object",
                "properties": {"owner": _prop(), "repo": _prop()},
                "required": ["owner", "repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_organizations",
            "description": "List organizations the signed-in user belongs to.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_repositories",
            "description": "Search GitHub for repositories.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": _prop(description="Search query, e.g. 'language:python stars:>1000'"),
                    "per_page": _prop("integer", "How many to return (default 15)"),
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_issues",
            "description": "Search GitHub for issues and pull requests.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": _prop(description="Search query, e.g. 'repo:owner/name is:open label:bug'"),
                    "per_page": _prop("integer", "How many to return (default 15)"),
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_users",
            "description": "Search GitHub for user or organization accounts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": _prop(description="Search query"),
                    "per_page": _prop("integer", "How many to return (default 15)"),
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_issue",
            "description": "Get full details of a single issue.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": _prop(),
                    "repo": _prop(),
                    "issue_number": _prop("integer", "Issue number"),
                },
                "required": ["owner", "repo", "issue_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_issue_comments",
            "description": "List comments on an issue or pull request.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": _prop(),
                    "repo": _prop(),
                    "issue_number": _prop("integer", "Issue or pull request number"),
                },
                "required": ["owner", "repo", "issue_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pull_request_files",
            "description": "List the files changed by a pull request, with add/delete counts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": _prop(),
                    "repo": _prop(),
                    "pull_number": _prop("integer", "Pull request number"),
                },
                "required": ["owner", "repo", "pull_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_pull_request_reviews",
            "description": "List reviews left on a pull request.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": _prop(),
                    "repo": _prop(),
                    "pull_number": _prop("integer", "Pull request number"),
                },
                "required": ["owner", "repo", "pull_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_pull_request_commits",
            "description": "List commits included in a pull request.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": _prop(),
                    "repo": _prop(),
                    "pull_number": _prop("integer", "Pull request number"),
                },
                "required": ["owner", "repo", "pull_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_commits",
            "description": "Compare two branches, tags, or commits and summarize the difference.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": _prop(),
                    "repo": _prop(),
                    "base": _prop(description="Base branch, tag, or commit SHA"),
                    "head": _prop(description="Head branch, tag, or commit SHA"),
                },
                "required": ["owner", "repo", "base", "head"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tags",
            "description": "List tags in a repository.",
            "parameters": {
                "type": "object",
                "properties": {"owner": _prop(), "repo": _prop()},
                "required": ["owner", "repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_latest_release",
            "description": "Get the most recent published release for a repository.",
            "parameters": {
                "type": "object",
                "properties": {"owner": _prop(), "repo": _prop()},
                "required": ["owner", "repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_contributors",
            "description": "List contributors to a repository, ranked by number of commits.",
            "parameters": {
                "type": "object",
                "properties": {"owner": _prop(), "repo": _prop()},
                "required": ["owner", "repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_forks",
            "description": "List forks of a repository.",
            "parameters": {
                "type": "object",
                "properties": {"owner": _prop(), "repo": _prop()},
                "required": ["owner", "repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_stargazers",
            "description": "List users who have starred a repository.",
            "parameters": {
                "type": "object",
                "properties": {"owner": _prop(), "repo": _prop()},
                "required": ["owner", "repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_workflows",
            "description": "List GitHub Actions workflows defined in a repository.",
            "parameters": {
                "type": "object",
                "properties": {"owner": _prop(), "repo": _prop()},
                "required": ["owner", "repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_workflow_runs",
            "description": "List recent GitHub Actions workflow runs, optionally for one workflow.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": _prop(),
                    "repo": _prop(),
                    "workflow_id": _prop("integer", "Optional workflow id to filter to"),
                },
                "required": ["owner", "repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the public web via DuckDuckGo. Not scoped to GitHub at all — "
                "use this only for things no GitHub tool can answer (general docs, "
                "error messages, library/framework questions, background on a "
                "technology), never to look something up inside the selected repo "
                "or its issues/PRs/users."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": _prop(description="Search query"),
                    "max_results": _prop("integer", "How many results to return (default 5)"),
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fork_repository",
            "description": "Fork a repository into the signed-in user's account. This modifies GitHub state.",
            "parameters": {
                "type": "object",
                "properties": {"owner": _prop(), "repo": _prop()},
                "required": ["owner", "repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_repository",
            "description": "Create a new repository owned by the signed-in user. This modifies GitHub state.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": _prop(description="Repository name"),
                    "description": _prop(description="Short description"),
                    "private": _prop("boolean", "Whether the repo should be private (default false)"),
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "star_repository",
            "description": "Star a repository as the signed-in user. This modifies GitHub state.",
            "parameters": {
                "type": "object",
                "properties": {"owner": _prop(), "repo": _prop()},
                "required": ["owner", "repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unstar_repository",
            "description": "Remove a star from a repository as the signed-in user. This modifies GitHub state.",
            "parameters": {
                "type": "object",
                "properties": {"owner": _prop(), "repo": _prop()},
                "required": ["owner", "repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_issue",
            "description": (
                "Edit an issue's title, body, or state (open/closed). This modifies the repository."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": _prop(),
                    "repo": _prop(),
                    "issue_number": _prop("integer", "Issue number"),
                    "title": _prop(description="New title"),
                    "body": _prop(description="New body"),
                    "state": _prop(description="open or closed"),
                },
                "required": ["owner", "repo", "issue_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_pull_request",
            "description": (
                "Edit a pull request's title, body, base branch, or state (open/closed). "
                "This modifies the repository."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": _prop(),
                    "repo": _prop(),
                    "pull_number": _prop("integer", "Pull request number"),
                    "title": _prop(description="New title"),
                    "body": _prop(description="New body"),
                    "state": _prop(description="open or closed"),
                    "base": _prop(description="New base branch"),
                },
                "required": ["owner", "repo", "pull_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_pull_request_review",
            "description": "Approve, request changes on, or comment on a pull request. This modifies the repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": _prop(),
                    "repo": _prop(),
                    "pull_number": _prop("integer", "Pull request number"),
                    "event": _prop(description="One of APPROVE, REQUEST_CHANGES, COMMENT"),
                    "body": _prop(description="Review comment text"),
                },
                "required": ["owner", "repo", "pull_number", "event"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_pull_request_reviewers",
            "description": "Request one or more GitHub users review a pull request. This modifies the repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": _prop(),
                    "repo": _prop(),
                    "pull_number": _prop("integer", "Pull request number"),
                    "reviewers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "GitHub usernames to request review from",
                    },
                },
                "required": ["owner", "repo", "pull_number", "reviewers"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_release",
            "description": "Publish a new release/tag for a repository. This modifies the repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": _prop(),
                    "repo": _prop(),
                    "tag_name": _prop(description="Tag to create the release from, e.g. v1.2.0"),
                    "name": _prop(description="Release title; defaults to the tag name"),
                    "body": _prop(description="Release notes"),
                    "draft": _prop("boolean", "Create as a draft (default false)"),
                    "prerelease": _prop("boolean", "Mark as a prerelease (default false)"),
                },
                "required": ["owner", "repo", "tag_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_branch",
            "description": "Permanently delete a branch. This modifies the repository and cannot be undone.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": _prop(),
                    "repo": _prop(),
                    "branch": _prop(description="Branch name to delete"),
                },
                "required": ["owner", "repo", "branch"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_collaborator",
            "description": "Invite a user as a collaborator on a repository. This modifies the repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": _prop(),
                    "repo": _prop(),
                    "username": _prop(description="GitHub username to invite"),
                    "permission": _prop(description="pull, push, or admin (default push)"),
                },
                "required": ["owner", "repo", "username"],
            },
        },
    },
]
