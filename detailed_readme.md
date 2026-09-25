# Git Show

Git Show is a chat assistant for GitHub. Sign in with GitHub, pick a repository, and ask about its code, history, issues, and pull requests in plain language — or ask for a change (a branch, a file edit, a pull request, an issue, a merge). Every change is shown to you first, with the exact diff where it applies, and only runs after you confirm it; Git Show then re-reads GitHub to make sure it landed.

It has two parts:
- a **FastAPI** backend: GitHub sign-in, a team of AI agents, and Postgres (Supabase) for sessions, chat history, memory, and an execution trace;
- a **React + Vite** frontend: a landing page at `/` and the chat app at `/app`.

## How it works

```
             user message
                  │
            ┌─────▼──────┐   works out the goal, then decides:
            │ Orchestrator│  one step, or does it need a plan?
            └─┬───┬─────┬┘
     one step │   │     │ one step
   (read-only)│   │     │ (one change)
              │   │ needs a plan
              ▼   ▼     ▼
          Reader  Planning agent  Writer ── you confirm ──▶ GitHub
             ▲      │   ▲   │                                │
             └─step─┘   │   └──step──▶ Writer                │
               result   │                                   ▼
                        └────────── result ─────────── Verifier (re-reads GitHub)
```

1. **Orchestrator** ([orchestrator.py](backend/orchestrator.py)) — reads the message and works out the goal in one sentence, then decides the route:
   - **read-only** requests (questions, lookups, explaining code) go to the **Reader** in one step;
   - **one change** that doesn't depend on reading files first (create a branch, open an issue) goes to the **Writer** in one step;
   - anything bigger goes to the **Planning agent**.
   It never touches GitHub itself. If the Reader discovers a request needs a change after all, it hands it back and the orchestrator passes it to the planner.
2. **Planning agent** ([agents/planner.py](backend/agents/planner.py)) — owns multi-step runs. It writes a plan in which every step has an *expected* outcome, sends each step to the Reader or Writer, and gets every output back. If an output is what the step expected, the plan continues; if not, the planner revises the plan and carries on with the new one.
3. **Reader** ([agents/reader.py](backend/agents/reader.py)) — read-only investigation with 40 read tools: the repository index, file outlines, single functions, line ranges, commits, diffs, issues, pull requests, and the user's saved history (memory). It never sees a write tool.
4. **Writer** ([agents/writer.py](backend/agents/writer.py)) — proposes exactly one change, using only the 19 write tools. The proposal is checked before you see it (an `edit_file` whose text no longer matches, or a branch that already exists, is caught here) and shown with a diff. It runs only after you press **Confirm**.
5. **Verifier** — after a confirmed write, re-reads GitHub to check the change really landed (the file has the committed content, the branch exists, the PR is merged…), then the repository index is refreshed.

Progress streams to the browser as it happens (agent hand-offs, tool calls, plan revisions), so the chat shows a live timeline of which agent is doing what.

### Guardrails

- **Nothing changes without your confirmation**, and destructive actions (deleting a file or branch, merging) get a red warning card.
- **No runaway runs.** There's no fixed step cap; instead, identical tool calls are answered from cache and flagged, a plan step that keeps coming back ends the run, and after 100 tool calls a run pauses with a **Continue** button.
- **Right repository.** A change aimed at a repository you didn't select (and didn't name) is rejected; a request about your GitHub profile can only write to your profile repository (`<login>/<login>`).
- **Cancel means stop.** Cancelling a proposal ends that attempt; the planner never re-proposes it.

## Reading code without reading everything

- **Repository index** ([repo_index.py](backend/repo_index.py)) — each branch's file tree is stored in Postgres, stamped with the commit it came from. Every use makes one small GitHub request for the branch's head commit, which both proves the user can still see the repo and tells whether the stored tree is stale; the tree is only refetched when the commit moved, and after every confirmed write.
- **Codebase outline** ([codebase.py](backend/codebase.py)) — for "explain this project" questions, the whole repository is downloaded once as an archive and every source file's classes and functions are listed (signature, line number, first docstring line), cached per commit. The orchestrator hands this outline to the Reader up front, so it reasons from the real code and only opens function bodies where it must.
- **Targeted reads** — `get_file_outline`, `get_function_source` (one function, returned whole), and `get_file_lines` (an exact range). Files over 200 lines come back from `get_file_contents` as an outline instead of their full body.

## Memory

Stored in Postgres, in three layers:

| Layer | What | Updated |
|---|---|---|
| Conversation | `messages` — every message, with its agent timeline and plan; `conversations` — title, repository, and chosen widgets | as each turn starts and ends |
| Summary | `conversation_summaries` — a rolling summary of each chat | after every assistant message, in the background |
| What the agents did | `agent_runs`, `plans`, `agent_steps`, `tool_calls`, `pending_actions` | as each step runs |

At the start of every turn, the agents get the chat's summary plus every message it doesn't cover yet, verbatim, so a slow or failed summary never loses a turn. The Reader can also look further back with `query_memory`: SQL it writes over read-only views of the user's own chats, runs, plans, and tool calls (see [Security notes](#security-notes) for how that's contained).

## Architecture

```
browser ──▶ frontend (React/Vite)
               │  /api/*  (Vite proxy locally; a Vercel rewrite when hosted)
               ▼
            backend (FastAPI) ──▶ GitHub REST API
               │        └──────▶ Gemini (OpenAI-compatible endpoint)
               ▼
            Postgres (Supabase)
```

- The browser only ever talks to its own origin; `/api/*` is forwarded to the backend, so the session cookie is first-party.
- Database writes the user doesn't need to wait on (trace rows, messages, summaries) go through a single background queue ([db/background.py](backend/db/background.py)), in order. Before a run pauses for a confirmation or Continue, the queue is flushed so the next request can resume from saved state.

## Tech stack

**Backend**
- FastAPI + Uvicorn
- Gemini via the `openai` Python SDK against Gemini's OpenAI-compatible endpoint (default model `gemini-flash-lite-latest`); plans and routing decisions use JSON-schema structured output
- Postgres on Supabase via `psycopg` 3 + `psycopg-pool`
- `cryptography` (Fernet) — GitHub tokens are encrypted at rest
- `sqlglot` — parses and validates the SQL the memory tool runs
- `requests` — GitHub REST calls; `ddgs` — web search as a last resort

**Frontend**
- React 19 + Vite 8, plain CSS
- Three.js — the landing page's 3D commit graph (loaded only on that page, in its own chunk)
- `@fontsource` Fraunces + Inter
- `oxlint`; Playwright only for ad-hoc local screenshot scripts

## Project structure

```
git_show/
├── backend/
│   ├── main.py            # HTTP routes: sign-in, install, chat, confirm/cancel, runs, conversations
│   ├── orchestrator.py    # goal + route: Reader / Writer in one step, or the Planning agent
│   ├── runtime.py         # run plumbing: events, saving state, the confirmation pause, apply + verify writes
│   ├── agents/
│   │   ├── planner.py     # Planning agent: plans, checks each output, revises
│   │   ├── reader.py      # Reader agent + post-write verification
│   │   ├── writer.py      # Writer agent: one validated proposal per step
│   │   ├── common.py      # run context, tool-calling loop, budget and loop detection
│   │   ├── toolsets.py    # which tools each agent can see
│   │   └── prompts.py     # shared prompt rules
│   ├── github_tools.py    # GitHub REST wrappers + tool schemas
│   ├── repo_index.py      # persistent per-branch file index
│   ├── codebase.py        # whole-codebase outline from one archive download
│   ├── repo_panel.py      # the chat's repository panel: tree, commits, summary, branches, PRs, contributors
│   ├── memory.py          # query_memory: validated, sandboxed SQL over the user's history
│   ├── summaries.py       # rolling conversation summaries
│   ├── llm.py             # Gemini client, JSON/schema completions
│   ├── db/
│   │   ├── schema.sql     # all tables, memory views, and roles (idempotent)
│   │   ├── migrate.py     # applies schema.sql
│   │   ├── __init__.py    # sessions, conversations, messages, summaries
│   │   ├── runs.py        # runs, plans, steps, tool calls, pending actions, trace
│   │   ├── repos.py       # repository index storage
│   │   └── background.py  # ordered background writer
│   ├── requirements.txt
│   └── .env.example
└── frontend/
    ├── src/
    │   ├── main.jsx           # routes "/" to Landing and "/app" to App; starts backend warm-up
    │   ├── Landing.jsx/.css   # landing page: 3D scroll story, agent diagram
    │   ├── landing/
    │   │   ├── commitGraph3d.js  # Three.js commit graph scene
    │   │   ├── Transcript.jsx    # line-by-line printed agent transcripts
    │   │   └── AgentTree.jsx     # animated agent diagram
    │   ├── App.jsx/.css       # chat app: history sidebar, agent timeline, plan card, confirm card
    │   ├── RepoPicker.jsx     # searchable repository picker
    │   ├── RepoPanel.jsx      # repository panel widgets, dragging and snapping, the full-tree window
    │   ├── widgetLayout.js    # the widget list, and where each widget is (home slot or floating)
    │   ├── WidgetPicker.jsx   # the window for choosing a chat's widgets
    │   ├── warmup.js          # wakes a sleeping backend on page load
    │   └── index.css
    ├── vercel.json            # /api/* rewrite to the backend when hosted on Vercel
    └── vite.config.js         # dev server + /api proxy to :8000
```

## Setup (local)

### Prerequisites
- Python 3.12+ and Node.js 18+
- A [Gemini API key](https://aistudio.google.com/apikey)
- A Supabase project (any Postgres works) — you need its **session pooler** connection string
- A [GitHub App](https://github.com/settings/apps) with:
  - **Callback URL** `http://localhost:5173/api/auth/github/callback`
  - **Permissions** for what Git Show should do, e.g. Contents, Pull requests, Issues: read & write; Metadata: read. Repository access is governed by these permissions, not by anything the app requests at sign-in — if a write fails with 403, check them first.

### Backend

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env              # fill in real values
python db/migrate.py              # creates tables, memory views, and roles
uvicorn main:app --reload --port 8000
```

| Variable | Required | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | yes | Gemini API key |
| `GEMINI_MODEL` | no | Model id (default `gemini-flash-lite-latest`) |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | yes | GitHub App's client credentials |
| `GITHUB_CALLBACK_URL` | yes | Must match a callback URL registered on the App |
| `FRONTEND_URL` | yes | Where users land after sign-in (they're sent to `FRONTEND_URL/app`) |
| `SUPABASE_DB_URL` | yes | Postgres connection string (URL-encode special characters in the password) |
| `TOKEN_ENCRYPTION_KEY` | yes | Fernet key for encrypting GitHub tokens; generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Changing it signs everyone out. |
| `GITHUB_APP_SLUG` | no | The App's URL name (`github.com/apps/<slug>`); shows the **Install** button |
| `SERVE_FRONTEND` | no | `1` to serve the built frontend from FastAPI (single-server hosting) |
| `CORS_ORIGINS` | no | Comma-separated origins, only if the frontend is on another origin |
| `COOKIE_SECURE` | no | Defaults to on when `FRONTEND_URL` is https |

### Frontend

```bash
cd frontend
npm install
npm run dev        # Vite on :5173, proxying /api to :8000
```

Open `http://localhost:5173` for the landing page, `http://localhost:5173/app` for the chat. `npm run build` makes a production build; `npm run lint` runs oxlint.

## Deploying

Local development needs none of this.

### GitHub App settings

- **Callback URL**: add `https://<your-domain>/api/auth/github/callback` (keep the localhost one; Apps accept several).
- **Request user authorization (OAuth) during installation**: on — installing then also signs the user in.
- **Where can this GitHub App be installed?** → *Any account*, if other people should use it.

Signing in is not the same as installing: a user's token only reaches repositories where the App is **installed**. A signed-in user with no accessible repositories sees an **Install Git Show on your repositories** button (`/api/auth/github/install`, needs `GITHUB_APP_SLUG`).

### Frontend on Vercel, backend on Render (current setup)

- **Vercel**: import the repo with **Root Directory** `frontend` (Vite preset, `npm run build`, output `dist`). No environment variables — the frontend has no secrets. [`frontend/vercel.json`](frontend/vercel.json) rewrites `/api/*` to the backend, keeping one origin.
- **Render** (web service): **Root Directory** `backend`, build `pip install -r requirements.txt`, start `uvicorn main:app --host 0.0.0.0 --port $PORT`, health check `/api/health`. Set the backend variables above, with `FRONTEND_URL` and `GITHUB_CALLBACK_URL` on the Vercel domain, and the same `TOKEN_ENCRYPTION_KEY` as anywhere else that shares the database.
- Render's free tier sleeps after 15 idle minutes and takes up to a minute to wake. The frontend wakes it as soon as any page loads and pings it every 10 minutes while a page is visible; a sign-in click before it's awake waits on the site with a "Starting the server…" note.

### Alternatively: one server

```bash
cd frontend && npm install && npm run build
cd ../backend && SERVE_FRONTEND=1 uvicorn main:app --host 0.0.0.0 --port $PORT
```

Either way, the backend needs a long-running host (Render, Railway, Fly.io, a VM): runs stream for up to a minute or more and database writes happen on a background thread, which serverless functions would cut off. Host it near the database — every query crosses that distance.

## API reference

| Method & path | Purpose |
|---|---|
| `GET /api/health` | Liveness check |
| `GET /api/config` | Public settings for the frontend (the install link, if configured) |
| `GET /api/auth/github/login` | Start GitHub sign-in (state-checked) |
| `GET /api/auth/github/install` | Start installing the GitHub App (state-checked) |
| `GET /api/auth/github/callback` | Finish sign-in or installation; sets the session cookie, redirects to `/app` |
| `GET /api/auth/me` | The signed-in user, or 401 |
| `POST /api/auth/logout` | End the session |
| `GET /api/github/repos` | The user's accessible repositories, for the picker |
| `GET /api/repos/{owner}/{repo}/tree` | Every path in the repository's default branch, for the repository panel |
| `GET /api/repos/{owner}/{repo}/commits` | The latest commits on the default branch |
| `GET /api/repos/{owner}/{repo}/branches` | Every branch, with how far each is ahead of / behind the default branch |
| `GET /api/repos/{owner}/{repo}/pulls` | Open pull requests, most recently updated first |
| `GET /api/repos/{owner}/{repo}/contributors` | Top contributors by commit count |
| `GET /api/repos/{owner}/{repo}/issues` | Open issues (not pull requests), most recently updated first |
| `GET /api/repos/{owner}/{repo}/ci` | The latest GitHub Actions run of each workflow |
| `GET /api/repos/{owner}/{repo}/release` | The latest release and how many commits the default branch has gained since |
| `GET /api/repos/{owner}/{repo}/languages` | Each language's share of the code |
| `GET /api/repos/{owner}/{repo}/summary` | A short model-written summary, main stack, and suggested questions (cached per repository for 6 hours) |
| `POST /api/chat` | Start a run (signed-in only); streams NDJSON events |
| `POST /api/github/actions/execute` | Confirm a proposed change; streams the rest of the run |
| `POST /api/github/actions/cancel` | Cancel a proposed change; streams the wrap-up |
| `POST /api/runs/{id}/continue` | Resume a run that paused after its tool budget |
| `GET /api/runs/{id}/trace` | A run's full trace: plan versions, steps, tool calls |
| `GET /api/conversations` | The user's chats, newest first |
| `GET /api/conversations/{id}` | One chat with its messages |
| `GET /api/conversations/{id}/changes` | The changes confirmed and made in a chat |
| `PUT /api/conversations/{id}/widgets` | Save which repository-panel widgets a chat shows (at most 6) |
| `DELETE /api/conversations/{id}` | Delete a chat |

Streaming endpoints send one JSON object per line: `agent` (a hand-off), `step` (a tool call), `plan` (a new plan version), and a final `final` event with the reply, and optionally `pending_action`, `can_continue`, or `signed_out`.

## Frontend

- **Landing page (`/`)**: a Three.js commit graph that grows on load while the headline prints letter by letter; scrolling flies the camera through a four-stage story (ask, plan, confirm, remember), each stage printing a real transcript line by line, with side branches colored by agent. A "Who does what" section animates the agent tree with pulses tracing a request. Reduced-motion users get the content without motion.
- **Chat (`/app`)**: signed-out visitors see the chat but can't type until they sign in. A sidebar lists past chats; each reply shows its agent timeline (collapsible), the plan checklist, and, for proposals, a card with **View changes** (diff), **Details**, **Cancel**, and **Confirm**.
- **Repository panel**: once a repository is chosen and the first message is sent, widgets float as cards in the main window: branches (ahead/behind the default branch), languages, and top contributors on the left; the project tree (click it for the whole tree in a window, with a filter), the latest commits, and a summary of the repository on the right. The chat sits between the two columns. Below 1200px wide they're opened from a top-bar button and float over the chat as one list. Tree, commits, branches, pull requests, and the other GitHub-backed widgets refresh after a confirmed change.
- **Movable widgets** (1200px and wider): drag a widget by its header anywhere in the chat area. Near one of the six home slots it snaps in magnetically (swapping with whatever is there); anywhere else it floats where it's dropped. Double-click a header to send that widget home. The layout is saved in the browser.
- **Choosing widgets**: the widgets button in the top bar opens a window with a switch for each of 13 widgets, up to 6 on at once. Besides the six above (on by default), there are open pull requests, open issues, CI status (latest run of each GitHub Actions workflow), latest release (and commits since), the current plan, the changes confirmed in this chat, and suggested questions (a click puts one in the chat box). The choice is saved with the chat in Postgres (`conversations.widgets`); a new chat shows the defaults. A widget switched on takes the first free place, preferring its own column. Switched-off widgets aren't fetched at all. The same window has **Reset positions** (after dragging) and **Use defaults**.
- Replies are rendered from a small, HTML-escaped Markdown subset (headings, lists, tables, code blocks, inline code, bold/italic).

## Security notes

- **Secrets** stay in `backend/.env` (gitignored) and the host's environment; the frontend has none.
- **Sessions**: the cookie is httponly, SameSite=Lax, and Secure over https; only a SHA-256 hash of it is stored. GitHub access and refresh tokens are encrypted with `TOKEN_ENCRYPTION_KEY`; expired tokens are refreshed, and a token GitHub rejects signs the user out.
- **Writes** never run without confirmation; confirmations are single-use (a double-click can't run a write twice) and expire after an hour.
- **Memory SQL** is model-written, so it's contained three ways: it must parse as a single `SELECT` over the `memory.*` views using allow-listed functions only; it runs as the `gitshow_memory` role, which can read nothing else; and it runs read-only with a 5-second timeout. The views only show the signed-in user's own rows.
- **Database**: every table has row-level security with no policies, so the Supabase publishable key can read nothing; the backend connects directly as `postgres`. Abandoned transactions are closed after 60 seconds (`idle_in_transaction_session_timeout`).

## Known limitations

- The first request after the free Render instance has slept waits up to a minute (see [Deploying](#deploying)).
- Every model call uses `gemini-flash-lite-latest` — fast and cheap, but a multi-step change can take 30–60 seconds, and much of the orchestration exists to keep a small model on track.
- In-process caches (index head checks, codebase outlines) and the background writer are per process; multiple workers work, but each keeps its own caches.
- No automated test suite in the repository; the agent flows were tested with scripted model responses against a real database, and against the live model during development.
