"""Whole-codebase outline: every source file's classes and functions (with
signatures, line numbers, and the first line of their docstring or leading
comment), built from a single archive download instead of reading files one
by one. An agent explaining a project reads this plus the README and infers
what each part does from its names and signatures — then reads individual
functions only where it needs more detail.

Outlines are cached per commit, so asking again about an unchanged repo is
free."""

import io
import tarfile
import threading

import requests

import github_tools
import repo_index

# Paths that are dependencies or build output, not the project's own code.
SKIP_DIRS = {
    "node_modules", "dist", "build", "out", ".git", "vendor", "__pycache__", ".venv", "venv", "env",
    ".next", ".nuxt", "coverage", "target", "bin", "obj", ".idea", ".vscode", "site-packages",
}
MAX_ARCHIVE_BYTES = 80 * 1024 * 1024
MAX_SOURCE_FILE_BYTES = 400 * 1024
# Past this size the outline is summarized per directory and the agent is
# asked to narrow it with `path`, so one call never floods the context.
MAX_OUTLINE_CHARS = 60_000

_cache: dict[tuple, dict] = {}
_lock = threading.Lock()


def _doc_hint(lines, index, ext):
    """First line of a Python docstring right after a def/class, or of a
    comment right above a JS/TS/Java/Go function."""
    if ext == "py":
        for j in range(index + 1, min(index + 6, len(lines))):
            text = lines[j].strip()
            if not text:
                continue
            for quote in ('"""', "'''"):
                if text.startswith(quote):
                    body = text[3:].strip()
                    if body.endswith(quote):
                        body = body[:-3].strip()
                    if body:
                        return body[:140]
                    return lines[j + 1].strip()[:140] if j + 1 < len(lines) else ""
            if text.endswith(":") or text.endswith(","):
                continue  # still inside a multi-line signature
            return ""
        return ""
    for j in range(index - 1, max(index - 4, -1), -1):
        text = lines[j].strip()
        if text.startswith("//"):
            return text.lstrip("/ ").strip()[:140]
        if text.startswith("*") and not text.startswith("*/"):
            candidate = text.lstrip("* ").strip()
            if candidate and not candidate.startswith("@"):
                return candidate[:140]
        if text.startswith("/**"):
            return text[3:].strip(" */")[:140]
        if text and not text.startswith(("*", "/")):
            break
    return ""


def _outline_file(text, ext):
    lines = text.split("\n")
    patterns = github_tools.FUNCTION_PATTERNS[ext]
    symbols = []
    for i, line in enumerate(lines):
        for pattern, kind in patterns:
            m = pattern.match(line)
            if m:
                entry = {"line": i + 1, "kind": kind, "name": m.group("name"), "signature": line.strip()[:120]}
                doc = _doc_hint(lines, i, ext)
                if doc:
                    entry["doc"] = doc
                symbols.append(entry)
                break
    return len(lines), symbols


def _build(token, owner, repo, commit_sha):
    res = requests.get(
        f"{github_tools.GITHUB_API}/repos/{owner}/{repo}/tarball/{commit_sha}",
        headers=github_tools._headers(token),
        timeout=60,
        stream=True,
    )
    github_tools._check(res)
    buf = io.BytesIO()
    for chunk in res.iter_content(1024 * 256):
        buf.write(chunk)
        if buf.tell() > MAX_ARCHIVE_BYTES:
            raise ValueError("This repository is too large to outline in one go; use find_files and get_file_outline instead.")
    buf.seek(0)

    files, other = {}, 0
    with tarfile.open(fileobj=buf, mode="r:gz") as archive:
        for member in archive:
            if not member.isfile():
                continue
            # Archive paths start with "<owner>-<repo>-<sha>/".
            path = member.name.split("/", 1)[1] if "/" in member.name else member.name
            parts = path.split("/")
            if any(p in SKIP_DIRS for p in parts[:-1]) or parts[-1].endswith((".min.js", ".bundle.js")):
                continue
            ext = parts[-1].rsplit(".", 1)[-1].lower() if "." in parts[-1] else ""
            if ext not in github_tools.FUNCTION_PATTERNS or member.size > MAX_SOURCE_FILE_BYTES:
                other += 1
                continue
            text = archive.extractfile(member).read().decode("utf-8", errors="replace")
            total, symbols = _outline_file(text, ext)
            files[path] = {"lines": total, "symbols": symbols}
    return {"files": files, "other_files": other}


def get_codebase_outline(token, owner, repo, path="", ref=None):
    index = repo_index.ensure_index(token, owner, repo, branch=ref)
    key = (index["repository"].lower(), index["commit_sha"])
    with _lock:
        outline = _cache.get(key)
    if outline is None:
        outline = _build(token, owner, repo, index["commit_sha"])
        with _lock:
            _cache[key] = outline

    prefix = path.strip("/")
    selected = {
        p: info for p, info in sorted(outline["files"].items())
        if not prefix or p == prefix or p.startswith(prefix + "/")
    }
    result = {
        "repository": index["repository"],
        "branch": index["branch"],
        "commit_sha": index["commit_sha"][:12],
        "path": prefix or "/",
        "source_files": len(selected),
        "non_source_files": outline["other_files"],
        "files": [{"path": p, **info} for p, info in selected.items()],
    }
    if len(str(result)) <= MAX_OUTLINE_CHARS:
        return result

    # Too big for one answer: summarize by directory and ask for a narrower path.
    by_dir: dict[str, dict] = {}
    for p, info in selected.items():
        rel = p[len(prefix) + 1:] if prefix else p
        top = (prefix + "/" if prefix else "") + (rel.split("/", 1)[0] if "/" in rel else "(files here)")
        entry = by_dir.setdefault(top, {"files": 0, "functions_and_classes": 0, "examples": []})
        entry["files"] += 1
        entry["functions_and_classes"] += len(info["symbols"])
        if len(entry["examples"]) < 6:
            entry["examples"].extend(s["name"] for s in info["symbols"][:2])
    return {
        **{k: result[k] for k in ("repository", "branch", "commit_sha", "path", "source_files", "non_source_files")},
        "too_large": True,
        "note": "The outline is too large to show at once. Call again with `path` set to one of these directories.",
        "directories": by_dir,
    }


TOOL_FUNCTIONS = {"get_codebase_outline": get_codebase_outline}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_codebase_outline",
            "description": (
                "Outline a whole codebase (or one directory of it) in one call: for every source "
                "file, its classes and functions with signatures, line numbers, and the first line "
                "of their docstring/comment — no function bodies. Use this FIRST to understand or "
                "explain a project: infer what each part does from names, signatures, and docs, "
                "then read specific functions with get_function_source only where you need more "
                "detail. If the result says too_large, call again with a narrower path."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner"},
                    "repo": {"type": "string", "description": "Repository name"},
                    "path": {"type": "string", "description": "Directory to outline, e.g. 'backend'; empty for the whole repo"},
                    "ref": {"type": "string", "description": "Branch; defaults to the default branch"},
                },
                "required": ["owner", "repo"],
            },
        },
    }
]
