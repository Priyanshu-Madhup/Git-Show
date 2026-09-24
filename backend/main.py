"""Git Show API: GitHub sign-in, chat history, and streaming endpoints that
hand every request to the orchestrator (see orchestrator.py)."""

import os
import secrets
import uuid
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

load_dotenv()

from fastapi import Cookie, FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse  # noqa: E402
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

NDJSON = "application/x-ndjson"

app = FastAPI(title="Git Show API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str
    chat_id: uuid.UUID
    repo: str | None = None


class ActionRequest(BaseModel):
    id: str


@app.get("/api/health")
def health():
    return {"status": "ok"}


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

    response = RedirectResponse(FRONTEND_URL)
    response.delete_cookie("oauth_state")
    response.set_cookie(
        "session_id",
        session_id,
        httponly=True,
        samesite="lax",
        max_age=int(db.SESSION_TTL.total_seconds()),
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
    session = get_session(session_id)
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
