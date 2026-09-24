"""Which tools each agent can see. An agent is only ever handed the schemas
for its own set, and run() refuses anything outside it, so e.g. the Reader
cannot mutate a repository even if the model hallucinates a write call."""

import json

import codebase
import github_tools
import memory
import repo_index

INDEX_TOOLS = set(repo_index.TOOL_FUNCTIONS) | set(codebase.TOOL_FUNCTIONS)
MEMORY_TOOL = "query_memory"

# Everything read-only. get_repo_tree is replaced by the persistent index
# (find_files / list_directory), which never dumps a whole tree into context.
READER_TOOLS = (github_tools.READ_TOOLS - {"get_repo_tree"}) | INDEX_TOOLS | {MEMORY_TOOL}

# Mutations only; every one is proposed to the user before it runs.
WRITER_TOOLS = set(github_tools.WRITE_TOOLS)

# Results that carry actual source text. They're passed through whole
# (a single function or requested line range is never cut) and kept as
# evidence for the Writer, which can't read files itself.
CONTENT_TOOLS = {"get_function_source", "get_file_lines", "get_file_contents", "get_readme", "get_pull_request_diff"}

_SCHEMAS = {
    s["function"]["name"]: s for s in github_tools.TOOL_SCHEMAS + repo_index.TOOL_SCHEMAS + codebase.TOOL_SCHEMAS + [memory.TOOL_SCHEMA]
}
_FUNCTIONS = {**github_tools.TOOL_FUNCTIONS, **repo_index.TOOL_FUNCTIONS, **codebase.TOOL_FUNCTIONS}

# Plain listing results are trimmed at item boundaries past this size so the
# JSON stays valid; content results are never trimmed here.
LIST_RESULT_CHAR_LIMIT = 16000


def schemas(names):
    return [_SCHEMAS[n] for n in sorted(names)]


def run(name, ctx, arguments, allowed):
    if name not in allowed:
        raise PermissionError(f"Tool {name} is not available to this agent.")
    if name == MEMORY_TOOL:
        # Scoped by the run's user and chat, never by anything the model sends.
        return memory.run_query(ctx.user_id, ctx.conversation_id, arguments.get("sql", ""))
    return _FUNCTIONS[name](ctx.token, **arguments)


def serialize_result(name, result) -> str:
    """Serialize a tool result for the model. Listings are truncated at item
    boundaries (with explicit shown/total counts) so the model never sees a
    half-cut object it might 'complete' by inventing rows."""
    text = json.dumps(result, default=str)
    if name in CONTENT_TOOLS or len(text) <= LIST_RESULT_CHAR_LIMIT:
        return text
    if isinstance(result, list):
        lo, hi = 0, len(result)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            candidate = json.dumps(
                {"items": result[:mid], "truncated": True, "shown": mid, "total": len(result)}, default=str
            )
            if len(candidate) <= LIST_RESULT_CHAR_LIMIT:
                lo = mid
            else:
                hi = mid - 1
        return json.dumps({"items": result[:lo], "truncated": True, "shown": lo, "total": len(result)}, default=str)
    if isinstance(result, dict):
        for key in ("matches", "entries", "files", "diff"):
            value = result.get(key)
            if isinstance(value, list):
                kept = value
                while kept and len(json.dumps({**result, key: kept}, default=str)) > LIST_RESULT_CHAR_LIMIT:
                    kept = kept[: max(1, len(kept) * 3 // 4)] if len(kept) > 1 else []
                return json.dumps(
                    {**result, key: kept, "truncated": True, "shown": len(kept), "total": len(value)}, default=str
                )
    return json.dumps(
        {"truncated": True, "note": "Result too large; showing partial data.", "partial": text[:LIST_RESULT_CHAR_LIMIT]}
    )


def step_label(tool_name, arguments):
    path = arguments.get("path")
    builders = {
        "query_memory": lambda: f"Accessing memory: {arguments.get('purpose') or 'recalling earlier work'}",
        "get_codebase_outline": lambda: f"Outlining the codebase{' in ' + path if path else ''} (functions and classes)",
        "get_readme": lambda: "Reading the README",
        "get_repository": lambda: "Looking up repository details",
        "list_directory": lambda: f"Listing {path or 'the repository root'}",
        "find_files": lambda: f"Finding files matching \"{arguments.get('pattern', '')}\"",
        "get_file_outline": lambda: f"Scanning {path or 'a file'}",
        "get_function_source": lambda: f"Reading {arguments.get('function_name', 'a function')} in {path or 'a file'}",
        "get_file_lines": lambda: (
            f"Reading {path or 'a file'} lines {arguments.get('start_line')}-{arguments.get('end_line')}"
        ),
        "get_file_contents": lambda: f"Reading {path or 'a file'}",
        "list_commits": lambda: "Listing recent commits",
        "get_commit": lambda: "Looking at a commit",
        "compare_commits": lambda: "Comparing commits",
        "list_issues": lambda: "Listing issues",
        "get_issue": lambda: f"Reading issue #{arguments.get('issue_number')}",
        "list_pull_requests": lambda: "Listing pull requests",
        "get_pull_request": lambda: f"Reading pull request #{arguments.get('pull_number')}",
        "get_pull_request_diff": lambda: f"Reading the diff of pull request #{arguments.get('pull_number')}",
        "get_pull_request_files": lambda: f"Listing files changed in pull request #{arguments.get('pull_number')}",
        "search_code": lambda: "Searching code",
        "search_repositories": lambda: "Searching repositories",
        "web_search": lambda: f"Searching the web for \"{arguments.get('query', '')}\"",
        "list_user_repos": lambda: "Listing repositories",
        "get_me": lambda: "Looking up your GitHub profile",
        "list_branches": lambda: "Listing branches",
    }
    build = builders.get(tool_name)
    if build:
        return build()
    return f"Calling {tool_name.replace('_', ' ')}"
