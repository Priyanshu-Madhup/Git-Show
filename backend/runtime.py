"""Run plumbing shared by the orchestrator and the Planning agent: progress
events, persistence of a run segment, the pause for a human confirmation,
and applying a confirmed write (execute, check GitHub's real state, refresh
the repository index)."""

import json
import logging
import secrets
import time
import uuid

import db
import llm
import repo_index
import summaries
from agents import reader, writer
from agents.common import (
    RUN_TOOL_BUDGET,
    GitHubAuthError,
    RunContext,
    conversation_messages,
    is_auth_failure,
    repo_context_message,
)
from db import background
from db import runs as run_store

log = logging.getLogger("gitshow.runtime")

WRITE_LABELS = {
    "edit_file": lambda a: f"Committing an edit to {a.get('path')}",
    "restore_file": lambda a: f"Restoring {a.get('path')} from {str(a.get('ref', ''))[:7]}",
    "create_or_update_file": lambda a: f"Writing {a.get('path')}",
    "delete_file": lambda a: f"Deleting {a.get('path')}",
    "create_branch": lambda a: f"Creating branch {a.get('branch')}",
    "delete_branch": lambda a: f"Deleting branch {a.get('branch')}",
    "create_pull_request": lambda a: f"Opening pull request \"{a.get('title')}\"",
    "merge_pull_request": lambda a: f"Merging pull request #{a.get('pull_number')}",
    "create_issue": lambda a: f"Opening issue \"{a.get('title')}\"",
    "add_issue_comment": lambda a: f"Commenting on #{a.get('issue_number')}",
}

DONE_LABELS = {
    "edit_file": lambda a: f"edited `{a.get('path')}`",
    "restore_file": lambda a: f"restored `{a.get('path')}` to its version at `{str(a.get('ref', ''))[:7]}`",
    "create_or_update_file": lambda a: f"wrote `{a.get('path')}`",
    "delete_file": lambda a: f"deleted `{a.get('path')}`",
    "create_branch": lambda a: f"created branch **{a.get('branch')}**",
    "delete_branch": lambda a: f"deleted branch **{a.get('branch')}**",
    "create_pull_request": lambda a: f"opened pull request **\"{a.get('title')}\"**",
    "merge_pull_request": lambda a: f"merged pull request **#{a.get('pull_number')}**",
    "create_issue": lambda a: f"opened issue **\"{a.get('title')}\"**",
    "add_issue_comment": lambda a: f"commented on **#{a.get('issue_number')}**",
}


# --- contexts ------------------------------------------------------------------


def new_context(*, message, chat_id, repo, session) -> RunContext:
    return RunContext(
        run_id=str(uuid.uuid4()),
        conversation_id=chat_id,
        user_id=session["user_id"] if session else None,
        token=session["access_token"] if session else None,
        session_hash=session["token_hash"] if session else None,
        user_login=(session.get("user") or {}).get("login") if session else None,
        repo=repo,
        user_request=message,
        state={"observations": [], "evidence": [], "tool_calls_used": 0, "budget_limit": RUN_TOOL_BUDGET},
    )


def restore_context(run, session) -> RunContext:
    """Rebuild a run's context from its saved state (after a confirmation
    or a Continue), with a fresh tool budget for this segment."""
    ctx = RunContext(
        run_id=run["id"],
        conversation_id=str(run["conversation_id"]),
        user_id=session["user_id"],
        token=session["access_token"],
        session_hash=session["token_hash"],
        user_login=(session.get("user") or {}).get("login"),
        repo=run["repo"],
        user_request=run["user_request"],
        state=run["state"] or {},
    )
    ctx.state.setdefault("observations", [])
    ctx.state.setdefault("evidence", [])
    ctx.extend_budget()
    return ctx


def load_context(ctx):
    """Conversation memory + repository overview for the agents."""
    context = db.get_context(ctx.conversation_id, ctx.user_id)
    ctx.context_messages = conversation_messages(context)
    overview = None
    owner, name = ctx.owner_repo
    if ctx.token and owner:
        overview = repo_index.overview(ctx.token, owner, name)
    ctx.repo_messages = who_message(ctx) + repo_context_message(ctx, overview)
    return {**context, "overview": overview}


def who_message(ctx):
    """Who is signed in, and where their GitHub profile README lives — so
    "my profile" or "my README" never needs a question back."""
    if not ctx.user_login:
        return []
    login = ctx.user_login
    return [
        {
            "role": "system",
            "content": (
                f"The signed-in GitHub user is {login}. Their GitHub profile README (the page shown "
                f"on github.com/{login}) is README.md in the repository {login}/{login}. Requests "
                "about 'my profile', 'my profile README', or 'my GitHub page' mean that file, not "
                "the selected repository."
            ),
        }
    ]


def scoped(ctx, instruction):
    """Stamp the selected repository onto an agent instruction, so agents act
    on it instead of guessing (e.g. searching the user's other repos)."""
    notes = []
    if ctx.repo:
        notes.append(
            f"The repository selected in the picker is {ctx.repo}; use it only if the instruction "
            "doesn't name a repository."
        )
    if ctx.user_login:
        login = ctx.user_login
        notes.append(f"Anything about the user's GitHub profile means README.md in {login}/{login}.")
    return f"{instruction}\n\n[{' '.join(notes)}]" if notes else instruction


# --- events ----------------------------------------------------------------------


def agent_event(ctx, agent, label):
    """An agent hand-off, shown live and kept in the message's timeline."""
    ctx.timeline.append({"kind": "agent", "agent": agent, "label": label})
    return {"type": "agent", "agent": agent, "label": label}


def tool_event(ctx, agent, tool, label):
    ctx.timeline.append({"kind": "tool", "agent": agent, "tool": tool, "label": label})
    return {"type": "step", "agent": agent, "tool": tool, "label": label}


def short(text, limit=90):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def ndjson_stream(gen):
    for event in gen:
        yield json.dumps(event, default=str) + "\n"


# --- persistence -----------------------------------------------------------------


def plan_snapshot(ctx, active_id=None):
    plan = ctx.state.get("plan")
    if not plan:
        return None
    return {
        "version": ctx.state.get("plan_version"),
        "goal": plan.get("goal"),
        "active_step_id": active_id,
        "steps": [
            {"id": s["id"], "agent": s["agent"], "instruction": s["instruction"], "status": s["status"]}
            for s in plan.get("steps", [])
        ],
    }


def checkpoint(ctx, **fields):
    background.enqueue(run_store.update_run, ctx.run_id, state=ctx.state, **fields)


def finish(ctx, reply, *, pending_action=None, can_continue=False, status="completed", error=None):
    """End this segment of the run: persist the run and the assistant
    message, schedule the summary, and return the final event."""
    reply = reply or "I don't have anything to add."
    plan = plan_snapshot(ctx)
    background.enqueue(run_store.update_run, ctx.run_id, status=status, state=ctx.state, error=error)
    background.enqueue(
        db.add_message,
        ctx.conversation_id,
        ctx.user_id,
        "assistant",
        reply,
        steps=ctx.timeline,
        pending_action_id=pending_action["id"] if pending_action else None,
        run_id=ctx.run_id,
        plan=plan,
        can_continue=can_continue,
    )
    summaries.schedule(ctx.conversation_id)
    if status in ("awaiting_confirmation", "paused"):
        # The next request (Confirm / Continue) resumes from the saved state,
        # so it must be durable before the user can click anything.
        background.flush()
    event = {"type": "final", "reply": reply, "run_id": ctx.run_id, "steps": ctx.timeline}
    if plan:
        event["plan"] = plan
    if pending_action:
        event["pending_action"] = pending_action
    if can_continue:
        event["can_continue"] = True
    return event


def guarded(ctx, gen):
    """Make sure every stream ends with a final event and the run is marked
    failed, whatever goes wrong mid-run."""
    try:
        yield from gen
    except GitHubAuthError as exc:
        if ctx.session_hash:
            db.delete_session_by_hash(ctx.session_hash)
        event = finish(
            ctx,
            "Your GitHub sign-in has expired or was revoked, so I stopped. Sign in again (top right) "
            "and resend your message.",
            status="failed",
            error=f"GitHub auth: {exc}",
        )
        yield {**event, "signed_out": True}
    except llm.LLMError as exc:
        yield finish(ctx, f"Something went wrong: {exc}", status="failed", error=str(exc))
    except Exception as exc:
        log.exception("Run %s crashed", ctx.run_id)
        yield finish(
            ctx,
            "Something went wrong while working on that, so I stopped. Try again, or rephrase the request.",
            status="failed",
            error=repr(exc),
        )


# --- writes ------------------------------------------------------------------------


def await_confirmation(ctx, step_id, outcome, *, plan_step_id=None, instruction=None):
    """Pause the run on a Writer proposal until the user confirms or cancels."""
    proposal = outcome.proposal
    tool_call_id = ctx.record_tool_call(step_id, proposal["tool"], proposal["arguments"], None, None, None)
    action_id = secrets.token_urlsafe(16)
    ctx.state["pending"] = {
        "action_id": action_id,
        "step_id": step_id,
        "plan_step_id": plan_step_id,
        "instruction": instruction,
    }
    # The pending action references the run, step, and tool-call rows, so
    # make sure the queued writes for those have landed first.
    background.flush()
    run_store.create_pending_action(
        action_id,
        ctx.session_hash,
        ctx.conversation_id,
        ctx.run_id,
        tool_call_id,
        proposal["tool"],
        proposal["arguments"],
        proposal["preview"],
    )
    ctx.finish_step(step_id, "awaiting_confirmation", outcome.text)
    yield agent_event(ctx, "writer", "Writer agent: waiting for your confirmation")
    yield finish(
        ctx,
        outcome.text,
        pending_action={
            "id": action_id,
            "tool": proposal["tool"],
            "arguments": proposal["arguments"],
            "preview": proposal["preview"],
        },
        status="awaiting_confirmation",
    )


def record_cancel(ctx, action, pending):
    if action.get("tool_call_id"):
        background.enqueue(run_store.complete_tool_call, action["tool_call_id"], {"cancelled": True}, False, 0)
    if pending.get("step_id"):
        ctx.finish_step(pending["step_id"], "cancelled", "The user cancelled this change.")


def apply_confirmed_write(ctx, action_id, action, pending):
    """Generator: run a confirmed write, check GitHub's actual state, and
    refresh the repository index. Returns a dict: status ('completed',
    'failed', or 'repository_check_failed'), observation (full detail for the
    Planning agent), and message (a plain-language line for the user)."""
    tool, args = action["tool"], action["arguments"]
    label = WRITE_LABELS.get(tool, lambda a: f"Running {tool.replace('_', ' ')}")(args)
    yield agent_event(ctx, "writer", "Writer agent: applying the confirmed change")
    yield tool_event(ctx, "writer", tool, label)
    started = time.monotonic()
    caught = None
    try:
        result = writer.execute(ctx.token, tool, args)
        success = True
    except Exception as exc:
        caught = exc
        result, success = {"error": str(exc)}, False
    duration_ms = int((time.monotonic() - started) * 1000)
    if action.get("tool_call_id"):
        background.enqueue(run_store.complete_tool_call, action["tool_call_id"], result, success, duration_ms)
    background.enqueue(run_store.finish_pending_action, action_id, "executed" if success else "failed", result)
    if pending.get("step_id"):
        ctx.finish_step(pending["step_id"], "completed" if success else "failed", json.dumps(result, default=str))
    if not success and is_auth_failure(caught):
        raise GitHubAuthError(result["error"])
    if not success:
        detail = f"GitHub rejected {tool}: {result['error']}"
        return {"status": "failed", "observation": detail, "message": f"That didn't work — {detail}"}

    yield agent_event(ctx, "verifier", "Reader agent: checking the change on GitHub")
    verify_step = ctx.start_step("verifier", f"Verify {tool}", plan_step_id=pending.get("plan_step_id"))
    passed, detail = reader.verify_write(ctx.token, tool, args, result)
    ctx.finish_step(verify_step, "completed" if passed else "failed", detail)
    yield tool_event(ctx, "verifier", tool, ("Confirmed: " if passed else "Check failed: ") + detail)

    index_note = ""
    if tool in repo_index.TREE_CHANGING_TOOLS:
        yield agent_event(ctx, "orchestrator", "Updating the repository index")
        index = repo_index.refresh_after_write(ctx.token, tool, args)
        if index:
            index_note = (
                f"\nRepository index refreshed: {index['branch']} at {index['commit_sha'][:10]}, "
                f"{index['file_count']} entries."
            )
    # Content read before the change is now stale; drop it so the Writer
    # never edits against an old copy of the file.
    if args.get("path"):
        ctx.state["evidence"] = [
            e for e in ctx.state.get("evidence", []) if (e.get("arguments") or {}).get("path") != args["path"]
        ]
    summary = (
        f"{tool} ran. GitHub returned: {json.dumps(result, default=str)}\n"
        f"Repository check {'PASSED' if passed else 'FAILED'}: {detail}{index_note}"
    )
    link = result.get("url") if isinstance(result, dict) else None
    message = (
        f"Done — {DONE_LABELS.get(tool, lambda a: tool.replace('_', ' '))(args)} in `{args.get('owner')}/{args.get('repo')}`. {detail}"
        + (f" [View on GitHub]({link})" if link and link.startswith("http") else "")
        if passed
        else f"{label} ran, but the check afterwards failed: {detail}"
    )
    return {
        "status": "completed" if passed else "repository_check_failed",
        "observation": summary,
        "message": message,
    }

