"""Reader agent: read-only GitHub investigation. It is never shown a write
tool. Answers the user directly for read-only questions, or reports
findings back to the orchestrator for a plan step. Also verifies the real
repository state after a confirmed write."""

import github_tools
from agents import prompts, toolsets
from agents.common import run_tool_agent

SYSTEM_PROMPT = (
    "You are Git Show's Reader agent. You investigate GitHub repositories — code, history, "
    "commits, diffs, issues, pull requests, users — using read-only tools. You cannot change "
    "anything.\n\n"
    f"Rules:\n{prompts.GROUNDING}\n{prompts.READING}\n{prompts.ESCALATION}\n"
    "- Never make the same call twice; reuse earlier results."
)

FOR_USER = (
    "\n\nYou are answering the user directly. Stop exploring as soon as you can answer. "
    + prompts.STYLE
)

FOR_PLANNER = (
    "\n\nYou are carrying out one step of a larger plan; your output goes back to the "
    "Planning agent, not to the user. Do only what the step asks, then report:\n"
    "- what you found, with exact file paths and line numbers;\n"
    "- if the plan may later change some code, quote that exact current code verbatim "
    "(including indentation) — the Writer can only edit text it is shown;\n"
    "- anything surprising that should change the plan (e.g. the logic lives somewhere "
    "unexpected, a file doesn't exist).\n"
    "Be factual and compact; no pleasantries."
)


SIGNED_OUT = (
    "\n\nThe user is not signed in to GitHub, so you have no tools. Reply conversationally; if "
    "they ask about a real repository or their account, tell them to sign in with the button in "
    "the top right."
)


def run(ctx, *, step_id, instruction, context_messages, audience):
    system = SYSTEM_PROMPT + (FOR_USER if audience == "user" else FOR_PLANNER)
    if not ctx.token:
        system += SIGNED_OUT
    return (
        yield from run_tool_agent(
            ctx,
            agent="reader",
            step_id=step_id,
            system_prompt=system,
            context_messages=context_messages,
            instruction=instruction,
            allowed_tools=toolsets.READER_TOOLS,
        )
    )


# --- post-write verification -------------------------------------------------


def _file_state(token, owner, repo, path, branch):
    try:
        _text, sha = github_tools._fetch_file(token, owner, repo, path, ref=branch)
        return sha
    except Exception as exc:
        response = getattr(exc, "response", None)
        if response is not None and response.status_code == 404:
            return None
        raise


def verify_write(token, tool, args, result) -> tuple[bool, str]:
    """Check GitHub's actual state matches what a confirmed write intended.
    Deterministic reads — no model involved. Returns (passed, detail)."""
    owner, repo = args.get("owner"), args.get("repo")
    branch = args.get("branch")
    try:
        if tool in ("edit_file", "create_or_update_file", "restore_file"):
            sha = _file_state(token, owner, repo, args["path"], branch)
            # For a restore, identical bytes give the identical blob sha as
            # the old version, so this proves an exact copy.
            expected = (result or {}).get("expected_sha") or (result or {}).get("content_sha")
            if sha is None:
                return False, f"{args['path']} does not exist on {branch or 'the default branch'} after the write."
            if expected and sha != expected:
                return False, f"{args['path']} has blob {sha[:10]}, expected {expected[:10]} — it changed again or the write didn't land."
            return True, f"{args['path']} on {branch or 'the default branch'} now has the committed content (blob {sha[:10]})."
        if tool == "delete_file":
            sha = _file_state(token, owner, repo, args["path"], branch)
            if sha is not None:
                return False, f"{args['path']} still exists."
            return True, f"{args['path']} no longer exists."
        if tool == "create_branch":
            ref = github_tools._get(token, f"/repos/{owner}/{repo}/git/ref/heads/{args['branch']}").json()
            return True, f"Branch {args['branch']} exists at {ref['object']['sha'][:10]}."
        if tool == "delete_branch":
            branches = {b["name"] for b in github_tools.list_branches(token, owner, repo)}
            if args["branch"] in branches:
                return False, f"Branch {args['branch']} still exists."
            return True, f"Branch {args['branch']} is gone."
        if tool == "create_pull_request":
            number = (result or {}).get("number")
            if not number:
                return False, "GitHub didn't return a pull request number."
            pr = github_tools.get_pull_request(token, owner, repo, number)
            return True, f"Pull request #{number} exists ({pr['state']}): {pr['head']} -> {pr['base']}."
        if tool == "merge_pull_request":
            pr = github_tools._get(token, f"/repos/{owner}/{repo}/pulls/{args['pull_number']}").json()
            if not pr.get("merged"):
                return False, f"Pull request #{args['pull_number']} is not merged."
            return True, f"Pull request #{args['pull_number']} is merged."
        if tool in ("create_issue", "update_issue"):
            number = args.get("issue_number") or (result or {}).get("number")
            if number:
                issue = github_tools.get_issue(token, owner, repo, number)
                expected_state = args.get("state")
                if expected_state and issue.get("state") != expected_state:
                    return False, f"Issue #{number} is {issue.get('state')}, expected {expected_state}."
                return True, f"Issue #{number} exists and is {issue.get('state')}."
    except Exception as exc:
        return False, f"Couldn't verify: {exc}"
    return True, "GitHub reported success; no further check applies to this action."
