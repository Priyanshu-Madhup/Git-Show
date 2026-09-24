"""Execution trace and resumable state for agent runs: agent_runs, plans,
agent_steps, tool_calls, and the pending_actions that pause a run for a
human confirmation."""

from psycopg.types.json import Jsonb

from db import PENDING_ACTION_TTL, claim_conversation, connection


def create_run(run_id, conversation_id, user_id, user_request, repo) -> bool:
    with connection() as conn:
        if not claim_conversation(conn, conversation_id, user_id):
            return False
        conn.execute(
            """
            insert into agent_runs (id, conversation_id, user_id, user_request, repo)
            values (%s, %s, %s, %s, %s)
            """,
            (run_id, conversation_id, user_id, user_request, repo),
        )
    return True


def get_run(run_id, user_id) -> dict | None:
    with connection() as conn:
        row = conn.execute(
            """
            select id, conversation_id, user_request, repo, mode, status, state
            from agent_runs where id = %s and user_id is not distinct from %s
            """,
            (run_id, user_id),
        ).fetchone()
    if not row:
        return None
    keys = ("id", "conversation_id", "user_request", "repo", "mode", "status", "state")
    run = dict(zip(keys, row))
    run["id"] = str(run["id"])
    return run


def update_run(run_id, *, status=None, mode=None, state=None, error=None) -> None:
    sets, params = ["updated_at = now()"], []
    if status is not None:
        sets.append("status = %s")
        params.append(status)
        if status in ("completed", "failed"):
            sets.append("completed_at = now()")
    if mode is not None:
        sets.append("mode = %s")
        params.append(mode)
    if state is not None:
        sets.append("state = %s")
        params.append(Jsonb(state))
    if error is not None:
        sets.append("error = %s")
        params.append(error)
    with connection() as conn:
        conn.execute(f"update agent_runs set {', '.join(sets)} where id = %s", (*params, run_id))


def claim_run_for_continue(run_id, user_id) -> dict | None:
    """Atomically move a paused run back to running, so a double-clicked
    Continue can't start it twice."""
    with connection() as conn:
        row = conn.execute(
            """
            update agent_runs set status = 'running', updated_at = now()
            where id = %s and user_id is not distinct from %s and status = 'paused'
            returning id
            """,
            (run_id, user_id),
        ).fetchone()
    return get_run(run_id, user_id) if row else None


def save_plan(run_id, version, goal, reasoning, steps, done) -> None:
    with connection() as conn:
        conn.execute(
            """
            insert into plans (run_id, version, goal, reasoning, steps, done)
            values (%s, %s, %s, %s, %s, %s)
            """,
            (run_id, version, goal, reasoning, Jsonb(steps), done),
        )


def start_step(step_id, run_id, step_number, agent, instruction, plan_version=None, plan_step_id=None) -> None:
    with connection() as conn:
        conn.execute(
            """
            insert into agent_steps (id, run_id, step_number, plan_version, plan_step_id, agent, instruction)
            values (%s, %s, %s, %s, %s, %s, %s)
            """,
            (step_id, run_id, step_number, plan_version, plan_step_id, agent, instruction),
        )


def finish_step(step_id, status, result) -> None:
    with connection() as conn:
        conn.execute(
            """
            update agent_steps set status = %s, result = %s,
                completed_at = case when %s then now() else null end
            where id = %s
            """,
            (status, result, status != "awaiting_confirmation", step_id),
        )


def record_tool_call(tool_call_id, run_id, step_id, tool_name, arguments, result, success, duration_ms) -> None:
    with connection() as conn:
        conn.execute(
            """
            insert into tool_calls (id, run_id, step_id, tool_name, arguments, result, success, duration_ms)
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (tool_call_id, run_id, step_id, tool_name, Jsonb(arguments), Jsonb(result), success, duration_ms),
        )


def complete_tool_call(tool_call_id, result, success, duration_ms) -> None:
    with connection() as conn:
        conn.execute(
            "update tool_calls set result = %s, success = %s, duration_ms = %s where id = %s",
            (Jsonb(result), success, duration_ms, tool_call_id),
        )


def get_trace(run_id, user_id) -> dict | None:
    """A run's full execution trace: plan versions, agent steps, and each
    step's tool calls, in order."""
    with connection() as conn:
        run = conn.execute(
            """
            select id, conversation_id, user_request, repo, mode, status, error, started_at, completed_at
            from agent_runs where id = %s and user_id is not distinct from %s
            """,
            (run_id, user_id),
        ).fetchone()
        if not run:
            return None
        plans = conn.execute(
            "select version, goal, reasoning, steps, done, created_at from plans where run_id = %s order by version",
            (run_id,),
        ).fetchall()
        steps = conn.execute(
            """
            select id, step_number, plan_version, plan_step_id, agent, instruction, status, result,
                   started_at, completed_at
            from agent_steps where run_id = %s order by step_number
            """,
            (run_id,),
        ).fetchall()
        calls = conn.execute(
            """
            select step_id, tool_name, arguments, result, success, duration_ms, created_at
            from tool_calls where run_id = %s order by created_at
            """,
            (run_id,),
        ).fetchall()
    calls_by_step = {}
    for step_id, *rest in calls:
        calls_by_step.setdefault(step_id, []).append(
            dict(zip(("tool", "arguments", "result", "success", "duration_ms", "created_at"), rest))
        )
    run_keys = ("id", "conversation_id", "user_request", "repo", "mode", "status", "error", "started_at", "completed_at")
    plan_keys = ("version", "goal", "reasoning", "steps", "done", "created_at")
    step_keys = (
        "id", "step_number", "plan_version", "plan_step_id", "agent", "instruction", "status", "result",
        "started_at", "completed_at",
    )
    return {
        **dict(zip(run_keys, run)),
        "plans": [dict(zip(plan_keys, p)) for p in plans],
        "steps": [{**dict(zip(step_keys, s)), "tool_calls": calls_by_step.get(s[0], [])} for s in steps],
    }


# --- pending write actions -------------------------------------------------


def create_pending_action(
    action_id, session_hash, conversation_id, run_id, tool_call_id, tool, arguments, preview
) -> None:
    with connection() as conn:
        conn.execute(
            """
            insert into pending_actions
                (id, session_hash, conversation_id, run_id, tool_call_id, tool, arguments, preview, expires_at)
            values (%s, %s, %s, %s, %s, %s, %s, %s, now() + %s)
            """,
            (
                action_id, session_hash, conversation_id, run_id, tool_call_id,
                tool, Jsonb(arguments), Jsonb(preview) if preview else None, PENDING_ACTION_TTL,
            ),
        )


def claim_pending_action(action_id, session_hash, new_status) -> dict | None:
    """Atomically move a pending action to new_status ('executing' or
    'cancelled') and return it, so a double-clicked button can't act twice.
    None if it doesn't exist, isn't this session's, expired, or was already
    handled."""
    with connection() as conn:
        row = conn.execute(
            """
            update pending_actions
            set status = %s,
                resolved_at = case when %s then now() else resolved_at end
            where id = %s and session_hash = %s and status = 'pending' and expires_at > now()
            returning tool, arguments, conversation_id, run_id, tool_call_id, preview
            """,
            (new_status, new_status == "cancelled", action_id, session_hash),
        ).fetchone()
    if not row:
        return None
    keys = ("tool", "arguments", "conversation_id", "run_id", "tool_call_id", "preview")
    action = dict(zip(keys, row))
    action["run_id"] = str(action["run_id"]) if action["run_id"] else None
    return action


def finish_pending_action(action_id, status: str, result) -> None:
    with connection() as conn:
        conn.execute(
            """
            update pending_actions set status = %s, result = %s, resolved_at = now()
            where id = %s
            """,
            (status, Jsonb(result), action_id),
        )
