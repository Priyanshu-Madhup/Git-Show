import json
import os
import re
import secrets
import threading
import time
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv
from fastapi import Cookie, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from openai import OpenAI
from pydantic import BaseModel

import github_tools

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# "-latest" aliases always resolve to Google's current stable model for that
# tier, so this keeps tracking their cheapest Flash tier without needing a
# code change whenever a new point release ships.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest")
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET")
GITHUB_CALLBACK_URL = os.getenv(
    "GITHUB_CALLBACK_URL", "http://localhost:5173/api/auth/github/callback"
)
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")

# Single-process, in-memory stores. Fine for a local dev server;
# swap for a real store (Redis, DB) before running more than one worker.
SESSIONS: dict[str, dict] = {}
PENDING_ACTIONS: dict[str, dict] = {}

# Rolling per-chat conversation summary, keyed by a client-generated chat_id.
# Replaces sending the full message history to the model on every turn.
# Lost on restart and on frontend reload (no chat_id persistence yet) — swap
# for a real store once chats are persisted (e.g. MongoDB).
CHAT_SUMMARIES: dict[str, str] = {}
SUMMARY_CHAR_BUDGET = 16000  # ~4k tokens at ~4 chars/token; hard safety cap

SUMMARIZER_SYSTEM_PROMPT = (
    "You maintain a running summary of an ongoing conversation between a "
    "user and Git Show, a GitHub assistant. You'll be given the existing "
    "summary (it may be empty, for a new conversation) plus the latest user "
    "message and assistant reply. Rewrite the summary to fold in this new "
    "exchange: keep facts, decisions, and specifics that matter for later "
    "turns (repos, branches, files, functions, PR/issue numbers, what was "
    "asked and answered); drop small talk and anything superseded by newer "
    "information. Keep the summary under about 3500 tokens (roughly 14000 "
    "characters) — if you're near that budget, compress or drop the least "
    "relevant older details rather than just appending to it. Reply with "
    "ONLY the updated summary text: no preamble, no headings, no code fences."
)

MAX_TOOL_ROUNDS = 10

# Repo file trees are pushed into context up front (see _repo_tree_context)
# instead of making the model spend a tool round discovering them, so this
# only needs to cover genuine multi-hop lookups (outline -> function source,
# etc.), not "what files exist here" exploration.
REPO_TREE_CACHE: dict[str, tuple[float, dict]] = {}
REPO_TREE_CACHE_TTL_SECONDS = 600

SYSTEM_PROMPT = (
    "You are Git Show, an assistant that helps developers explore a "
    "repository's history, commits, diffs, issues, and pull requests, and "
    "can also make changes (branches, files, PRs, issues) when asked. "
    "Answer clearly and concisely. Format responses with markdown (headings, "
    "bold, bullet or numbered lists, inline code) where it aids readability. "
    "Never use emojis.\n\n"
    "You have tools to read and modify GitHub repositories, users, and "
    "organizations. Rules:\n"
    "- Before calling any tool, check the 'Summary of the conversation so "
    "far' message (if present) for whether it already contains what you "
    "need — a repo's structure, a file's contents or outline, a function's "
    "behavior, an issue/PR's details, etc. from earlier in this chat. If "
    "it's already there, answer from it directly instead of re-fetching. "
    "Only call a tool for information that's missing from the summary or "
    "that the user is now asking about fresh (e.g. asking to re-check "
    "something that may have changed).\n"
    "- When the user refers to themselves ('my profile', 'my repos', "
    "'mine', 'I'), call get_me or list_user_repos with no username instead "
    "of asking who they are — you already know from their session.\n"
    "- If a 'Project structure' listing for the selected repo appears below, "
    "that IS the output of get_repo_tree for its default branch — it's "
    "already been fetched for you. Read it to find the file(s) you need "
    "instead of calling get_repo_tree again. Only call get_repo_tree "
    "yourself if that listing is absent, says the tree is unavailable, or "
    "you need a non-default branch.\n"
    "- When asked to describe, summarize, or explain a project's code in "
    "detail, work in cheap-to-expensive stages and stop as soon as you have "
    "enough to answer: (1) Check the 'Project structure' listing and "
    "get_readme first. (2) For the specific source files that look relevant "
    "by name/extension (using the listing you already have), call "
    "get_file_outline to see their function/class names cheaply — never "
    "call get_file_contents on a whole file just to skim it. (3) Only call "
    "get_function_source (or get_file_contents as a last resort) once you "
    "know exactly which single function or file answers the question, e.g. "
    "a user follow-up naming it.\n"
    "- If the user names or clearly implies a specific file (e.g. 'the main "
    "file of backend' means backend/main.py), call get_file_outline (or "
    "get_file_contents) on that exact path directly — check the 'Project "
    "structure' listing first if you're unsure of the exact path. Don't "
    "call get_repo_tree, list_branches, search_code, or search_repositories "
    "'just in case' — only fall back to those if the direct call actually "
    "fails.\n"
    "- search_code and search_repositories query all of GitHub, not the "
    "selected repo specifically. Never use them to locate a file that "
    "should already be visible in the 'Project structure' listing or "
    "reachable via get_repo_tree/get_file_outline on the selected repo — "
    "they're for finding things outside it (other repos, or code you "
    "don't know the location of anywhere).\n"
    "- web_search leaves GitHub entirely and queries the open web. It's a "
    "last resort: only call it when the user asks something no GitHub tool "
    "could ever answer (e.g. what a third-party error message means, how a "
    "library/framework works, general background on a technology) — never "
    "as a way to avoid or shortcut the GitHub tools above for anything "
    "about the selected repo, its users, issues, or PRs.\n"
    "- Never call the same tool with the same arguments more than once in a "
    "conversation. You already have the result from the first call; reuse "
    "it instead of re-fetching.\n"
    "- If you're missing information needed to call a tool correctly "
    "(which repo, which branch, a PR number, file content, a commit "
    "message, etc.) and it isn't obvious from context, ask the user a "
    "short clarifying question instead of guessing.\n"
    "- Never call a write tool (anything that creates, updates, deletes, "
    "merges, stars, or forks) with guessed or incomplete arguments. This "
    "applies most of all to any field that's a name/identifier the user "
    "has to choose and that has no sensible default — a new branch's name, "
    "a new file's path, a commit message, a release tag/name, an issue or "
    "PR title, a repo name to create/fork as. If the user's message and "
    "the conversation summary don't state one explicitly, you MUST ask for "
    "it — do not invent a plausible-sounding placeholder (e.g. "
    "'feature/new-branch', 'update-file', 'my-new-repo') and present it as "
    "a proposed action. This is different from fields like from_branch or "
    "a repo owner, which usually do have an obvious default (the repo's "
    "current default branch, the signed-in user) — only ask about those if "
    "context makes the default genuinely unclear.\n"
    "- After a tool call returns, use its actual result to answer; don't "
    "invent data a tool didn't return.\n"
    "- You already have full access to the user's GitHub data through your "
    "tools. Never tell the user to generate a personal access token, run "
    "curl or other manual API calls themselves, or otherwise fetch data "
    "outside of your tools — that's your job. If a list tool's result "
    "looks like it might be truncated (e.g. hit a per_page limit), say so "
    "briefly, or call the tool again with a larger per_page, instead of "
    "instructing the user to query GitHub directly.\n"
    "- Present results plainly and don't add unsolicited extra sections "
    "(e.g. 'how to see more', alternate ways to get the data) unless asked.\n"
    "- If a tool result has \"truncated\": true, only list the items actually "
    "present (\"shown\") and tell the user how many of the \"total\" you're "
    "showing. Never continue a list past what a tool returned by guessing or "
    "extrapolating a pattern (e.g. sequential names, dates, or numbers) — "
    "that data is fabricated even if it looks plausible."
)

EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF"
    "\U00002B00-\U00002BFF"
    "\U0000FE0F"
    "]+",
    flags=re.UNICODE,
)


def strip_emoji(text: str) -> str:
    return EMOJI_PATTERN.sub("", text)

app = FastAPI(title="Git Show API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = OpenAI(api_key=GEMINI_API_KEY, base_url=GEMINI_BASE_URL) if GEMINI_API_KEY else None


class ChatRequest(BaseModel):
    message: str
    chat_id: str
    repo: str | None = None


class PendingAction(BaseModel):
    id: str
    tool: str
    arguments: dict


class ChatResponse(BaseModel):
    reply: str
    pending_action: PendingAction | None = None


class ActionRequest(BaseModel):
    id: str


@app.get("/api/health")
def health():
    return {"status": "ok"}


def get_session(session_id: str | None) -> dict | None:
    if not session_id:
        return None
    return SESSIONS.get(session_id)


@app.get("/api/auth/github/login")
def github_login():
    if not GITHUB_CLIENT_ID:
        raise HTTPException(status_code=500, detail="GITHUB_CLIENT_ID is not configured.")

    state = secrets.token_urlsafe(24)
    params = {
        "client_id": GITHUB_CLIENT_ID,
        "redirect_uri": GITHUB_CALLBACK_URL,
        "state": state,
        # GITHUB_CLIENT_ID here is a GitHub App's client ID (its "Iv23..."
        # prefix, and the granular per-resource consent screen GitHub shows
        # for it, both confirm this) — not a classic OAuth App. For a
        # GitHub App, this "scope" param is a no-op; repo access is
        # entirely governed by the permissions configured on the App itself
        # (github.com -> app settings -> Permissions & events), not by
        # anything sent here. Kept for correctness if this ever points at a
        # real classic OAuth App instead, where it would matter.
        "scope": "repo",
    }
    response = RedirectResponse(f"https://github.com/login/oauth/authorize?{urlencode(params)}")
    response.set_cookie("oauth_state", state, httponly=True, samesite="lax", max_age=600)
    return response


@app.get("/api/auth/github/callback")
def github_callback(code: str, state: str, oauth_state: str | None = Cookie(default=None)):
    if not state or state != oauth_state:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state.")

    token_res = requests.post(
        "https://github.com/login/oauth/access_token",
        headers={"Accept": "application/json"},
        data={
            "client_id": GITHUB_CLIENT_ID,
            "client_secret": GITHUB_CLIENT_SECRET,
            "code": code,
            "redirect_uri": GITHUB_CALLBACK_URL,
        },
        timeout=10,
    )
    token_data = token_res.json()
    access_token = token_data.get("access_token")
    if not access_token:
        raise HTTPException(
            status_code=400,
            detail=f"GitHub token exchange failed: {token_data.get('error_description', token_data)}",
        )

    user_res = requests.get(
        "https://api.github.com/user",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=10,
    )
    user = user_res.json()

    session_id = secrets.token_urlsafe(32)
    SESSIONS[session_id] = {
        "access_token": access_token,
        "user": {
            "login": user.get("login"),
            "name": user.get("name"),
            "avatar_url": user.get("avatar_url"),
        },
    }

    response = RedirectResponse(FRONTEND_URL)
    response.delete_cookie("oauth_state")
    response.set_cookie(
        "session_id",
        session_id,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 7,
    )
    return response


@app.get("/api/auth/me")
def auth_me(session_id: str | None = Cookie(default=None)):
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=401, detail="Not signed in.")
    return session["user"]


@app.post("/api/auth/logout")
def auth_logout(session_id: str | None = Cookie(default=None)):
    if session_id:
        SESSIONS.pop(session_id, None)
    response = JSONResponse({"ok": True})
    response.delete_cookie("session_id")
    return response


@app.get("/api/github/repos")
def list_repos(session_id: str | None = Cookie(default=None)):
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=401, detail="Not signed in.")

    res = requests.get(
        "https://api.github.com/user/repos",
        headers={
            "Authorization": f"Bearer {session['access_token']}",
            "Accept": "application/vnd.github+json",
        },
        params={"per_page": 50, "sort": "updated"},
        timeout=10,
    )
    if res.status_code != 200:
        raise HTTPException(status_code=502, detail="Failed to fetch repositories from GitHub.")

    repos = [
        {
            "name": r["name"],
            "full_name": r["full_name"],
            "private": r["private"],
            "html_url": r["html_url"],
        }
        for r in res.json()
    ]
    return {"repos": repos}


def _tool_result_content(result, limit=8000):
    """Serialize a tool result for the model, truncating at item boundaries
    so the JSON stays valid. A blind string slice can cut a large list mid-
    object, handing the model malformed data that it then tends to "complete"
    by inventing plausible-looking rows — this keeps truncation explicit
    instead."""
    text = json.dumps(result)
    if len(text) <= limit:
        return text

    if isinstance(result, list):
        lo, hi = 0, len(result)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            candidate = json.dumps(
                {"items": result[:mid], "truncated": True, "shown": mid, "total": len(result)}
            )
            if len(candidate) <= limit:
                lo = mid
            else:
                hi = mid - 1
        return json.dumps(
            {"items": result[:lo], "truncated": True, "shown": lo, "total": len(result)}
        )

    return json.dumps({"truncated": True, "note": "Result too large; showing partial data.", "partial": text[:limit]})


def _run_completion(messages, tools):
    kwargs = {"model": GEMINI_MODEL, "messages": messages, "timeout": 30}
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    try:
        return client.chat.completions.create(**kwargs)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Gemini request failed: {exc}") from exc


def _summarize_turn(existing_summary, user_message, assistant_reply):
    prompt = (
        "EXISTING SUMMARY:\n"
        f"{existing_summary or '(empty — this is the start of the conversation)'}\n\n"
        f"NEW USER MESSAGE:\n{user_message}\n\n"
        f"NEW ASSISTANT REPLY:\n{assistant_reply}"
    )
    messages = [
        {"role": "system", "content": SUMMARIZER_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    completion = _run_completion(messages, tools=None)
    summary = (completion.choices[0].message.content or "").strip()
    return summary[:SUMMARY_CHAR_BUDGET]


def _describe_pending_action(tool_name, arguments):
    """Turn a raw tool name + arguments into a short, plain-language
    description for the user to review before confirming. Function-calling
    responses normally come back with no assistant message text at all, so
    without this the user would just see a dump of raw JSON args — fine for
    small ones, unreadable for anything with a large text field like file
    content. Returns None on failure so the caller can fall back to the raw
    JSON rendering rather than breaking the confirmation flow."""
    args_preview = json.dumps(arguments)
    if len(args_preview) > 4000:
        args_preview = args_preview[:4000] + "... (truncated for this summary)"
    prompt = (
        "A user is about to be asked to confirm this proposed GitHub action. "
        "Write 1-2 short plain-language sentences describing what it will do, "
        "for them to review before confirming. If an argument holds a large "
        "text blob (e.g. file content), describe what it contains/changes "
        "instead of reproducing it. Don't invent details beyond what the "
        "arguments show. End with a short prompt to confirm or cancel.\n\n"
        f"Tool: {tool_name}\n"
        f"Arguments: {args_preview}"
    )
    try:
        completion = _run_completion(
            [
                {
                    "role": "system",
                    "content": (
                        "You write short, plain-language confirmations of a "
                        "pending action for a human review step. Never use "
                        "emojis or markdown headings."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            tools=None,
        )
        text = strip_emoji((completion.choices[0].message.content or "").strip())
        return text or None
    except HTTPException:
        return None


def _update_summary_async(chat_id, user_message, assistant_reply):
    if not chat_id or not assistant_reply:
        return

    def worker():
        try:
            existing = CHAT_SUMMARIES.get(chat_id, "")
            CHAT_SUMMARIES[chat_id] = _summarize_turn(existing, user_message, assistant_reply)
        except Exception:
            pass  # best-effort; keep the previous summary on failure

    threading.Thread(target=worker, daemon=True).start()


def _step_label(tool_name, arguments):
    path = arguments.get("path")
    builders = {
        "get_readme": lambda: "Reading the README",
        "get_repository": lambda: "Looking up repository details",
        "get_repo_tree": lambda: "Inspecting the project structure",
        "get_file_outline": lambda: f"Scanning {path or 'a file'}",
        "get_function_source": lambda: f"Looking at {arguments.get('function_name', 'a function')} in {path or 'a file'}",
        "get_file_contents": lambda: f"Reading {path or 'a file'}",
        "list_commits": lambda: "Listing recent commits",
        "get_commit": lambda: "Looking at a commit",
        "list_issues": lambda: "Listing issues",
        "list_pull_requests": lambda: "Listing pull requests",
        "get_pull_request": lambda: "Looking at a pull request",
        "get_pull_request_diff": lambda: "Reading a pull request diff",
        "search_code": lambda: "Searching code",
        "search_repositories": lambda: "Searching repositories",
        "web_search": lambda: f"Searching the web for \"{arguments.get('query', '')}\"",
        "list_user_repos": lambda: "Listing repositories",
    }
    build = builders.get(tool_name)
    if build:
        return build()
    return f"Calling {tool_name.replace('_', ' ')}"


def _repo_tree_context(access_token, repo):
    """Fetch (or reuse a cached) file tree for the selected repo and format
    it as a system message, so the model starts a turn already knowing the
    project layout instead of spending its first tool round on
    get_repo_tree. Returns None if the repo isn't well-formed or the fetch
    fails — the model still has get_repo_tree itself as a fallback."""
    if not repo or "/" not in repo:
        return None
    owner, _, name = repo.partition("/")

    cached = REPO_TREE_CACHE.get(repo)
    if cached and time.time() - cached[0] < REPO_TREE_CACHE_TTL_SECONDS:
        result = cached[1]
    else:
        try:
            result = github_tools.get_repo_tree(access_token, owner, name)
        except Exception:
            return None
        REPO_TREE_CACHE[repo] = (time.time(), result)

    lines = [f"{f['path']}{'/' if f['type'] == 'dir' else ''}" for f in result.get("files", [])]
    body = "\n".join(lines)
    if result.get("truncated"):
        body += f"\n... ({result.get('total_entries')} entries total, truncated)"
    return (
        f"Project structure of {repo} (default branch, from get_repo_tree — "
        f"already fetched, don't call it again for this branch):\n{body}"
    )


def _chat_stream(payload: ChatRequest, session_id: str | None):
    def emit(event):
        return json.dumps(event) + "\n"

    session = get_session(session_id)
    access_token = session["access_token"] if session else None

    system_prompt = SYSTEM_PROMPT
    if payload.repo:
        system_prompt += (
            f"\n\nThe user's currently selected repository is {payload.repo}. "
            "Use it as the default owner/repo for tool calls unless they name "
            "a different repository."
        )
    if not access_token:
        system_prompt += (
            "\n\nThe user is not signed in to GitHub right now, so no repository "
            "tools are available. If they ask about a real repository, tell them "
            "to sign in with the button in the top right first."
        )

    summary = CHAT_SUMMARIES.get(payload.chat_id, "")

    messages = [{"role": "system", "content": system_prompt}]
    if access_token and payload.repo:
        tree_context = _repo_tree_context(access_token, payload.repo)
        if tree_context:
            messages.append({"role": "system", "content": tree_context})
    if summary:
        messages.append({"role": "system", "content": f"Summary of the conversation so far:\n{summary}"})
    messages.append({"role": "user", "content": payload.message})

    tools = github_tools.TOOL_SCHEMAS if access_token else None
    tool_cache: dict[tuple, dict] = {}

    for _ in range(MAX_TOOL_ROUNDS):
        try:
            completion = _run_completion(messages, tools)
        except HTTPException as exc:
            yield emit({"type": "final", "reply": f"Something went wrong: {exc.detail}"})
            return
        msg = completion.choices[0].message

        if not msg.tool_calls:
            reply = strip_emoji(msg.content or "")
            yield emit({"type": "final", "reply": reply})
            _update_summary_async(payload.chat_id, payload.message, reply)
            return

        messages.append(
            {
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
            }
        )

        write_call = next(
            (tc for tc in msg.tool_calls if tc.function.name in github_tools.WRITE_TOOLS), None
        )
        if write_call:
            arguments = json.loads(write_call.function.arguments or "{}")
            confirm_id = secrets.token_urlsafe(16)
            PENDING_ACTIONS[confirm_id] = {
                "tool": write_call.function.name,
                "arguments": arguments,
                "session_id": session_id,
            }
            reply = strip_emoji(
                msg.content
                or _describe_pending_action(write_call.function.name, arguments)
                or (
                    f"I'd like to run **{write_call.function.name}** with `{json.dumps(arguments)}`. "
                    "Confirm to proceed."
                )
            )
            yield emit(
                {
                    "type": "final",
                    "reply": reply,
                    "pending_action": {"id": confirm_id, "tool": write_call.function.name, "arguments": arguments},
                }
            )
            _update_summary_async(payload.chat_id, payload.message, reply)
            return

        for tc in msg.tool_calls:
            arguments = json.loads(tc.function.arguments or "{}")
            cache_key = (tc.function.name, json.dumps(arguments, sort_keys=True))
            if cache_key in tool_cache:
                # Already fetched this exact call earlier in the turn — hand
                # the model the same result instantly instead of re-hitting
                # GitHub (and skip showing a duplicate step in the timeline).
                # The reminder below is aimed at the model, not the user: a
                # weaker model can re-request an identical call anyway, and
                # a plain repeated result doesn't visibly signal "this was a
                # mistake, stop doing it" the way this note does.
                content = (
                    "[Repeated call — you already made this exact call earlier "
                    "in this turn. Do not call it again; use the result below.]\n"
                    + _tool_result_content(tool_cache[cache_key])
                )
            else:
                yield emit(
                    {"type": "step", "tool": tc.function.name, "label": _step_label(tc.function.name, arguments)}
                )
                try:
                    result = github_tools.run_tool(tc.function.name, access_token, arguments)
                except Exception as exc:
                    result = {"error": str(exc)}
                tool_cache[cache_key] = result
                content = _tool_result_content(result)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})

    # Ran out of tool rounds. Rather than discard everything gathered so
    # far, force one last text-only completion (no tools) so the model
    # writes up whatever it already has instead of dead-ending.
    messages.append(
        {
            "role": "system",
            "content": (
                "You've used up your tool-call budget for this turn. Do not "
                "request any more tools. Answer the user now using only the "
                "information already gathered above; say plainly which part, "
                "if any, you couldn't get to."
            ),
        }
    )
    try:
        completion = _run_completion(messages, tools=None)
        reply = strip_emoji(completion.choices[0].message.content or "")
    except HTTPException:
        reply = ""
    if not reply:
        reply = "I wasn't able to finish that within the allotted number of steps."
    yield emit({"type": "final", "reply": reply})
    _update_summary_async(payload.chat_id, payload.message, reply)


@app.post("/api/chat")
def chat(payload: ChatRequest, session_id: str | None = Cookie(default=None)):
    if not GEMINI_API_KEY or GEMINI_API_KEY == "your_gemini_api_key_here":
        raise HTTPException(
            status_code=500,
            detail="GEMINI_API_KEY is not configured. Set it in backend/.env.",
        )

    return StreamingResponse(_chat_stream(payload, session_id), media_type="application/x-ndjson")


@app.post("/api/github/actions/execute", response_model=ChatResponse)
def execute_action(payload: ActionRequest, session_id: str | None = Cookie(default=None)):
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=401, detail="Not signed in.")

    action = PENDING_ACTIONS.pop(payload.id, None)
    if not action or action["session_id"] != session_id:
        raise HTTPException(status_code=404, detail="This action has expired or was already handled.")

    try:
        result = github_tools.run_tool(action["tool"], session["access_token"], action["arguments"])
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GitHub action failed: {exc}") from exc

    summary_messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"I just executed {action['tool']} with arguments {json.dumps(action['arguments'])}. "
                f"Result: {json.dumps(result)[:3000]}. Summarize what happened in one or two sentences."
            ),
        },
    ]
    completion = _run_completion(summary_messages, tools=None)
    reply = strip_emoji(completion.choices[0].message.content or "Done.")
    return ChatResponse(reply=reply)


@app.post("/api/github/actions/cancel")
def cancel_action(payload: ActionRequest, session_id: str | None = Cookie(default=None)):
    action = PENDING_ACTIONS.get(payload.id)
    if action and action["session_id"] == session_id:
        PENDING_ACTIONS.pop(payload.id, None)
    return {"ok": True}
