"""Architecture analysis: a repository's components and how they connect,
for the architecture diagram.

Built from the same single archive download as the codebase outline: every
source file's functions and classes plus its real imports, and the files
that say how the project is built, deployed, and stores data. Imports are
resolved to files in the repository, giving a deterministic module graph;
the model then groups the code into components, connections, and key flows,
using that graph as evidence. The result is stored per repository, so
opening it again never re-runs the analysis; it only runs again when asked.

Analyses run in a background thread (one at a time per repository), since
they take longer than a proxy will hold a request open."""

import logging
import posixpath
import re
import sys
import threading
from collections import Counter, defaultdict
from datetime import datetime, timezone

import codebase
import github_tools
import llm
import repo_index
from db import repos as store

log = logging.getLogger("gitshow.architecture")

ARCHITECTURE_VERSION = 1
# Directory depth for the module graph, reduced until it has at most this
# many modules, so a big repo still gives a readable diagram.
MODULE_START_DEPTH = 3
MAX_MODULES = 40
# How much of the repository the model is shown.
README_CHARS = 5000
MANIFEST_TOTAL_CHARS = 14000
MANIFEST_CHARS = 1500
OUTLINE_TOTAL_CHARS = 30000
FILES_PER_MODULE = 25
SYMBOLS_PER_FILE = 8
MAX_MODULE_EDGES = 90
MAX_EXTERNAL = 40

COMPONENT_KINDS = [
    "frontend", "backend", "service", "worker", "agent", "data", "storage", "external", "infra",
    "library", "other",
]
CONNECTION_KINDS = ["calls", "http", "imports", "reads", "writes", "renders", "events", "deploys", "auth"]

ARCH_PROMPT = (
    "You are a software architect. From a repository's README, its build/deploy/config files, an "
    "outline of its source files grouped by directory (functions and classes, no bodies), and its "
    "real import graph, describe its architecture for an architecture diagram.\n\n"
    "Rules:\n"
    "- 4 to 14 components. A component is a meaningful unit: an app (e.g. the web frontend), a "
    "service or API server, a subsystem inside one (e.g. the agent layer, data access, auth), a "
    "data store (a database, cache, object storage), or an external system the code talks to (a "
    "third-party API, an LLM provider, GitHub). Group by responsibility; never one component per "
    "file.\n"
    "- Ground everything in the input. Only name databases, APIs, services, and libraries that the "
    "files, manifests, config, or README actually show. Never invent.\n"
    "- paths: the directories or files, written exactly as they appear in the input, that make up "
    "each component; empty for external systems and data stores outside the repository. Every "
    "source directory should belong to some component.\n"
    "- id: a short, unique, lowercase slug (letters, digits, hyphens).\n"
    "- name: short, at most 3 words (e.g. 'Agent core', 'Tool registry', 'Supabase'); put the "
    "detail in description.\n"
    "- Never create a component for tests, build scripts, CI, or config files: skip them "
    "entirely, they are not part of the architecture.\n"
    "- connections: real interactions — which component calls which (in-process or over HTTP), "
    "which reads or writes which store, which renders what, what is deployed where. Direction is "
    "from the initiator. label says what flows, in a few words (e.g. 'REST /api/*', 'SQL via "
    "psycopg', 'tool calls'). Use the import graph as evidence for in-process dependencies.\n"
    "- connection kind: 'calls' for in-process calls between parts of the same program; 'http' "
    "only for network requests (REST, OAuth, third-party APIs); 'reads' or 'writes' for data "
    "stores; 'renders' for UI; 'events' for streams, websockets, or queues; 'deploys' for "
    "hosting; 'auth' for sign-in.\n"
    "- flows: the 2 to 4 most important end-to-end flows (e.g. what happens when a user sends a "
    "message), each as 3 to 7 short steps that name components.\n"
    "- summary: 2 to 4 plain sentences on the overall architecture.\n"
    "- description: 1 to 2 sentences. responsibilities: 2 to 5 short items. tech: the main "
    "languages, frameworks, or libraries it uses.\n"
    "Plain text in every field: no markdown, no emoji."
)

ARCH_SCHEMA = {
    "title": "architecture",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "components": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "kind": {"type": "string", "enum": COMPONENT_KINDS},
                    "description": {"type": "string"},
                    "responsibilities": {"type": "array", "items": {"type": "string"}},
                    "paths": {"type": "array", "items": {"type": "string"}},
                    "tech": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "name", "kind", "description", "responsibilities", "paths", "tech"],
            },
        },
        "connections": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "source": {"type": "string"},
                    "target": {"type": "string"},
                    "label": {"type": "string"},
                    "kind": {"type": "string", "enum": CONNECTION_KINDS},
                },
                "required": ["source", "target", "label", "kind"],
            },
        },
        "flows": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "steps": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "steps"],
            },
        },
    },
    "required": ["summary", "components", "connections", "flows"],
}

_STDLIB = set(getattr(sys, "stdlib_module_names", ()))
_NODE_BUILTINS = {
    "fs", "path", "os", "http", "https", "url", "crypto", "child_process", "util", "events", "stream",
    "zlib", "net", "tls", "dns", "buffer", "assert", "readline", "worker_threads", "process", "querystring",
}
_JS_SUFFIXES = ["", ".js", ".jsx", ".ts", ".tsx", "/index.js", "/index.jsx", "/index.ts", "/index.tsx"]
_GO_MODULE = re.compile(r"^module\s+(\S+)", re.M)

_starting = threading.Lock()


# --- the import graph ----------------------------------------------------------


def _shared_prefix(a, b):
    n = 0
    for x, y in zip(a.split("/")[:-1], b.split("/")):
        if x != y:
            break
        n += 1
    return n


def _python_index(paths):
    """Every dotted name a .py file could be imported as (each suffix of its
    path, since the import root varies), mapped to the files it could be."""
    index = defaultdict(list)
    for p in paths:
        if not p.endswith(".py"):
            continue
        parts = p[:-3].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        for i in range(len(parts)):
            index[".".join(parts[i:])].append(p)
    return index


def _resolve_python(importer, imp, files, py_index):
    module, names = imp["module"], imp.get("names") or []
    if module.startswith("."):
        level = len(module) - len(module.lstrip("."))
        rest = module.lstrip(".")
        base = importer.split("/")[:-1]
        base = base[: len(base) - (level - 1)] if level > 1 else base
        targets = [f"{rest}.{n}" if rest else n for n in names] + ([rest] if rest else [])
        found = []
        for t in targets:
            stem = "/".join(base + t.split("."))
            for candidate in (stem + ".py", stem + "/__init__.py"):
                if candidate in files:
                    found.append(candidate)
                    break
        return found, None
    if module.split(".")[0] in _STDLIB:
        return [], None
    found = []
    for t in [f"{module}.{n}" for n in names] + [module]:
        candidates = py_index.get(t)
        if candidates:
            found.append(max(candidates, key=lambda c: (_shared_prefix(importer, c), -len(c))))
            if t == module:
                break
    if found:
        return found, None
    return [], module.split(".")[0]


def _resolve_js(importer, spec, files):
    if spec.startswith("node:") or spec in _NODE_BUILTINS:
        return [], None
    if spec.startswith((".", "@/", "~/")):
        if spec.startswith("."):
            target = posixpath.normpath(posixpath.join(posixpath.dirname(importer), spec))
        else:
            parts = importer.split("/")
            if "src" not in parts:
                return [], None
            target = "/".join(parts[: parts.index("src") + 1] + [spec[2:]])
        stems = [target]
        if target.endswith(".js"):
            stems.append(target[:-3])  # TypeScript ESM imports name the compiled .js file
        for stem in stems:
            for suffix in _JS_SUFFIXES:
                if stem + suffix in files:
                    return [stem + suffix], None
        return [], None  # a stylesheet, image, or something outside the outline
    name = "/".join(spec.split("/")[:2]) if spec.startswith("@") else spec.split("/")[0]
    return [], name


def _resolve_go(spec, go_modules, dirs):
    for module_path, root in go_modules:
        if spec == module_path or spec.startswith(module_path + "/"):
            rel = spec[len(module_path):].strip("/")
            target = "/".join(p for p in (root, rel) if p)
            return ([target] if target in dirs else []), None
    first = spec.split("/")[0]
    if "." not in first:
        return [], None  # the standard library
    return [], "/".join(spec.split("/")[:3])


def _resolve_java(spec, java_files):
    if spec.startswith(("java.", "javax.", "jdk.", "sun.")):
        return [], None
    tail = spec.replace(".", "/") + ".java"
    for p in java_files:
        if p == tail or p.endswith("/" + tail):
            return [p], None
    return [], ".".join(spec.split(".")[:2])


def import_graph(outline):
    """(edges, external): edges are (importer file, imported file or
    directory) inside the repository; external counts how many files import
    each outside package."""
    files = set(outline["files"])
    dirs = {posixpath.dirname(p) for p in files}
    py_index = _python_index(files)
    java_files = [p for p in files if p.endswith(".java")]
    go_modules = []
    for path, text in outline.get("manifests", {}).items():
        if path.rsplit("/", 1)[-1] == "go.mod":
            m = _GO_MODULE.search(text)
            if m:
                go_modules.append((m.group(1), posixpath.dirname(path)))

    edges, external = set(), Counter()
    for path, info in outline["files"].items():
        ext = path.rsplit(".", 1)[-1].lower()
        used_outside = set()
        for imp in info.get("imports", []):
            if ext == "py":
                found, outside = _resolve_python(path, imp, files, py_index)
            elif ext in ("js", "jsx", "ts", "tsx"):
                found, outside = _resolve_js(path, imp["module"], files)
            elif ext == "go":
                found, outside = _resolve_go(imp["module"], go_modules, dirs)
            elif ext == "java":
                found, outside = _resolve_java(imp["module"], java_files)
            else:
                found, outside = [], None
            edges.update((path, f) for f in found if f != path)
            if outside:
                used_outside.add(outside)
        external.update(used_outside)
    return sorted(edges), external


# --- the module graph ----------------------------------------------------------


def _module_of(path, depth):
    parts = path.split("/")[:-1]
    return "/".join(parts[:depth]) if parts else "(root)"


def module_graph(outline, edges):
    files = outline["files"]
    depth = MODULE_START_DEPTH
    while depth > 1 and len({_module_of(p, depth) for p in files}) > MAX_MODULES:
        depth -= 1

    def module_for(target):
        # A directory target (Go) is a module path already; reduce it to depth.
        if target in files:
            return _module_of(target, depth)
        return "/".join(target.split("/")[:depth]) or "(root)"

    modules = defaultdict(lambda: {"files": 0, "lines": 0, "symbols": 0, "paths": []})
    for path, info in files.items():
        m = modules[_module_of(path, depth)]
        m["files"] += 1
        m["lines"] += info["lines"]
        m["symbols"] += len(info["symbols"])
        m["paths"].append(path)

    counts = Counter()
    for src, dst in edges:
        a, b = _module_of(src, depth), module_for(dst)
        if a != b and b in modules:
            counts[(a, b)] += 1
    return dict(modules), counts


# --- the model's input ---------------------------------------------------------


def _symbols_text(symbols):
    shown = []
    for s in symbols[:SYMBOLS_PER_FILE]:
        shown.append(f"{s['name']} — {s['doc']}" if s.get("doc") and len(shown) < 3 else s["name"])
    more = len(symbols) - SYMBOLS_PER_FILE
    return ", ".join(shown) + (f", +{more} more" if more > 0 else "")


def _context(repository, branch, readme, outline, modules, module_edges, external):
    parts = [f"REPOSITORY: {repository} (branch {branch})", "README:\n" + (readme[:README_CHARS] if readme else "(none)")]

    manifests, used = [], 0
    for path, text in sorted(outline.get("manifests", {}).items()):
        block = f"--- {path} ---\n{text[:MANIFEST_CHARS]}"
        if used + len(block) > MANIFEST_TOTAL_CHARS:
            manifests.append(f"--- {path} --- (omitted)")
            continue
        manifests.append(block)
        used += len(block)
    parts.append("BUILD, DEPLOY, CONFIG, AND SCHEMA FILES:\n" + ("\n".join(manifests) or "(none)"))

    lines, used, cut = [], 0, False
    for name in sorted(modules):
        m = modules[name]
        lines.append(f"[{name}/] {m['files']} files, {m['lines']} lines")
        for path in sorted(m["paths"])[:FILES_PER_MODULE]:
            info = outline["files"][path]
            row = f"  {path} ({info['lines']} lines): {_symbols_text(info['symbols']) or '(no functions or classes)'}"
            if used + len(row) > OUTLINE_TOTAL_CHARS:
                cut = True
                break
            lines.append(row)
            used += len(row)
        if len(m["paths"]) > FILES_PER_MODULE:
            lines.append(f"  … and {len(m['paths']) - FILES_PER_MODULE} more files")
        if cut:
            lines.append("(the rest of the outline is omitted for length)")
            break
    parts.append("SOURCE OUTLINE BY DIRECTORY:\n" + "\n".join(lines))

    edge_lines = [f"{a} -> {b}: {n}" for (a, b), n in module_edges.most_common(MAX_MODULE_EDGES)]
    parts.append("IMPORT GRAPH (importing directory -> imported directory: files importing):\n" + ("\n".join(edge_lines) or "(none found)"))
    ext_lines = [f"{name}: {n}" for name, n in external.most_common(MAX_EXTERNAL)]
    parts.append("EXTERNAL PACKAGES (package: files importing it):\n" + ("\n".join(ext_lines) or "(none found)"))
    return "\n\n".join(parts)


# --- turning the model's answer into a diagram --------------------------------


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:40] or "component"


def _validate(raw):
    components = raw.get("components")
    if not isinstance(components, list) or not components:
        raise ValueError("no components")
    if not all(isinstance(c, dict) and c.get("name") for c in components):
        raise ValueError("every component needs a name")


def _clean_list(values, limit, chars=120):
    return [str(v).strip()[:chars] for v in (values or []) if str(v).strip()][:limit]


def _build_architecture(raw, outline, edges, modules, module_edges, external):
    files = outline["files"]
    known = set(files) | set(outline.get("manifests", {}))
    for p in list(known):
        while "/" in p:
            p = p.rsplit("/", 1)[0]
            known.add(p)

    components, ids = [], set()
    for c in raw["components"]:
        cid = base = _slug(c.get("id") or c.get("name"))
        n = 2
        while cid in ids:
            cid, n = f"{base}-{n}", n + 1
        ids.add(cid)
        paths = []
        for p in c.get("paths") or []:
            p = str(p).strip().strip("/")
            if p in known and p not in paths:
                paths.append(p)
        components.append(
            {
                "id": cid,
                "source_id": str(c.get("id") or ""),
                "name": str(c["name"]).strip()[:60],
                "kind": c.get("kind") if c.get("kind") in COMPONENT_KINDS else "other",
                "description": str(c.get("description") or "").strip()[:400],
                "responsibilities": _clean_list(c.get("responsibilities"), 6),
                "paths": paths,
                "tech": _clean_list(c.get("tech"), 8, 40),
            }
        )
    by_source_id = {c["source_id"]: c["id"] for c in components if c["source_id"]}
    by_source_id.update({c["id"]: c["id"] for c in components})

    # Each source file belongs to the component claiming its longest path.
    claims = sorted(((p, c["id"]) for c in components for p in c["paths"]), key=lambda pc: -len(pc[0]))

    def owner(path):
        for p, cid in claims:
            if path == p or path.startswith(p + "/"):
                return cid
        return None

    metrics = defaultdict(lambda: {"files": 0, "lines": 0, "symbols": 0})
    file_owner = {}
    for path, info in files.items():
        cid = owner(path)
        file_owner[path] = cid
        if cid:
            metrics[cid]["files"] += 1
            metrics[cid]["lines"] += info["lines"]
            metrics[cid]["symbols"] += len(info["symbols"])
    for c in components:
        c["metrics"] = metrics[c["id"]]
        del c["source_id"]

    import_counts = Counter()
    for src, dst in edges:
        a, b = file_owner.get(src), owner(dst)
        if a and b and a != b:
            import_counts[(a, b)] += 1

    connections, seen = [], set()
    for conn in raw.get("connections") or []:
        s, t = by_source_id.get(str(conn.get("source"))), by_source_id.get(str(conn.get("target")))
        if not s or not t or s == t or (s, t) in seen:
            continue
        seen.add((s, t))
        connections.append(
            {
                "id": f"{s}->{t}",
                "source": s,
                "target": t,
                "label": str(conn.get("label") or "").strip()[:60],
                "kind": conn.get("kind") if conn.get("kind") in CONNECTION_KINDS else "calls",
                "imports": import_counts.get((s, t), 0),
                "inferred": False,
            }
        )
    # Dependencies the import graph proves but the model left out.
    for (s, t), n in import_counts.most_common():
        if (s, t) in seen or (t, s) in seen:
            continue
        seen.add((s, t))
        connections.append(
            {"id": f"{s}->{t}", "source": s, "target": t, "label": f"imports ({n})", "kind": "imports", "imports": n, "inferred": True}
        )

    module_nodes = []
    for name, m in sorted(modules.items()):
        owners = Counter(file_owner.get(p) for p in m["paths"] if file_owner.get(p))
        module_nodes.append(
            {
                "id": name,
                "files": m["files"],
                "lines": m["lines"],
                "symbols": m["symbols"],
                "component": owners.most_common(1)[0][0] if owners else None,
                "paths": sorted(m["paths"])[:200],
            }
        )

    languages = Counter(p.rsplit(".", 1)[-1].lower() for p in files)
    return {
        "summary": str(raw.get("summary") or "").strip()[:1200],
        "components": components,
        "connections": connections,
        "flows": [
            {"name": str(f.get("name") or "").strip()[:80], "steps": _clean_list(f.get("steps"), 8, 200)}
            for f in (raw.get("flows") or [])[:6]
            if f.get("name")
        ],
        "modules": {
            "nodes": module_nodes,
            "edges": [{"source": a, "target": b, "count": n} for (a, b), n in module_edges.most_common()],
        },
        "external": [{"name": name, "uses": n} for name, n in external.most_common(MAX_EXTERNAL)],
        "stats": {
            "source_files": len(files),
            "other_files": outline.get("other_files", 0),
            "lines": sum(i["lines"] for i in files.values()),
            "symbols": sum(len(i["symbols"]) for i in files.values()),
            "internal_imports": len(edges),
            "languages": dict(languages.most_common(8)),
        },
    }


def analyze(token, owner, repo, index) -> dict:
    """Build the architecture for the commit index points at. Slow: one
    archive download (usually cached) and one model call."""
    outline = codebase.load(token, owner, repo, index["commit_sha"])
    if not outline["files"]:
        raise ValueError("This repository has no source files Git Show can outline (Python, JavaScript/TypeScript, Java, Go).")
    edges, external = import_graph(outline)
    modules, module_edges = module_graph(outline, edges)
    readme = github_tools.get_readme(token, owner, repo)
    context = _context(
        index["repository"], index["branch"], readme.get("content") if readme.get("found") else "",
        outline, modules, module_edges, external,
    )
    raw = llm.complete_json(
        [{"role": "system", "content": ARCH_PROMPT}, {"role": "user", "content": context}],
        schema=ARCH_SCHEMA,
        validate=_validate,
    )
    result = _build_architecture(raw, outline, edges, modules, module_edges, external)
    result.update(
        {
            "version": ARCHITECTURE_VERSION,
            "repository": index["repository"],
            "branch": index["branch"],
            "commit_sha": index["commit_sha"],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "model": llm.MODEL,
        }
    )
    return result


# --- jobs and the API's view of them -------------------------------------------


def _run_job(token, owner, repo, index):
    try:
        result = analyze(token, owner, repo, index)
        store.finish_architecture_job(index["repository_id"], result)
    except (ValueError, llm.LLMError) as exc:
        store.fail_architecture_job(index["repository_id"], str(exc))
    except Exception as exc:
        log.exception("Architecture analysis of %s failed", index["repository"])
        store.fail_architecture_job(index["repository_id"], f"The analysis failed: {exc.__class__.__name__}.")


def _payload(index, row):
    if not row:
        return {"status": "none", "architecture": None, "current_commit": index["commit_sha"], "stale": False}
    architecture = row["architecture"]
    return {
        "status": row["status"],
        "architecture": architecture,
        "error": row["error"] if row["status"] == "failed" else None,
        "current_commit": index["commit_sha"],
        "stale": bool(architecture) and architecture.get("commit_sha") != index["commit_sha"],
    }


def status(token, owner, repo) -> dict:
    """The stored analysis, if any. Never starts one. The index lookup also
    proves the user can still see this repository."""
    index = repo_index.ensure_index(token, owner, repo)
    return _payload(index, store.get_architecture(index["repository_id"]))


def start(token, owner, repo, force=False) -> dict:
    """Start an analysis unless one is running or (without force) one is
    already stored — clicking again never repeats the work."""
    index = repo_index.ensure_index(token, owner, repo)
    owner_name, _, repo_name = index["repository"].partition("/")
    with _starting:
        row = store.get_architecture(index["repository_id"])
        already = row and (row["status"] == "running" or (row["status"] == "ready" and not force))
        if not already and store.claim_architecture_job(index["repository_id"], index["branch"], index["commit_sha"]):
            threading.Thread(
                target=_run_job, args=(token, owner_name, repo_name, index), name="architecture", daemon=True
            ).start()
            row = store.get_architecture(index["repository_id"])
    return _payload(index, row)
