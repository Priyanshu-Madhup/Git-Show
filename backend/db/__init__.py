"""Postgres (Supabase) persistence for sessions, conversations, messages, and
summaries. Agent runs live in db/runs.py, the repository index in
db/repos.py. Schema lives in db/schema.sql."""

import hashlib
import os
import threading
import time
from contextlib import contextmanager
from datetime import timedelta

from cryptography.fernet import Fernet
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

SESSION_TTL = timedelta(days=7)
PENDING_ACTION_TTL = timedelta(hours=1)

# Every query to the Supabase pooler is a long round trip (~0.7 s from this
# machine), so only health-check a pooled connection before reuse if it has
# sat idle long enough that the pooler might have closed it.
IDLE_CHECK_SECONDS = 30

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()
_last_used: dict[int, float] = {}
_fernet: Fernet | None = None


def _check_if_idle(conn):
    if time.monotonic() - _last_used.get(id(conn), 0) < IDLE_CHECK_SECONDS:
        return
    ConnectionPool.check_connection(conn)


def _get_pool() -> ConnectionPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            # Supabase's session pooler caps concurrent clients on small
            # plans, so keep this modest.
            _pool = ConnectionPool(
                os.environ["SUPABASE_DB_URL"],
                min_size=1,
                max_size=6,
                max_idle=600,
                check=_check_if_idle,
                kwargs={"connect_timeout": 15},
                open=True,
            )
    return _pool


@contextmanager
def connection():
    with _get_pool().connection() as conn:
        try:
            yield conn
        finally:
            _last_used[id(conn)] = time.monotonic()


def _cipher() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(os.environ["TOKEN_ENCRYPTION_KEY"].encode())
    return _fernet


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --- users & sessions -------------------------------------------------------


def upsert_user(github_id: int, login: str, name: str | None, avatar_url: str | None) -> int:
    with connection() as conn:
        row = conn.execute(
            """
            insert into users (github_id, login, name, avatar_url)
            values (%s, %s, %s, %s)
            on conflict (github_id) do update
                set login = excluded.login,
                    name = excluded.name,
                    avatar_url = excluded.avatar_url,
                    updated_at = now()
            returning id
            """,
            (github_id, login, name, avatar_url),
        ).fetchone()
    return row[0]


def _encrypt(value):
    return _cipher().encrypt(value.encode()).decode() if value else None


def _expiry(expires_in):
    try:
        return timedelta(seconds=int(expires_in)) if expires_in else None
    except (TypeError, ValueError):
        return None


def create_session(token: str, user_id: int, access_token: str, refresh_token=None, expires_in=None) -> None:
    expires = _expiry(expires_in)
    with connection() as conn:
        conn.execute("delete from sessions where expires_at < now()")
        conn.execute(
            """
            insert into sessions
                (token_hash, user_id, encrypted_access_token, encrypted_refresh_token,
                 access_token_expires_at, expires_at)
            values (%s, %s, %s, %s, now() + %s, now() + %s)
            """,
            (hash_session_token(token), user_id, _encrypt(access_token), _encrypt(refresh_token), expires, SESSION_TTL),
        )


def update_session_tokens(token: str, access_token: str, refresh_token=None, expires_in=None) -> None:
    with connection() as conn:
        conn.execute(
            """
            update sessions
            set encrypted_access_token = %s,
                encrypted_refresh_token = coalesce(%s, encrypted_refresh_token),
                access_token_expires_at = now() + %s
            where token_hash = %s
            """,
            (_encrypt(access_token), _encrypt(refresh_token), _expiry(expires_in), hash_session_token(token)),
        )


def delete_session_by_hash(token_hash: str) -> None:
    with connection() as conn:
        conn.execute("delete from sessions where token_hash = %s", (token_hash,))


def get_session(token: str | None) -> dict | None:
    if not token:
        return None
    token_hash = hash_session_token(token)
    with connection() as conn:
        row = conn.execute(
            """
            select s.encrypted_access_token, u.id, u.login, u.name, u.avatar_url,
                   s.encrypted_refresh_token,
                   s.access_token_expires_at is not null
                       and s.access_token_expires_at < now() + interval '2 minutes'
            from sessions s join users u on u.id = s.user_id
            where s.token_hash = %s and s.expires_at > now()
            """,
            (token_hash,),
        ).fetchone()
    if not row:
        return None
    encrypted, user_id, login, name, avatar_url, encrypted_refresh, needs_refresh = row
    return {
        "token_hash": token_hash,
        "user_id": user_id,
        "access_token": _cipher().decrypt(encrypted.encode()).decode(),
        "refresh_token": _cipher().decrypt(encrypted_refresh.encode()).decode() if encrypted_refresh else None,
        "needs_refresh": needs_refresh,
        "user": {"login": login, "name": name, "avatar_url": avatar_url},
    }


def delete_session(token: str) -> None:
    with connection() as conn:
        conn.execute("delete from sessions where token_hash = %s", (hash_session_token(token),))


# --- conversations & messages ----------------------------------------------


def claim_conversation(conn, conversation_id, user_id) -> bool:
    """Create the conversation if it's new, and report whether it belongs to
    user_id (None for signed-out chats). Keeps one user from reading or
    writing another's chat by sending their chat_id."""
    row = conn.execute(
        """
        insert into conversations (id, user_id) values (%s, %s)
        on conflict (id) do update set updated_at = now()
            where conversations.user_id is not distinct from excluded.user_id
        returning id
        """,
        (conversation_id, user_id),
    ).fetchone()
    return row is not None


TITLE_MAX_CHARS = 60


def _title_from(message: str) -> str:
    first_line = message.strip().splitlines()[0] if message.strip() else "New chat"
    if len(first_line) <= TITLE_MAX_CHARS:
        return first_line
    return first_line[: TITLE_MAX_CHARS - 1].rstrip() + "…"


def add_message(
    conversation_id,
    user_id,
    role,
    content,
    steps=None,
    pending_action_id=None,
    repo=None,
    run_id=None,
    plan=None,
    can_continue=False,
) -> int | None:
    """Append a message to a chat. Signed-out chats are stored too (they
    feed the summary), but only signed-in users can list them. The first
    user message becomes the chat's title."""
    with connection() as conn:
        if not claim_conversation(conn, conversation_id, user_id):
            return None
        if role == "user":
            conn.execute(
                """
                update conversations
                set title = coalesce(title, %s), repo = coalesce(%s, repo)
                where id = %s
                """,
                (_title_from(content), repo, conversation_id),
            )
        if can_continue and run_id:
            # Only the newest message of a paused run offers Continue.
            conn.execute("update messages set can_continue = false where run_id = %s", (run_id,))
        row = conn.execute(
            """
            insert into messages
                (conversation_id, role, content, steps, pending_action_id, run_id, plan, can_continue)
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            returning id
            """,
            (
                conversation_id,
                role,
                content,
                Jsonb(steps or []),
                pending_action_id,
                run_id,
                Jsonb(plan) if plan else None,
                can_continue,
            ),
        ).fetchone()
    return row[0]


def clear_continue(run_id) -> None:
    with connection() as conn:
        conn.execute("update messages set can_continue = false where run_id = %s", (run_id,))


def list_conversations(user_id, limit=50) -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            """
            select c.id, c.title, c.repo, c.updated_at
            from conversations c
            where c.user_id = %s
              and exists (select 1 from messages m where m.conversation_id = c.id)
            order by c.updated_at desc
            limit %s
            """,
            (user_id, limit),
        ).fetchall()
    return [
        {"id": str(r[0]), "title": r[1] or "New chat", "repo": r[2], "updated_at": r[3].isoformat()}
        for r in rows
    ]


def get_conversation(conversation_id, user_id) -> dict | None:
    """A chat and its messages, or None if it isn't this user's. Proposed
    writes come back as pending_action only while they can still be
    confirmed; otherwise action_state says what became of them."""
    with connection() as conn:
        conv = conn.execute(
            "select id, title, repo, widgets from conversations where id = %s and user_id = %s",
            (conversation_id, user_id),
        ).fetchone()
        if not conv:
            return None
        rows = conn.execute(
            """
            select m.role, m.content, m.steps, m.plan, m.run_id, m.can_continue,
                   r.status,
                   p.id, p.tool, p.arguments, p.preview, p.status, p.expires_at > now()
            from messages m
            left join pending_actions p on p.id = m.pending_action_id
            left join agent_runs r on r.id = m.run_id
            where m.conversation_id = %s
            order by m.id
            """,
            (conversation_id,),
        ).fetchall()

    messages = []
    for (
        role, content, steps, plan, run_id, can_continue, run_status,
        action_id, tool, arguments, preview, action_status, live,
    ) in rows:
        message = {"role": role, "content": content, "steps": steps, "plan": plan}
        if run_id:
            message["run_id"] = str(run_id)
        if can_continue and run_status == "paused":
            message["can_continue"] = True
        if action_id:
            if action_status == "pending" and live:
                message["pending_action"] = {
                    "id": action_id, "tool": tool, "arguments": arguments, "preview": preview,
                }
            else:
                message["action_state"] = "expired" if action_status == "pending" else action_status
        messages.append(message)
    return {
        "id": str(conv[0]),
        "title": conv[1] or "New chat",
        "repo": conv[2],
        "widgets": conv[3],
        "messages": messages,
    }


# Argument keys worth showing when listing a change; file contents and the
# like are left out.
CHANGE_ARGUMENT_KEYS = (
    "path", "branch", "from_branch", "title", "pull_number", "issue_number", "tag_name", "name", "head", "base",
)


def list_executed_actions(conversation_id, user_id, limit=30) -> list[dict]:
    """The changes confirmed and made in a chat, newest first."""
    with connection() as conn:
        rows = conn.execute(
            """
            select p.tool, p.arguments, p.result, p.resolved_at
            from pending_actions p
            join conversations c on c.id = p.conversation_id
            where p.conversation_id = %s and c.user_id = %s and p.status = 'executed'
            order by p.resolved_at desc nulls last
            limit %s
            """,
            (conversation_id, user_id, limit),
        ).fetchall()
    changes = []
    for tool, arguments, result, resolved_at in rows:
        arguments = arguments or {}
        result = result if isinstance(result, dict) else {}
        url = result.get("html_url") or result.get("url")
        changes.append(
            {
                "tool": tool,
                "repo": f"{arguments.get('owner')}/{arguments.get('repo')}" if arguments.get("owner") else None,
                "details": {k: arguments[k] for k in CHANGE_ARGUMENT_KEYS if arguments.get(k) not in (None, "")},
                "url": url if isinstance(url, str) and url.startswith("https://github.com/") else None,
                "at": resolved_at.isoformat() if resolved_at else None,
            }
        )
    return changes


def set_conversation_widgets(conversation_id, user_id, widgets) -> bool:
    """Save which repository-panel widgets a chat shows. The chat may not be
    stored yet (its first message is still being written), so it's claimed
    first, exactly as adding a message would."""
    with connection() as conn:
        if not claim_conversation(conn, conversation_id, user_id):
            return False
        conn.execute("update conversations set widgets = %s where id = %s", (widgets, conversation_id))
    return True


def delete_conversation(conversation_id, user_id) -> bool:
    with connection() as conn:
        row = conn.execute(
            "delete from conversations where id = %s and user_id = %s returning id",
            (conversation_id, user_id),
        ).fetchone()
    return row is not None


# --- summaries --------------------------------------------------------------


def get_context(conversation_id, user_id) -> dict:
    """What the agents know about the chat so far, in one round trip: the
    rolling summary plus every message newer than what it covers, verbatim.
    owner is 'new' (no such chat yet), 'mine', or 'other' (someone else's
    chat id — the caller must refuse)."""
    with connection() as conn:
        row = conn.execute(
            """
            select c.user_id is not distinct from %s,
                   coalesce(s.summary, ''),
                   coalesce((
                       select json_agg(json_build_object('role', m.role, 'content', m.content) order by m.id)
                       from messages m
                       where m.conversation_id = c.id and m.id > coalesce(s.summarized_through, 0)
                   ), '[]'::json)
            from conversations c
            left join conversation_summaries s on s.conversation_id = c.id
            where c.id = %s
            """,
            (user_id, conversation_id),
        ).fetchone()
    if not row:
        return {"owner": "new", "summary": "", "recent": []}
    mine, summary, recent = row
    if not mine:
        return {"owner": "other", "summary": "", "recent": []}
    return {"owner": "mine", "summary": summary, "recent": recent}


def get_unsummarized(conversation_id) -> tuple[str, int, list[dict]]:
    with connection() as conn:
        row = conn.execute(
            "select summary, summarized_through from conversation_summaries where conversation_id = %s",
            (conversation_id,),
        ).fetchone()
        summary, through = row if row else ("", 0)
        rows = conn.execute(
            "select id, role, content from messages where conversation_id = %s and id > %s order by id",
            (conversation_id, through),
        ).fetchall()
    return summary, through, [{"id": i, "role": r, "content": c} for i, r, c in rows]


def save_summary(conversation_id, summary: str, expected_through: int, new_through: int) -> bool:
    """Store a new summary only if nobody else advanced it in the meantime
    (optimistic concurrency); returns whether it was stored."""
    with connection() as conn:
        if expected_through == 0:
            row = conn.execute(
                """
                insert into conversation_summaries (conversation_id, summary, summarized_through)
                values (%s, %s, %s)
                on conflict (conversation_id) do update
                    set summary = excluded.summary,
                        summarized_through = excluded.summarized_through,
                        updated_at = now()
                    where conversation_summaries.summarized_through = 0
                returning 1
                """,
                (conversation_id, summary, new_through),
            ).fetchone()
        else:
            row = conn.execute(
                """
                update conversation_summaries
                set summary = %s, summarized_through = %s, updated_at = now()
                where conversation_id = %s and summarized_through = %s
                returning 1
                """,
                (summary, new_through, conversation_id, expected_through),
            ).fetchone()
    return row is not None
