"""Planning agent: handles goals that need more than one step.

Once the orchestrator hands it a goal, the Planning agent owns the run: it
writes a plan, calls the Reader and Writer agents for each step, and every
step's output is reported straight back to it. If an output is what the
step expected, it moves on to the next step; if not (or it changes what
the plan should be), it revises the plan and carries on with the new one.

    plan -> Reader/Writer -> output -> as expected? -> next step
                                         \\-> no -> revise plan -> ...

A Writer step pauses the run until the user confirms; resume() picks it up
from there."""

import json
import re

import llm
import runtime
from agents import prompts, reader, writer

AGENTS = ("reader", "writer")

# The same step (agent + instruction) planned this many times means the plan
# is going in circles; the planner is then told to wrap up.
REPEAT_STEP_LIMIT = 3

PLAN_PROMPT = (
    "You are Git Show's Planning agent. You turn a user's goal into a short plan of steps, and "
    "revise it when a step's output isn't what was expected. Each step is carried out by one "
    "agent:\n"
    "- reader: read-only investigation (find files in the repository index, read outlines, "
    "functions, line ranges, commits, diffs, issues, pull requests, the user's saved chat "
    "memory). Its output includes exact code it quotes.\n"
    "- writer: makes exactly ONE GitHub change (edit/create/delete a file, create a branch, open "
    "a PR or issue, comment, merge, ...). The user confirms it before it runs, and GitHub is "
    "re-checked afterwards. The writer cannot read files: an earlier reader step must have "
    "quoted the exact current code it will change.\n"
    "Steps are plain-language instructions for those agents — never tool names or tool "
    "arguments.\n\n"
    "Rules:\n"
    "- The reader and writer cannot talk to the user. Never plan a step to ask, prompt, or "
    "wait for the user. If something truly only the user can decide is missing, set done=true "
    "and ask in final_answer instead.\n"
    "- When the user leaves the details to you ('make it look good', 'improve it', 'clean it "
    "up', 'use the existing one and change it'), don't ask for content: have the reader read "
    "what exists, then have the writer improve it with good judgment. The user reviews the "
    "exact diff before anything is committed, so that is where they give feedback.\n"
    "- The user's GitHub profile page is just README.md in the repository named after them "
    "(planning input: profile_readme). Improving 'my profile' means reading and editing that "
    "file — a normal edit the writer can make, never something you lack access to.\n"
    "- Do exactly what the user asked, nothing more. Never write code, create files, or make "
    "changes they didn't ask for.\n"
    "- Read requests in git/GitHub terms: 'create a branch legend' means a git branch named "
    "legend, not code about branches.\n"
    "- Only add reader steps the plan actually needs. Editing an existing file needs a reader "
    "step first to quote the exact code; creating a branch, opening an issue, or commenting "
    "does not.\n"
    "- A writer step is complete in itself: editing or creating a file commits it (with its "
    "commit message), so never add a separate 'commit' or 'push' step.\n"
    "- Give every step an 'expected' outcome: what its output should show if it went right. "
    "That is what its output will be checked against.\n"
    "- Instructions must be self-contained: every reader and writer instruction names the exact "
    "repository (owner/name) and file path it's about, plus any function, branch, names, or "
    "messages. Never leave the repository implicit.\n"
    "- When revising, keep finished steps as done, keep ids stable, give new steps new ids, and "
    "don't repeat a step whose output you already have.\n"
    "- If the user cancelled a proposed change, don't propose it (or a variant) again.\n"
    "- If you need something only the user can decide (a branch name, commit message, which of "
    "several options), set done=true and ask in final_answer.\n"
    "- When the goal is achieved, set done=true and write final_answer for the user: what was "
    "found or changed, with paths and links. "
    + prompts.STYLE
)

PLAN_SCHEMA = {
    "title": "plan",
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "reasoning": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "agent": {"type": "string", "enum": list(AGENTS)},
                    "instruction": {"type": "string"},
                    "expected": {"type": "string"},
                    "status": {"type": "string", "enum": ["pending", "done", "skipped"]},
                },
                "required": ["id", "agent", "instruction", "expected", "status"],
            },
        },
        "done": {"type": "boolean"},
        "final_answer": {"type": "string"},
    },
    "required": ["goal", "reasoning", "steps", "done", "final_answer"],
}

REVIEW_PROMPT = (
    "You are Git Show's Planning agent, checking the output of one step of your plan. Compare "
    "it with what the step was expected to produce. Reply with ONLY a JSON object:\n"
    '{"as_expected": true | false, "plan_still_valid": true | false, "reason": "one sentence"}\n'
    "- as_expected: the step did what it was asked and the output is trustworthy and sufficient.\n"
    "- plan_still_valid: the remaining steps still make sense given this output (false if, e.g., "
    "the code lives somewhere unexpected, a file doesn't exist, or more work turned up)."
)

REVIEW_SCHEMA = {
    "title": "review",
    "type": "object",
    "properties": {
        "as_expected": {"type": "boolean"},
        "plan_still_valid": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["as_expected", "plan_still_valid", "reason"],
}


# --- model calls -------------------------------------------------------------------


def format_observations(observations):
    if not observations:
        return "(no steps run yet)"
    lines = []
    for o in observations:
        header = f"[step {o.get('plan_step_id')}] {o['agent']}: {o['instruction']}"
        verdict = f"\nPlanner's check: {o['verdict']}" if o.get("verdict") else ""
        lines.append(f"{header}\nStatus: {o['status']}{verdict}\nOutput:\n{o['result']}")
    return "\n\n".join(lines)


ASKS_USER = re.compile(
    r"\b(ask|prompt|request|wait for|waiting for|confirm with|check with)\b[^.]*\b(the )?user\b"
    r"|\buser('s)? (response|reply|input|answer|details|preferences)\b",
    re.IGNORECASE,
)


def _validate_plan(raw):
    steps = raw.get("steps")
    if not isinstance(steps, list):
        raise ValueError("steps must be a list")
    for s in steps:
        if isinstance(s, dict) and s.get("status", "pending") == "pending" and ASKS_USER.search(str(s.get("instruction", ""))):
            raise ValueError(
                "a step asks or waits for the user, but the reader and writer can't talk to the "
                "user. Either do the work with good judgment (read what exists, then improve it), "
                "or set done=true and ask the user in final_answer"
            )
    for s in steps:
        if not isinstance(s, dict) or str(s.get("agent", "")).lower() not in AGENTS or not str(s.get("instruction", "")).strip():
            raise ValueError('every step needs "agent" ("reader" or "writer") and a plain-language "instruction"')
    if not raw.get("done") and not any(s.get("status", "pending") == "pending" for s in steps):
        raise ValueError("a plan that isn't done needs at least one pending step")
    if raw.get("done") and not str(raw.get("final_answer") or "").strip():
        raise ValueError("a finished plan needs a final_answer for the user")


def _normalize_plan(raw, previous):
    steps = []
    for i, s in enumerate(raw.get("steps") or [], start=1):
        if not isinstance(s, dict) or str(s.get("agent", "")).lower() not in AGENTS:
            continue
        try:
            step_id = int(s.get("id", i))
        except (TypeError, ValueError):
            step_id = i
        steps.append(
            {
                "id": step_id,
                "agent": str(s["agent"]).lower(),
                "instruction": str(s.get("instruction", "")).strip(),
                "expected": str(s.get("expected", "")).strip(),
                "status": s.get("status") if s.get("status") in ("pending", "done", "skipped") else "pending",
            }
        )
    return {
        "goal": str(raw.get("goal") or (previous or {}).get("goal") or "").strip(),
        "reasoning": str(raw.get("reasoning") or "").strip(),
        "steps": steps,
        "done": bool(raw.get("done")) or not any(s["status"] == "pending" for s in steps),
        "final_answer": llm.strip_emoji(str(raw.get("final_answer") or "")).strip(),
    }


def make_plan(ctx, *, why=None, must_finish=None) -> dict:
    """Create the plan, or revise it (why says what prompted the revision)."""
    state = ctx.state
    request = {
        "goal": state.get("goal") or ctx.user_request,
        "user_request": ctx.user_request,
        "selected_repository": ctx.repo,
        "signed_in_user": ctx.user_login,
        "profile_readme": f"{ctx.user_login}/{ctx.user_login} README.md" if ctx.user_login else None,
        "current_plan": state.get("plan") and {k: state["plan"][k] for k in ("goal", "steps")},
    }
    parts = [
        f"Planning input:\n{json.dumps(request, indent=2)}",
        f"Step outputs so far:\n{format_observations(state.get('observations', []))}",
    ]
    if why:
        parts.append(f"Why you're revising the plan: {why}")
    if must_finish:
        parts.append(
            f"{must_finish} You must now finish: set done=true, propose no further steps, and write "
            "the best final_answer you can from the outputs above, saying plainly what was and "
            "wasn't done (and, if useful, asking what the user would like instead)."
        )
    messages = [
        {"role": "system", "content": PLAN_PROMPT},
        *ctx.context_messages,
        *ctx.repo_messages,
        {"role": "user", "content": "\n\n".join(parts)},
    ]
    ran = {o.get("plan_step_id") for o in state.get("observations", [])}

    def validate(value):
        _validate_plan(value)
        phantom = [
            s for s in value.get("steps", [])
            if isinstance(s, dict) and s.get("status") == "done" and s.get("id") not in ran
        ]
        if phantom:
            raise ValueError(
                f"step {phantom[0].get('id')} is marked done but never ran. Mark steps done only "
                "after their output came back; leave new steps pending"
            )

    raw = llm.complete_json(messages, schema=PLAN_SCHEMA, validate=None if must_finish else validate)
    plan = _normalize_plan(raw, state.get("plan"))
    if must_finish:
        plan["done"] = True
    _enforce_statuses(state, plan)
    return plan


def review_step(ctx, step, observation) -> dict:
    messages = [
        {"role": "system", "content": REVIEW_PROMPT},
        {
            "role": "user",
            "content": (
                f"Goal: {ctx.state.get('goal') or ctx.user_request}\n"
                f"Remaining plan: {json.dumps([s for s in ctx.state['plan']['steps'] if s['status'] == 'pending'])}\n\n"
                f"Step {step['id']} ({step['agent']}): {step['instruction']}\n"
                f"Expected: {step.get('expected') or '(not stated)'}\n\n"
                f"Output:\n{observation['result']}"
            ),
        },
    ]
    try:
        raw = llm.complete_json(messages, schema=REVIEW_SCHEMA)
    except (llm.LLMError, ValueError):
        raw = {"as_expected": True, "plan_still_valid": True, "reason": "Couldn't review; accepting."}
    return {
        "as_expected": bool(raw.get("as_expected")),
        "plan_still_valid": bool(raw.get("plan_still_valid", True)),
        "reason": str(raw.get("reason") or "").strip(),
    }


def _enforce_statuses(state, plan):
    """A step's status follows what actually happened, not just what the
    model wrote: done only if its output was as expected, skipped if the
    user cancelled it."""
    latest = {}
    for o in state.get("observations", []):
        latest[o.get("plan_step_id")] = o
    for step in plan["steps"]:
        o = latest.get(step["id"])
        if not o:
            if step["status"] == "done":
                step["status"] = "pending"  # nothing ran, so it can't be done
            continue
        if o["status"] == "cancelled_by_user":
            step["status"] = "skipped"
        elif o.get("as_expected") and o["status"] == "completed":
            step["status"] = "done"
        elif step["status"] == "done":
            step["status"] = "pending"


# --- the planning loop ---------------------------------------------------------------


def _adopt(ctx, plan, label):
    """Record a new plan version and show it."""
    from db import background
    from db import runs as run_store

    state = ctx.state
    state["plan_version"] = state.get("plan_version", 0) + 1
    state["plan"] = plan
    background.enqueue(
        run_store.save_plan, ctx.run_id, state["plan_version"], plan["goal"], plan["reasoning"], plan["steps"], plan["done"]
    )
    runtime.checkpoint(ctx)
    yield runtime.agent_event(ctx, "planner", label)
    yield {"type": "plan", "plan": runtime.plan_snapshot(ctx, active_id=_next_step(plan) and _next_step(plan)["id"])}


def _next_step(plan):
    return next((s for s in plan["steps"] if s["status"] == "pending"), None)


def _plan_and_adopt(ctx, *, why=None, must_finish=None, label):
    step_id = ctx.start_step("planner", why or must_finish or "create plan")
    plan = make_plan(ctx, why=why, must_finish=must_finish)
    ctx.finish_step(step_id, "completed", json.dumps({"done": plan["done"], "steps": len(plan["steps"])}))
    yield from _adopt(ctx, plan, label)


def run(ctx):
    """Entry point from the orchestrator: plan the goal, then execute."""
    yield from _plan_and_adopt(ctx, label="Planning agent: plan ready")
    yield from _execute(ctx)


def continue_run(ctx):
    yield runtime.agent_event(ctx, "planner", "Planning agent: continuing where I left off")
    yield from _execute(ctx)


def _execute(ctx):
    state = ctx.state
    while True:
        if ctx.budget_exhausted():
            yield from _pause_for_budget(ctx)
            return
        if state.get("force_finish"):
            reason = state.pop("force_finish")
            yield from _plan_and_adopt(ctx, must_finish=reason, label="Planning agent: wrapping up")
            yield runtime.finish(ctx, state["plan"]["final_answer"] or _fallback_answer(ctx))
            return

        plan = state["plan"]
        if plan["done"]:
            yield runtime.finish(ctx, plan["final_answer"] or _fallback_answer(ctx))
            return

        step = _next_step(plan)
        if step is None:
            yield from _plan_and_adopt(
                ctx,
                why=(
                    "Every planned step is done. If the goal is met, set done=true with the "
                    "final_answer; otherwise add the steps still needed."
                ),
                label="Planning agent: all steps done — checking the goal is met",
            )
            continue

        key = f"{step['agent']}:{' '.join(step['instruction'].lower().split())}"
        counts = state.setdefault("step_counts", {})
        counts[key] = counts.get(key, 0) + 1
        if counts[key] >= REPEAT_STEP_LIMIT:
            state["force_finish"] = "The same step keeps coming back without progress."
            continue

        position = plan["steps"].index(step) + 1
        progress = f"step {position}/{len(plan['steps'])}"

        if step["agent"] == "reader":
            yield runtime.agent_event(ctx, "reader", f"Planning agent → Reader agent · {progress}: {runtime.short(step['instruction'])}")
            step_id = ctx.start_step("reader", step["instruction"], plan_step_id=step["id"])
            result = yield from reader.run(
                ctx,
                step_id=step_id,
                instruction=runtime.scoped(ctx, step["instruction"]),
                context_messages=ctx.context_messages
                + ctx.repo_messages
                + [{"role": "system", "content": "Outputs of the plan's earlier steps:\n" + format_observations(state["observations"])}],
                audience="planner",
            )
            ctx.finish_step(step_id, "completed", result.text)
            state["evidence"].extend(result.evidence)
            observation = _observe(ctx, step, "completed", result.text or "(the reader returned nothing)")
            yield runtime.agent_event(ctx, "reader", "Reader agent → Planning agent: output reported")
            yield from _check(ctx, step, observation)
            continue

        # Writer step: propose one change, then pause for the user's confirmation.
        yield runtime.agent_event(ctx, "writer", f"Planning agent → Writer agent · {progress}: {runtime.short(step['instruction'])}")
        step_id = ctx.start_step("writer", step["instruction"], plan_step_id=step["id"])
        outcome = yield from writer.propose(
            ctx,
            instruction=runtime.scoped(ctx, step["instruction"]),
            context_messages=ctx.context_messages + ctx.repo_messages,
            observations_text=format_observations(state["observations"]),
            evidence=state["evidence"],
        )
        if outcome.proposal is None:
            ctx.finish_step(step_id, "failed", outcome.text)
            observation = _observe(ctx, step, "no_change_proposed", outcome.text)
            yield runtime.agent_event(ctx, "writer", "Writer agent → Planning agent: couldn't propose a change")
            yield from _check(ctx, step, observation)
            continue
        yield from runtime.await_confirmation(
            ctx, step_id, outcome, plan_step_id=step["id"], instruction=step["instruction"]
        )
        return


def _observe(ctx, step, status, result):
    observation = {
        "plan_step_id": step["id"],
        "agent": step["agent"],
        "instruction": step["instruction"],
        "status": status,
        "result": result,
    }
    ctx.state["observations"].append(observation)
    runtime.checkpoint(ctx)
    return observation


def _check(ctx, step, observation):
    """The Planning agent checks a step's output against what it expected:
    as expected -> next step; otherwise -> revise the plan."""
    step_id = ctx.start_step("planner", f"check step {step['id']}", plan_step_id=step["id"])
    verdict = review_step(ctx, step, observation)
    ctx.finish_step(step_id, "completed", json.dumps(verdict))
    yield from _apply_verdict(ctx, step, observation, verdict)


def _apply_verdict(ctx, step, observation, verdict):
    observation["as_expected"] = verdict["as_expected"] and observation["status"] == "completed"
    observation["verdict"] = ("as expected" if observation["as_expected"] else "not as expected") + (
        f" — {verdict['reason']}" if verdict.get("reason") else ""
    )
    position = f"step {ctx.state['plan']['steps'].index(step) + 1}" if step in ctx.state["plan"]["steps"] else "the step"
    if observation["as_expected"] and verdict["plan_still_valid"]:
        step["status"] = "done"
        runtime.checkpoint(ctx)
        yield runtime.agent_event(ctx, "planner", f"Planning agent: {position} output as expected — continuing")
        yield {"type": "plan", "plan": runtime.plan_snapshot(ctx, active_id=_next_step(ctx.state["plan"]) and _next_step(ctx.state["plan"])["id"])}
        return
    why = (
        f"Step {step['id']}'s output {'was as expected but changes what the rest of the plan should be' if observation['as_expected'] else 'was not as expected'}: "
        f"{verdict['reason']}"
    )
    yield runtime.agent_event(
        ctx, "planner", f"Planning agent: {position} output {'changes the plan' if observation['as_expected'] else 'not as expected'} — {runtime.short(verdict['reason'], 110)}"
    )
    yield from _plan_and_adopt(ctx, why=why, label="Planning agent: plan revised")


def resume(ctx, action_id, action, pending, approved):
    """Pick the plan back up after the user confirmed or cancelled a
    Writer proposal. The Writer's output goes straight back to the
    Planning agent like any other step's."""
    plan = ctx.state["plan"]
    step = next((s for s in plan["steps"] if s["id"] == pending.get("plan_step_id")), None) or {
        "id": pending.get("plan_step_id"),
        "agent": "writer",
        "instruction": pending.get("instruction") or action["tool"],
    }

    if not approved:
        runtime.record_cancel(ctx, action, pending)
        _observe(ctx, step, "cancelled_by_user", f"The user cancelled the proposed {action['tool']} ({runtime.short(json.dumps(action['arguments']), 200)}).")
        ctx.state["force_finish"] = "The user cancelled the proposed change, so don't propose it (or a variant of it) again."
        yield runtime.agent_event(ctx, "planner", "You cancelled the change — Planning agent is wrapping up")
        yield from _execute(ctx)
        return

    applied = yield from runtime.apply_confirmed_write(ctx, action_id, action, pending)
    observation = _observe(ctx, step, applied["status"], applied["observation"])
    yield runtime.agent_event(ctx, "writer", "Writer agent → Planning agent: output reported")
    if applied["status"] == "completed":
        # GitHub itself confirmed the change landed, so no model is needed
        # to judge it; anything else goes through the normal check.
        yield from _apply_verdict(
            ctx, step, observation, {"as_expected": True, "plan_still_valid": True, "reason": "confirmed on GitHub"}
        )
    else:
        yield from _check(ctx, step, observation)
    yield from _execute(ctx)


def _fallback_answer(ctx):
    results = [o for o in ctx.state.get("observations", []) if o.get("result")]
    if results and results[-1].get("status") == "cancelled_by_user":
        return "Okay, I didn't make that change. Let me know if you'd like something different instead."
    if not results:
        return "I couldn't make progress on that. Could you rephrase or give more detail?"
    return f"I wasn't able to finish everything. Here's the last thing I found:\n\n{results[-1]['result']}"


def _pause_for_budget(ctx):
    used = ctx.tool_calls_used
    steps = (ctx.state.get("plan") or {}).get("steps", [])
    done = [s for s in steps if s["status"] == "done"]
    yield runtime.agent_event(ctx, "planner", f"Planning agent: paused after {used} tool calls")
    yield runtime.finish(
        ctx,
        f"I've made {used} tool calls on this so far and completed {len(done)} of {len(steps)} planned "
        "steps. I've paused here so it doesn't run on unchecked — press **Continue** to keep going.",
        can_continue=True,
        status="paused",
    )
