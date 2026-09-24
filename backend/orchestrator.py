"""The orchestrator: works out what the user wants, then decides who does it.

    user -> orchestrator: goal + "can one agent do this in one step?"
              |-- yes, read-only      -> Reader agent  -> answer
              |-- yes, one change     -> Writer agent  -> confirm -> apply -> check GitHub
              '-- no, needs planning  -> Planning agent (owns the run from here: it calls the
                                         Reader/Writer, checks each output, revises the plan)

It never performs a GitHub operation itself. Entry points return
generators of NDJSON lines for StreamingResponse."""

import json
import time

import codebase
import db
import llm
import runtime
from agents import planner, prompts, reader, toolsets, writer
from agents.common import GitHubAuthError, is_auth_failure
from db import background
from db import runs as run_store

INTERPRET_PROMPT = (
    "You are Git Show's orchestrator. Read the user's latest message (in the context of the "
    "conversation) and work out:\n"
    "1. goal: what they actually want, as one clear sentence. Read requests in git/GitHub terms: "
    "'create a branch legend' means create a git branch named 'legend'. Don't add anything they "
    "didn't ask for.\n"
    "2. Whether ONE agent can do it in ONE step:\n"
    "   - reader: anything read-only — answer a question, look something up, list or show "
    "things, explain or review code, recall earlier chats. One reader step may use many read "
    "tools, so even deep read-only questions are one step.\n"
    "   - writer: exactly one GitHub change that doesn't depend on reading existing file "
    "contents first — create/delete a branch, open an issue or PR, comment, merge a named PR, "
    "star, fork, create a brand-new file whose full content the user gave.\n"
    "   - needs_planning: several steps — editing existing code (it must be read first), "
    "several changes, investigation followed by a change, or anything whose approach depends "
    "on what is found.\n"
    "3. changes_github: true only if the user explicitly asks to create, change, delete, merge, "
    "comment on, or otherwise modify something on GitHub. Asking to be told, shown, explained, "
    "or summarized something is false — answer it, never write a file to answer it.\n"
    "4. about_codebase: true if answering needs an understanding of the repository's code as a "
    "whole or of one directory (explain/summarize/review the project, its architecture, how it "
    "works). codebase_path: that directory, or empty for the whole repository.\n"
    "5. instruction: for a one-step task, a self-contained instruction for that agent with "
    "every name, message, and detail the user gave. Empty when planning is needed.\n"
    "Reply with only the JSON object."
)

INTERPRET_SCHEMA = {
    "title": "interpretation",
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "changes_github": {"type": "boolean"},
        "needs_planning": {"type": "boolean"},
        "agent": {"type": "string", "enum": ["reader", "writer"]},
        "about_codebase": {"type": "boolean"},
        "codebase_path": {"type": "string"},
        "instruction": {"type": "string"},
    },
    "required": ["goal", "changes_github", "needs_planning", "agent", "about_codebase", "codebase_path", "instruction"],
}


def _interpret(ctx) -> dict:
    if not ctx.token:
        # Signed out: no tools at all, so a plain conversational reply.
        return {
            "goal": ctx.user_request,
            "needs_planning": False,
            "agent": "reader",
            "instruction": ctx.user_request,
            "about_codebase": False,
            "codebase_path": "",
        }
    step_id = ctx.start_step("orchestrator", ctx.user_request)
    try:
        raw = llm.complete_json(
            [
                {"role": "system", "content": INTERPRET_PROMPT},
                *ctx.context_messages,
                {"role": "user", "content": ctx.user_request},
            ],
            schema=INTERPRET_SCHEMA,
        )
    except (llm.LLMError, ValueError):
        raw = {}
    changes = bool(raw.get("changes_github"))
    decision = {
        "goal": str(raw.get("goal") or ctx.user_request).strip(),
        # Nothing to change on GitHub means nothing for the Writer or a
        # plan of changes: a read-only request always goes to the Reader,
        # whatever agent the model picked.
        "needs_planning": bool(raw.get("needs_planning")) and changes,
        "agent": raw.get("agent") if changes and raw.get("agent") in ("reader", "writer") else "reader",
        "instruction": str(raw.get("instruction") or "").strip() or ctx.user_request,
        "about_codebase": bool(raw.get("about_codebase")),
        "codebase_path": str(raw.get("codebase_path") or "").strip(),
    }
    ctx.finish_step(step_id, "completed", str(decision))
    return decision


# --- entry points ------------------------------------------------------------------


def start(*, message, chat_id, repo, session):
    return runtime.ndjson_stream(_start(message, str(chat_id), repo, session))


def resume_action(*, action_id, approved, session):
    return runtime.ndjson_stream(_resume(action_id, approved, session))


def continue_run(*, run_id, session):
    return runtime.ndjson_stream(_continue(str(run_id), session))


def _start(message, chat_id, repo, session):
    ctx = runtime.new_context(message=message, chat_id=chat_id, repo=repo, session=session)
    yield runtime.agent_event(ctx, "orchestrator", "Orchestrator: reading your request")
    if repo and ctx.token:
        yield runtime.agent_event(ctx, "orchestrator", f"Loading the {repo} index")
    context = runtime.load_context(ctx)
    if context["owner"] == "other":
        yield {"type": "final", "reply": "This chat belongs to a different account.", "error": True}
        return
    if context.get("summary") or context.get("recent"):
        parts = (["summary"] if context.get("summary") else []) + (
            [f"{len(context['recent'])} recent messages"] if context.get("recent") else []
        )
        yield runtime.agent_event(ctx, "memory", f"Accessing memory: conversation {' + '.join(parts)}")

    background.enqueue(run_store.create_run, ctx.run_id, chat_id, ctx.user_id, message, repo)
    background.enqueue(db.add_message, chat_id, ctx.user_id, "user", message, repo=repo, run_id=ctx.run_id)
    yield from runtime.guarded(ctx, _dispatch(ctx))


def _dispatch(ctx):
    yield runtime.agent_event(ctx, "orchestrator", "Orchestrator: working out the goal")
    decision = _interpret(ctx)
    ctx.state["goal"] = decision["goal"]
    yield runtime.agent_event(ctx, "orchestrator", f"Goal: {runtime.short(decision['goal'], 140)}")

    if decision["needs_planning"]:
        yield from _hand_to_planner(ctx)
        return

    if decision["agent"] == "writer":
        ctx.state["mode"] = "writer"
        runtime.checkpoint(ctx, mode="writer")
        yield runtime.agent_event(ctx, "writer", "Orchestrator → Writer agent (one step)")
        step_id = ctx.start_step("writer", decision["instruction"])
        outcome = yield from writer.propose(
            ctx,
            instruction=runtime.scoped(ctx, decision["instruction"]),
            context_messages=ctx.context_messages + ctx.repo_messages,
            observations_text="",
            evidence=[],
        )
        if outcome.proposal is None:
            # Usually missing information (e.g. no commit message) — the
            # Writer's explanation goes back to the user as a question.
            ctx.finish_step(step_id, "failed", outcome.text)
            yield runtime.finish(ctx, outcome.text)
            return
        yield from runtime.await_confirmation(ctx, step_id, outcome, instruction=decision["instruction"])
        return

    ctx.state["mode"] = "reader"
    runtime.checkpoint(ctx, mode="reader")
    yield runtime.agent_event(ctx, "reader", "Orchestrator → Reader agent (one step)")
    step_id = ctx.start_step("reader", decision["instruction"])
    outline_messages = []
    if decision["about_codebase"]:
        outline_messages = yield from _codebase_context(ctx, step_id, decision["codebase_path"])
    result = yield from reader.run(
        ctx,
        step_id=step_id,
        instruction=runtime.scoped(ctx, decision["instruction"]),
        context_messages=ctx.context_messages + ctx.repo_messages + outline_messages,
        audience="user",
    )
    ctx.finish_step(step_id, "completed", result.text)
    if prompts.ESCALATE_TOKEN in (result.text or "")[:200] and ctx.token:
        # The Reader found this needs a change after all.
        yield runtime.agent_event(ctx, "reader", "Reader agent: this needs a change — back to the orchestrator")
        yield from _hand_to_planner(ctx)
        return
    yield runtime.finish(ctx, result.text)


def _codebase_context(ctx, step_id, path):
    """For questions about the code as a whole, give the Reader the codebase
    outline up front — every file's functions and classes with signatures
    and docstring first lines — so it reasons from the real code rather than
    only the README, and reads function bodies only where it must."""
    owner, name = ctx.owner_repo
    if not (ctx.token and owner):
        return []
    arguments = {"owner": owner, "repo": name, "path": path}
    label = toolsets.step_label("get_codebase_outline", arguments)
    yield runtime.tool_event(ctx, "reader", "get_codebase_outline", label)
    started = time.monotonic()
    try:
        outline = codebase.get_codebase_outline(ctx.token, owner, name, path=path)
        success = True
    except Exception as exc:
        if is_auth_failure(exc):
            raise GitHubAuthError(str(exc)) from exc
        outline, success = {"error": str(exc)}, False
    ctx.state["tool_calls_used"] = ctx.tool_calls_used + 1
    ctx.record_tool_call(step_id, "get_codebase_outline", arguments, outline, success, int((time.monotonic() - started) * 1000))
    if not success:
        return []
    return [
        {
            "role": "system",
            "content": (
                "Codebase outline, already fetched for you (every source file's classes and "
                "functions with signatures, line numbers, and docstring/comment first lines). "
                "Base your answer on this real code, together with the README: infer what each "
                "file and function does from its name, signature, and doc. Read a function body "
                "(get_function_source) only where this can't tell you what you need. If it says "
                "too_large, outline the relevant directories with get_codebase_outline(path=...).\n"
                + json.dumps(outline, default=str)
            ),
        }
    ]


def _hand_to_planner(ctx):
    ctx.state["mode"] = "planner"
    runtime.checkpoint(ctx, mode="planner")
    yield runtime.agent_event(ctx, "planner", "Orchestrator → Planning agent (needs a plan)")
    yield from planner.run(ctx)


# --- resuming after a confirmation, or a Continue ----------------------------------------


def _resume(action_id, approved, session):
    if not session:
        yield {"type": "final", "reply": "You're not signed in.", "error": True}
        return
    action = run_store.claim_pending_action(action_id, session["token_hash"], "executing" if approved else "cancelled")
    if not action:
        yield {"type": "final", "reply": "This action has expired or was already handled.", "error": True}
        return
    run = run_store.get_run(action["run_id"], session["user_id"]) if action["run_id"] else None
    if not run:
        run_store.finish_pending_action(action_id, "cancelled", {"error": "created before the agent update"})
        yield {
            "type": "final",
            "reply": "That proposal was made before Git Show was updated and can't be resumed. Please ask again.",
            "error": True,
        }
        return

    ctx = runtime.restore_context(run, session)
    pending = ctx.state.pop("pending", None) or {}
    runtime.load_context(ctx)
    background.enqueue(run_store.update_run, ctx.run_id, status="running")

    if run["mode"] == "planner":
        yield from runtime.guarded(ctx, planner.resume(ctx, action_id, action, pending, approved))
    else:
        yield from runtime.guarded(ctx, _resume_single_write(ctx, action_id, action, pending, approved))


def _resume_single_write(ctx, action_id, action, pending, approved):
    if not approved:
        runtime.record_cancel(ctx, action, pending)
        yield runtime.agent_event(ctx, "orchestrator", "You cancelled the change")
        yield runtime.finish(ctx, "Okay, I didn't make that change.")
        return
    applied = yield from runtime.apply_confirmed_write(ctx, action_id, action, pending)
    yield runtime.finish(ctx, applied["message"], status="completed" if applied["status"] == "completed" else "failed")


def _continue(run_id, session):
    if not session:
        yield {"type": "final", "reply": "You're not signed in.", "error": True}
        return
    run = run_store.claim_run_for_continue(run_id, session["user_id"])
    if not run:
        yield {"type": "final", "reply": "This run can't be continued (it already finished or was resumed).", "error": True}
        return
    ctx = runtime.restore_context(run, session)
    background.enqueue(db.clear_continue, run_id)
    runtime.load_context(ctx)
    yield from runtime.guarded(ctx, planner.continue_run(ctx))
