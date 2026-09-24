"""Persistent repository index: each branch's file tree (paths, types,
sizes, blob shas) stored in Postgres and stamped with the commit it was
built from. File contents are never stored — agents fetch source only when
they need it.

Freshness: every use looks up the branch's head commit with the *user's*
token. That one small request both proves the user can still see the repo
and tells us whether the stored tree is stale; the tree is only refetched
when the commit moved."""

import hashlib
import threading
import time

import requests

import github_tools
from db import repos as store

# Within one run an agent may browse the index many times; re-checking the
# head commit on each call would be wasted requests.
HEAD_CHECK_TTL_SECONDS = 20
_head_checks: dict[tuple, tuple[float, dict]] = {}
_lock = threading.Lock()
# A tree's overview never changes for a given commit, so compute it once.
_overviews: dict[tuple, dict] = {}

# Tools whose success changes a branch's file tree.
TREE_CHANGING_TOOLS = {
    "edit_file",
    "create_or_update_file",
    "restore_file",
    "delete_file",
    "create_branch",
    "delete_branch",
    "merge_pull_request",
}


def _memo_key(token, full_name, branch):
    return (hashlib.sha256(token.encode()).hexdigest()[:16], full_name.lower(), branch or "")


def _register_repository(token, owner, repo):
    info = github_tools._get(token, f"/repos/{owner}/{repo}").json()
    repository_id = store.upsert_repository(
        info["id"], info["owner"]["login"], info["name"], info["default_branch"], info["private"]
    )
    return {
        "id": repository_id,
        "github_repo_id": info["id"],
        "owner": info["owner"]["login"],
        "name": info["name"],
        "default_branch": info["default_branch"],
        "tree": None,
    }


def _head_commit(token, owner, repo, branch):
    res = github_tools._get(
        token, f"/repos/{owner}/{repo}/commits/{branch}", accept="application/vnd.github.sha"
    )
    return res.text.strip()


def _fetch_entries(token, owner, repo, commit_sha):
    data = github_tools._get(
        token, f"/repos/{owner}/{repo}/git/trees/{commit_sha}", params={"recursive": "1"}
    ).json()
    entries = []
    for item in data.get("tree", []):
        if item["type"] not in ("blob", "tree"):
            continue  # submodules
        path = item["path"]
        entries.append(
            {
                "path": path,
                "type": "dir" if item["type"] == "tree" else "file",
                "size": item.get("size"),
                "sha": item.get("sha"),
                "parent_path": path.rsplit("/", 1)[0] if "/" in path else "",
            }
        )
    return data["sha"], bool(data.get("truncated")), entries


def ensure_index(token, owner, repo, branch=None, force_check=False) -> dict:
    """Return the up-to-date index for owner/repo@branch (default branch if
    omitted), refreshing it from GitHub only if the head commit changed.
    Normally one database query plus one small GitHub request. Raises
    requests.HTTPError if the user can't access the repo or branch."""
    full_name = f"{owner}/{repo}"
    key = _memo_key(token, full_name, branch)
    if not force_check:
        with _lock:
            cached = _head_checks.get(key)
        if cached and time.time() - cached[0] < HEAD_CHECK_TTL_SECONDS:
            return cached[1]

    record = store.lookup(full_name, branch) or _register_repository(token, owner, repo)
    target = branch or record["default_branch"]
    try:
        head = _head_commit(token, record["owner"], record["name"], target)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if branch is not None or status not in (404, 422):
            raise
        # The default branch may have been renamed since we stored it.
        record = _register_repository(token, owner, repo)
        target = record["default_branch"]
        head = _head_commit(token, record["owner"], record["name"], target)
        record["tree"] = store.get_tree(record["id"], target)

    tree = record["tree"]
    refreshed = False
    if not tree or tree["commit_sha"] != head:
        tree_sha, truncated, entries = _fetch_entries(token, record["owner"], record["name"], head)
        tree_id = store.replace_tree(record["id"], target, head, tree_sha, truncated, entries)
        tree = {"id": tree_id, "commit_sha": head, "tree_sha": tree_sha, "truncated": truncated, "file_count": len(entries)}
        refreshed = True

    result = {
        "repository": f"{record['owner']}/{record['name']}",
        "repository_id": record["id"],
        "tree_id": tree["id"],
        "branch": target,
        "is_default_branch": target == record["default_branch"],
        "commit_sha": tree["commit_sha"],
        "file_count": tree["file_count"],
        "truncated": tree["truncated"],
        "refreshed": refreshed,
    }
    with _lock:
        _head_checks[key] = (time.time(), result)
        if branch is None:
            _head_checks[_memo_key(token, full_name, target)] = (time.time(), result)
    return result


def invalidate(owner, repo):
    full_name = f"{owner}/{repo}".lower()
    with _lock:
        for key in [k for k in _head_checks if k[1] == full_name]:
            del _head_checks[key]


def refresh_after_write(token, tool, args) -> dict | None:
    """After a successful write that changes files or branches, bring the
    affected branch's index up to date (the head commit moved, so this
    refetches its tree). Returns the new index state, or None."""
    if tool not in TREE_CHANGING_TOOLS:
        return None
    owner, repo = args.get("owner"), args.get("repo")
    if not owner or not repo:
        return None
    invalidate(owner, repo)
    if tool == "delete_branch":
        return None
    branch = None if tool == "merge_pull_request" else (args.get("branch") or None)
    try:
        return ensure_index(token, owner, repo, branch=branch, force_check=True)
    except requests.HTTPError:
        return None


def overview(token, owner, repo, branch=None) -> dict | None:
    """Compact orientation for the planner and agents: top-level entries,
    shallow directories, and file-type counts — not the whole tree."""
    try:
        index = ensure_index(token, owner, repo, branch=branch)
    except requests.HTTPError:
        return None
    memo_key = (index["tree_id"], index["commit_sha"])
    with _lock:
        cached = _overviews.get(memo_key)
    if cached is None:
        cached = store.overview(index["tree_id"])
        with _lock:
            _overviews[memo_key] = cached
    return {**cached, **{k: index[k] for k in ("repository", "branch", "commit_sha", "file_count", "truncated")}}


# --- index tools exposed to agents ------------------------------------------


def list_directory(token, owner, repo, path="", ref=None):
    index = ensure_index(token, owner, repo, branch=ref)
    entries = store.list_directory(index["tree_id"], path.strip("/"))
    return {
        "branch": index["branch"],
        "commit_sha": index["commit_sha"][:12],
        "path": path.strip("/") or "/",
        "entries": [{**e, "path": e["path"] + ("/" if e["type"] == "dir" else "")} for e in entries],
    }


def find_files(token, owner, repo, pattern, ref=None):
    index = ensure_index(token, owner, repo, branch=ref)
    matches = store.find_files(index["tree_id"], pattern)
    return {
        "branch": index["branch"],
        "pattern": pattern,
        "match_count": len(matches),
        "matches": [m["path"] + ("/" if m["type"] == "dir" else "") for m in matches],
        **({"index_truncated": True} if index["truncated"] else {}),
    }


TOOL_FUNCTIONS = {"list_directory": list_directory, "find_files": find_files}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": (
                "List the files and folders directly inside one directory of a repository, "
                "from Git Show's stored index (fast, always current with the branch head). "
                "Use an empty path for the repository root."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner"},
                    "repo": {"type": "string", "description": "Repository name"},
                    "path": {"type": "string", "description": "Directory path, e.g. 'backend' or '' for root"},
                    "ref": {"type": "string", "description": "Branch name; defaults to the default branch"},
                },
                "required": ["owner", "repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": (
                "Find files or folders in a repository whose path matches a pattern, from Git "
                "Show's stored index. Plain text matches anywhere in the path "
                "(case-insensitive); '*' is a wildcard, e.g. '*auth*.py' or 'src/*/index.ts'. "
                "Use this to locate files instead of guessing paths or searching all of GitHub."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner"},
                    "repo": {"type": "string", "description": "Repository name"},
                    "pattern": {"type": "string", "description": "Text or wildcard pattern to match paths against"},
                    "ref": {"type": "string", "description": "Branch name; defaults to the default branch"},
                },
                "required": ["owner", "repo", "pattern"],
            },
        },
    },
]
