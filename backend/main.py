"""Git Show API: GitHub sign-in, chat history, and streaming endpoints that
hand every request to the orchestrator (see orchestrator.py)."""

import os
import secrets
import uuid
from pathlib import Path
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

load_dotenv()

from fastapi import Cookie, FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import db  # noqa: E402
import llm  # noqa: E402
import orchestrator  # noqa: E402
from db import runs as run_store  # noqa: E402

GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET")
GITHUB_CALLBACK_URL = os.getenv(
    "GITHUB_CALLBACK_URL", "http://localhost:5173/api/auth/github/callback"
)
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")
# "/" is the landing page; signing in or installing returns people to the chat.
APP_URL = FRONTEND_URL.rstrip("/") + "/app"

# Everything below is optional and defaults to the local dev setup (Vite on
# :5173 proxying /api to this server), so a local run needs none of it.

# The GitHub App's URL name (github.com/apps/<slug>). Enables the "Install
# Git Show on your repositories" link; without it the link is hidden.
GITHUB_APP_SLUG = os.getenv("GITHUB_APP_SLUG", "").strip()

# Origins allowed to call the API from a browser, comma-separated. Only
# matters if the frontend is served from a different origin than the API.
CORS_ORIGINS = [
    o.strip()
    for o in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
    if o.strip()
]

# Cookies must be Secure over HTTPS; that follows FRONTEND_URL unless set.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", str(FRONTEND_URL.startswith("https://"))).lower() in ("1", "true", "yes")

# Set SERVE_FRONTEND=1 to have this server also serve the built frontend
# (frontend/dist, from `npm run build`), so a deployment is one origin and
# one process. Off by default: locally, Vite serves the frontend.
SERVE_FRONTEND = os.getenv("SERVE_FRONTEND", "").lower() in ("1", "true", "yes")
FRONTEND_DIST = Path(os.getenv("FRONTEND_DIST", Path(__file__).resolve().parent.parent / "frontend" / "dist"))

NDJSON = "application/x-ndjson"

app = FastAPI(title="Git Show API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _set_cookie(response, name, value, max_age):
    response.set_cookie(name, value, httponly=True, samesite="lax", secure=COOKIE_SECURE, max_age=max_age)


def _begin_github_redirect(url, params):
    """Redirect to GitHub with a fresh anti-CSRF state, remembered in a
    short-lived cookie and checked when GitHub sends the user back."""
    state = secrets.token_urlsafe(24)
    response = RedirectResponse(f"{url}?{urlencode({**params, 'state': state})}")
    _set_cookie(response, "oauth_state", state, 600)
    return response


class ChatRequest(BaseModel):
    message: str
    chat_id: uuid.UUID
    repo: str | None = None


class ActionRequest(BaseModel):
    id: str


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/config")
def public_config():
    """Non-secret settings the frontend needs."""
    return {"install_url": "/api/auth/github/install" if GITHUB_APP_SLUG else None}


def _refresh_github_token(refresh_token: str) -> dict | None:
    """Trade a GitHub App refresh token for a new access token."""
    try:
        res = requests.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            timeout=10,
        )
        data = res.json()
    except (requests.RequestException, ValueError):
        return None
    return data if data.get("access_token") else None


def get_session(session_id: str | None) -> dict | None:
    """The signed-in session, with its GitHub token refreshed first if it
    has expired (or is about to). A session whose token can't be refreshed
    is removed, so the user is asked to sign in again."""
    session = db.get_session(session_id)
    if not session or not session.get("needs_refresh"):
        return session
    fresh = _refresh_github_token(session["refresh_token"]) if session.get("refresh_token") else None
    if not fresh:
        db.delete_session(session_id)
        return None
    db.update_session_tokens(session_id, fresh["access_token"], fresh.get("refresh_token"), fresh.get("expires_in"))
    return db.get_session(session_id)


@app.get("/api/auth/github/login")
def github_login():
    if not GITHUB_CLIENT_ID:
        raise HTTPException(status_code=500, detail="GITHUB_CLIENT_ID is not configured.")

    params = {
        "client_id": GITHUB_CLIENT_ID,
        "redirect_uri": GITHUB_CALLBACK_URL,
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
    return _begin_github_redirect("https://github.com/login/oauth/authorize", params)


@app.get("/api/auth/github/install")
def github_install():
    """Send the user to install the GitHub App on their account or repos.
    With "Request user authorization (OAuth) during installation" enabled in
    the App's settings, GitHub signs them in as part of the same flow and
    comes back to the callback below, carrying this state."""
    if not GITHUB_APP_SLUG:
        raise HTTPException(status_code=500, detail="GITHUB_APP_SLUG is not configured.")
    return _begin_github_redirect(f"https://github.com/apps/{GITHUB_APP_SLUG}/installations/new", {})


@app.get("/api/auth/github/callback")
def github_callback(
    code: str | None = None,
    state: str | None = None,
    installation_id: str | None = None,
    setup_action: str | None = None,
    oauth_state: str | None = Cookie(default=None),
):
    if not code:
        # Back from installing (or changing) the App without a sign-in code,
        # e.g. "Request user authorization during installation" is off:
        # sign in normally, which is instant once the App is authorized.
        return RedirectResponse("/api/auth/github/login" if setup_action else APP_URL)
    if not state or state != oauth_state:
        if setup_action:
            # Installed from github.com directly rather than through our
            # Install link, so there's no state to match; start a normal
            # (state-checked) sign-in instead of trusting this code.
            return RedirectResponse("/api/auth/github/login")
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
    if user_res.status_code != 200:
        raise HTTPException(status_code=502, detail="Failed to fetch your GitHub profile.")
    user = user_res.json()

    user_id = db.upsert_user(user["id"], user["login"], user.get("name"), user.get("avatar_url"))
    session_id = secrets.token_urlsafe(32)
    db.create_session(
        session_id,
        user_id,
        access_token,
        refresh_token=token_data.get("refresh_token"),
        expires_in=token_data.get("expires_in"),
    )

    response = RedirectResponse(APP_URL)
    response.delete_cookie("oauth_state")
    _set_cookie(response, "session_id", session_id, int(db.SESSION_TTL.total_seconds()))
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
        db.delete_session(session_id)
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

def _require_session(session_id: str | None) -> dict:
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=401, detail="Not signed in.")
    return session


def _require_llm():
    if not llm.is_configured():
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY is not configured. Set it in backend/.env.")


@app.post("/api/chat")
def chat(payload: ChatRequest, session_id: str | None = Cookie(default=None)):
    _require_llm()
    if not payload.message.strip():
        raise HTTPException(status_code=400, detail="Message is empty.")
    session = _require_session(session_id)
    return StreamingResponse(
        orchestrator.start(message=payload.message, chat_id=payload.chat_id, repo=payload.repo, session=session),
        media_type=NDJSON,
    )


@app.post("/api/github/actions/execute")
def execute_action(payload: ActionRequest, session_id: str | None = Cookie(default=None)):
    """Confirm a proposed write: the orchestrator applies it, verifies it,
    and carries on with the plan — streamed like /api/chat."""
    _require_llm()
    session = _require_session(session_id)
    return StreamingResponse(
        orchestrator.resume_action(action_id=payload.id, approved=True, session=session), media_type=NDJSON
    )


@app.post("/api/github/actions/cancel")
def cancel_action(payload: ActionRequest, session_id: str | None = Cookie(default=None)):
    """Decline a proposed write; the Planning agent decides what to do next."""
    _require_llm()
    session = _require_session(session_id)
    return StreamingResponse(
        orchestrator.resume_action(action_id=payload.id, approved=False, session=session), media_type=NDJSON
    )


@app.post("/api/runs/{run_id}/continue")
def continue_run(run_id: uuid.UUID, session_id: str | None = Cookie(default=None)):
    """Resume a run that paused after its tool budget."""
    _require_llm()
    session = _require_session(session_id)
    return StreamingResponse(orchestrator.continue_run(run_id=run_id, session=session), media_type=NDJSON)


@app.get("/api/runs/{run_id}/trace")
def run_trace(run_id: uuid.UUID, session_id: str | None = Cookie(default=None)):
    """The full execution trace of a run — every plan version, agent step,
    and tool call — to answer "why did Git Show do that?"."""
    session = _require_session(session_id)
    trace = run_store.get_trace(run_id, session["user_id"])
    if not trace:
        raise HTTPException(status_code=404, detail="Run not found.")
    return trace


@app.get("/api/conversations")
def list_conversations(session_id: str | None = Cookie(default=None)):
    session = _require_session(session_id)
    return {"conversations": db.list_conversations(session["user_id"])}


@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: uuid.UUID, session_id: str | None = Cookie(default=None)):
    session = _require_session(session_id)
    conversation = db.get_conversation(conversation_id, session["user_id"])
    if not conversation:
        raise HTTPException(status_code=404, detail="Chat not found.")
    return conversation


@app.delete("/api/conversations/{conversation_id}")
def delete_conversation(conversation_id: uuid.UUID, session_id: str | None = Cookie(default=None)):
    session = _require_session(session_id)
    if not db.delete_conversation(conversation_id, session["user_id"]):
        raise HTTPException(status_code=404, detail="Chat not found.")
    return {"ok": True}


if SERVE_FRONTEND:
    # Registered last so every /api route above takes precedence. Unknown
    # paths get index.html, letting the single-page app handle them.
    if not (FRONTEND_DIST / "index.html").exists():
        raise RuntimeError(f"SERVE_FRONTEND is on but {FRONTEND_DIST} has no index.html — run `npm run build` first.")
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str):
        if path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found.")
        candidate = (FRONTEND_DIST / path).resolve()
        if path and candidate.is_file() and FRONTEND_DIST.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
