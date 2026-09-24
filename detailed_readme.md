# Git Show

Git Show is a chat assistant for exploring and acting on GitHub repositories in plain language. Sign in with GitHub, pick a repository, and ask questions about its history, issues, and pull requests — or ask the assistant to make changes (create branches, open PRs/issues, comment, merge, etc.), which it will always propose and let you confirm before executing.

It's a two-part app: a **FastAPI** backend that handles GitHub OAuth, session storage, and an LLM tool-calling loop, and a **React + Vite** frontend that renders a single-page chat UI.

## How it works

1. **Sign in with GitHub** — the user clicks the avatar icon in the top bar, which starts a standard OAuth Authorization Code flow against a GitHub OAuth App.
2. **Pick a repository** — once signed in, the frontend fetches the user's repos and shows a picker; the selected `owner/repo` is sent with every chat message as context.
3. **Ask a question** — the message, prior turns, and selected repo are POSTed to `/api/chat`. The backend builds a system prompt and calls the Gemini-hosted LLM (via Gemini's OpenAI-compatible endpoint) with a large set of GitHub tool schemas attached (function calling).
4. **Tool calling loop** — the backend runs up to `MAX_TOOL_ROUNDS` (10) rounds: if the model requests a **read** tool (list commits, get a diff, fetch an issue, etc.), the backend executes it immediately against the GitHub REST API and feeds the result back to the model. If the model requests a **write** tool (create a branch, open a PR, merge, delete a branch, etc.), the backend does *not* execute it — it stores the call as a "pending action" and returns it to the frontend for explicit human confirmation. Before the loop starts, if a repo is selected the backend pre-fetches (and caches for 10 minutes) that repo's file tree and injects it as context, so the model doesn't have to spend a tool round discovering the project layout on its own. If the round budget still runs out, the backend forces one last tools-disabled completion so the model writes up whatever it already found instead of dead-ending.
5. **Confirm or cancel** — the frontend renders a proposed-action card with the tool name and arguments. Confirming calls `/api/github/actions/execute`, which runs the actual GitHub write and asks the model for a one-line summary of the result. Cancelling just discards the pending action.
6. **Streamed reply** — replies aren't streamed from the LLM itself (the Gemini call is synchronous); instead, the frontend does a client-side typewriter animation over the returned text for a chat-like feel.

## Architecture

```
frontend (React/Vite, :5173)  --/api proxy-->  backend (FastAPI, :8000)  -->  GitHub REST API
                                                        |
                                                        --> Gemini (LLM tool calling)
```

- The Vite dev server proxies any `/api/*` request to `http://localhost:8000`, so the frontend only ever talks to its own origin.
- Sessions and pending write-actions are kept in-memory (plain Python dicts) — fine for a single local dev process, not for multiple workers or production (see [Known limitations](#known-limitations)).

## Tech stack

**Backend**
- [FastAPI](https://fastapi.tiangolo.com/) — HTTP API and routing
- [Uvicorn](https://www.uvicorn.org/) — ASGI server
- [Gemini API](https://ai.google.dev/) — LLM inference, called via the `openai` Python SDK against Gemini's OpenAI-compatible endpoint (default model `gemini-flash-lite-latest`)
- `requests` — GitHub REST API calls
- `python-dotenv` — loads `.env`
- Pydantic — request/response models

**Frontend**
- React 19 + Vite 8
- Plain CSS (`App.css`, `index.css`) — no CSS framework
- `@fontsource` (Fraunces + Inter) for typography
- `oxlint` for linting
- Playwright — used only for local, manual screenshot scripts (not a test suite)

## Project structure

```
git_show/
├── backend/
│   ├── main.py                # FastAPI app: OAuth, sessions, /api/chat, action confirm/cancel
│   ├── github_tools.py        # GitHub REST wrappers + tool schemas exposed to the LLM
│   ├── requirements.txt
│   ├── .env.example           # template for required environment variables
│   └── .env                   # local secrets (gitignored)
└── frontend/
    ├── src/
    │   ├── App.jsx             # entire chat UI: auth, repo picker, message list, action cards
    │   ├── App.css / index.css # styling
    │   ├── main.jsx             # React entry point
    │   └── assets/logo.png
    ├── index.html
    ├── vite.config.js          # dev server + /api proxy to :8000
    ├── package.json
    ├── screenshot.mjs / screenshot2.mjs  # ad-hoc Playwright scripts for manual UI screenshots
    └── .oxlintrc.json
```

## Setup

### Prerequisites
- Python 3.12+
- Node.js 18+ (for Vite 8 / React 19)
- A [Gemini API key](https://aistudio.google.com/apikey)
- A GitHub app registered as either:
  - a classic [GitHub OAuth App](https://github.com/settings/developers) — `GITHUB_CLIENT_ID`/`SECRET` are the OAuth App's; grant access at auth time via the `scope` param `main.py` sends to the authorize URL, or
  - a [GitHub App](https://github.com/settings/apps) using its user-to-server OAuth flow — same `GITHUB_CLIENT_ID`/`SECRET` fields, but access is governed entirely by the **Permissions & events** configured on the App itself (the `scope` param is ignored). This is what `GITHUB_APP_ID` in `.env.example` is for, and what a client ID with an `Iv23...`-style prefix indicates. If a write tool 403s despite a signed-in user having full rights on GitHub, check the App's repository permissions (e.g. **Contents** must be Read & write for branch/file/release operations) before suspecting the code.

  Either way, register it with:
  - **Homepage URL**: `http://localhost:5173`
  - **Authorization callback URL**: `http://localhost:5173/api/auth/github/callback`

### Backend

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
copy .env.example .env        # then fill in real values
uvicorn main:app --reload --port 8000
```

Environment variables (`backend/.env`):

| Variable | Purpose |
|---|---|
| `GEMINI_API_KEY` | API key for Gemini inference |
| `GEMINI_MODEL` | Model id (default `gemini-flash-lite-latest`, Gemini's cheapest Flash tier) |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | GitHub OAuth App credentials |
| `GITHUB_CALLBACK_URL` | OAuth redirect target (must match the app's registered callback) |
| `FRONTEND_URL` | Where to redirect the browser after OAuth completes |

### Frontend

```bash
cd frontend
npm install
npm run dev      # starts Vite on :5173, proxying /api to :8000
```

Then open `http://localhost:5173`. Both servers must be running for the app to work.

Other frontend scripts:
- `npm run build` — production build
- `npm run preview` — preview the production build
- `npm run lint` — run oxlint

## Deploying

Local development needs none of this — every setting below is optional and defaults to the local setup.

### 1. GitHub App settings (github.com → Settings → Developer settings → GitHub Apps → your app)

- **Callback URL**: add `https://<your-domain>/api/auth/github/callback` (keep the localhost one too; GitHub Apps accept several).
- **Request user authorization (OAuth) during installation**: on — installing then signs the user in, in one step.
- **Where can this GitHub App be installed?** → *Any account*, so other people can install it.
- **Permissions**: whatever Git Show should be able to do (e.g. Contents, Pull requests, Issues: read & write; Metadata: read).

Signing in is not the same as installing: a user's token only reaches repositories where the app is **installed**. When a signed-in user has no accessible repositories, Git Show shows an **Install Git Show on your repositories** button (and a "Manage repository access" link otherwise), which goes to `/api/auth/github/install`. Both need `GITHUB_APP_SLUG`.

### 2. One origin, one process (recommended)

Build the frontend and let FastAPI serve it, so the browser, API, and session cookie share one origin:

```bash
cd frontend && npm install && npm run build      # -> frontend/dist
cd ../backend && pip install -r requirements.txt
SERVE_FRONTEND=1 uvicorn main:app --host 0.0.0.0 --port $PORT
```

Environment on the host (in addition to the ones in `.env.example`):

| Variable | Value |
|---|---|
| `FRONTEND_URL` | `https://<your-domain>` (https also turns on Secure cookies) |
| `GITHUB_CALLBACK_URL` | `https://<your-domain>/api/auth/github/callback` |
| `GITHUB_APP_SLUG` | the app's URL name, from `github.com/apps/<slug>` |
| `SERVE_FRONTEND` | `1` |

If the frontend is hosted separately instead, point it at the API through a rewrite of `/api/*` (keeping one origin), or set `CORS_ORIGINS` to the frontend's origin — though cross-site cookies are more fragile than one origin.

### 3. Where to host

Use a long-running server (Railway, Render, Fly.io, a VM): agent runs stream for up to a minute and database writes happen on a background thread, which serverless functions would time out or kill. Put it in the same region as the Supabase project — every database round trip crosses that distance. Run `python db/migrate.py` once against the production database before first start.

## API reference (backend)

| Method & path | Purpose |
|---|---|
| `GET /api/health` | Liveness check |
| `GET /api/auth/github/login` | Redirects to GitHub's OAuth authorize page |
| `GET /api/auth/github/callback` | OAuth callback; exchanges code for a token, creates a session cookie |
| `GET /api/auth/me` | Returns the signed-in user (login/name/avatar) or 401 |
| `POST /api/auth/logout` | Clears the session |
| `GET /api/github/repos` | Lists the signed-in user's repos (for the repo picker) |
| `POST /api/chat` | Main chat endpoint; runs the tool-calling loop, may return a `pending_action` |
| `POST /api/github/actions/execute` | Executes a previously proposed write action by id |
| `POST /api/github/actions/cancel` | Discards a previously proposed write action |

Auth is cookie-based (`session_id`, httponly), with a short-lived `oauth_state` cookie used to prevent CSRF during the OAuth handshake.

## GitHub tools exposed to the LLM

Defined in `backend/github_tools.py`, split into two sets:

**Read tools** (executed automatically, no confirmation): `list_commits`, `get_commit`, `get_file_contents`, `list_branches`, `list_issues`, `list_pull_requests`, `get_pull_request`, `get_pull_request_diff`, `get_pull_request_files`, `list_pull_request_reviews`, `list_pull_request_commits`, `search_code`, `search_repositories`, `search_issues`, `search_users`, `list_releases`, `get_latest_release`, `list_tags`, `list_contributors`, `list_forks`, `list_stargazers`, `list_workflows`, `list_workflow_runs`, `compare_commits`, `get_me`, `get_user`, `list_user_repos`, `get_repository`, `list_organizations`, `get_issue`, `list_issue_comments`.

**Write tools** (always require user confirmation via the action card): `create_branch`, `create_or_update_file`, `delete_file`, `create_pull_request`, `merge_pull_request`, `create_issue`, `add_issue_comment`, `fork_repository`, `create_repository`, `star_repository`, `unstar_repository`, `update_issue`, `update_pull_request`, `submit_pull_request_review`, `request_pull_request_reviewers`, `create_release`, `delete_branch`, `add_collaborator`.

All tools call the GitHub REST API directly with the signed-in user's OAuth token — there is no GitHub App installation flow currently wired up in `main.py`, despite `.env.example` referencing `GITHUB_APP_ID`.

## Frontend behavior notes

- The landing view shows a centered search field with example query chips; on first message it "docks" the search bar to the bottom using a manual FLIP animation (`useLayoutEffect` computing/transitioning `transform`), then reveals the scrolling message list.
- Assistant replies are revealed with a client-side typewriter effect (character-by-character `setInterval`), not real token streaming from the backend.
- Chat responses are rendered as a small hand-rolled Markdown subset: bold/italic/inline-code, bullet/numbered lists, tables, single-line headings, and paragraphs — via `formatMessage`/`formatInline` in `App.jsx`, with HTML-escaping applied before any markup injection.
- Emojis are stripped server-side (`strip_emoji` in `main.py`) before replies are returned.
- Requests to `/api/chat` have a 170s client-side abort timeout, with a "still working" hint shown after 4s.

## Security notes

- `backend/.gitignore` already excludes `.env` and `*.pem`, so secrets and the observed `gitshowmelegend...private-key.pem` file are not tracked by git.
- Session and pending-action state is kept in an in-memory dict (`SESSIONS`, `PENDING_ACTIONS`) — restarting the backend logs everyone out and drops any unconfirmed actions.
- Write actions are never auto-executed by the model; they always round-trip through the frontend for an explicit user confirmation before hitting the GitHub API.

## Known limitations

- In-memory session/action storage means the backend cannot run with multiple workers/processes without a shared store (e.g. Redis).
- No automated test suite; `screenshot.mjs`/`screenshot2.mjs` are manual Playwright scripts for grabbing UI screenshots during development, not CI tests.
- CORS and cookie settings (`samesite="lax"`, no `secure` flag) are configured for local HTTP development, not production HTTPS deployment.
