"""Writer agent: GitHub mutations only. It proposes exactly one change —
for the orchestrator (a one-step request) or a plan step (for the Planning
agent). The change is shown to the user, with a diff where it applies, and
only after they confirm does execute() run it."""

import json
from dataclasses import dataclass

import requests

import github_tools
import llm
from agents import prompts, toolsets
from agents.common import GitHubAuthError, is_auth_failure

# Retries when a proposal can't apply (e.g. an edit's old_text doesn't match
# the file). These are attempts at one step, not a cap on the run: the
# planner sees the failure and can re-plan (e.g. have the Reader re-read).
MAX_PROPOSAL_ATTEMPTS = 3

SYSTEM_PROMPT = (
    "You are Git Show's Writer agent. You make exactly one GitHub change per request, using "
    "the write tools you're given. You cannot read the repository yourself: everything you know "
    "about current file contents is in the evidence provided.\n\n"
    "Rules:\n"
    "- Call exactly one tool. Your change will be shown to the user to confirm before it runs.\n"
    "- Only make the change the instruction asks for. Never create files, docs, or anything else "
    "the user didn't request; if the instruction is really a question, call no tool and say so.\n"
    "- To undo, revert, or roll back a file, use restore_file with the commit to restore from; "
    "never retype old content.\n"
    "- Replace an existing file's whole content (create_or_update_file) only with the full "
    "current text in the evidence; otherwise it will be refused.\n"
    "- To change part of an existing file, use edit_file with old_text copied verbatim from the "
    "evidence (exact indentation and whitespace), including enough surrounding lines to be "
    "unique. Use create_or_update_file only for new files or full rewrites you can see whole.\n"
    "- Only a few names are the user's to choose: a new branch name, a new repository name, a "
    "release tag. If one of those is needed and not given, don't call a tool: say exactly what's "
    "missing. Everything else you write yourself — commit messages, PR and issue titles and "
    "text, and the content itself when the user asked you to improve or rewrite something "
    "('make it look good'). Use the existing content as the starting point.\n"
    "- Obvious defaults are fine: the selected repo's owner/name, its default branch as a base.\n"
    "- Write clear, specific commit messages and PR/issue text.\n"
    "- If you call no tool, reply in plain text explaining why."
)


@dataclass
class WriterOutcome:
    proposal: dict | None
    text: str
    # Set when the change needs a file read first (the Writer can't read):
    # the orchestrator hands the request to the Planning agent instead.
    needs_read: str | None = None


def _read_in_full(evidence, owner, repo, path):
    """Whether this run holds a complete copy of owner/repo/path, read by
    the Reader — the only safe basis for replacing its whole content."""
    target = path.lower().lstrip("/")
    for e in evidence or []:
        args, result = e.get("arguments") or {}, e.get("result") or {}
        if (args.get("owner") or "").lower() != owner.lower() or (args.get("repo") or "").lower() != repo.lower():
            continue
        if str(result.get("path") or args.get("path") or "").lower().lstrip("/") != target:
            continue
        if e.get("tool") in ("get_file_contents", "get_readme"):
            if result.get("content") is not None and not result.get("truncated") and not result.get("content_omitted"):
                return True
        if e.get("tool") == "get_file_lines":
            if result.get("start_line") == 1 and result.get("end_line") == result.get("total_lines") and not result.get("truncated"):
                return True
    return False


def _evidence_message(evidence):
    if not evidence:
        return []
    blocks = [
        f"### {e['tool']} {json.dumps(e['arguments'])}\n{json.dumps(e['result'], default=str)}" for e in evidence
    ]
    return [
        {
            "role": "system",
            "content": "Evidence gathered by earlier steps (exact tool results):\n\n" + "\n\n".join(blocks),
        }
    ]


# Simple actions are described from a template; only file writes (whose
# content needs summarizing) cost an extra model call.
_TEMPLATES = {
    "create_branch": lambda a: f"Create branch **{a.get('branch')}** from **{a.get('from_branch') or 'the default branch'}**",
    "delete_branch": lambda a: f"Permanently delete branch **{a.get('branch')}**",
    "restore_file": lambda a: f"Restore `{a.get('path')}` exactly as it was at commit `{str(a.get('ref', ''))[:7]}`",
    "create_pull_request": lambda a: f"Open a pull request **\"{a.get('title')}\"** from **{a.get('head')}** into **{a.get('base')}**",
    "merge_pull_request": lambda a: f"Merge pull request **#{a.get('pull_number')}** ({a.get('merge_method') or 'merge'})",
    "create_issue": lambda a: f"Open an issue titled **\"{a.get('title')}\"**",
    "add_issue_comment": lambda a: f"Comment on **#{a.get('issue_number')}**",
    "create_release": lambda a: f"Publish release **{a.get('name') or a.get('tag_name')}** (tag {a.get('tag_name')})",
    "star_repository": lambda a: "Star the repository",
    "unstar_repository": lambda a: "Unstar the repository",
    "fork_repository": lambda a: "Fork the repository to your account",
}


def describe(tool, arguments, preview) -> str:
    """A short plain-language description of a proposed write for the
    confirmation card, instead of a raw JSON dump."""
    template = _TEMPLATES.get(tool)
    if template:
        where = f" in `{arguments.get('owner')}/{arguments.get('repo')}`" if arguments.get("repo") else ""
        return f"{template(arguments)}{where}. Confirm to go ahead, or cancel."
    args_preview = json.dumps(arguments)
    if len(args_preview) > 4000:
        args_preview = args_preview[:4000] + "... (truncated for this summary)"
    try:
        completion = llm.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "You write short, plain-language confirmations of a pending GitHub action "
                        "for a human review step. Never use emojis or markdown headings."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Write 1-2 short sentences describing what this action will do. If an "
                        "argument holds a large text blob, describe what it changes rather than "
                        "reproducing it. Don't invent details. End with a short prompt to confirm "
                        f"or cancel.\n\nTool: {tool}\nArguments: {args_preview}"
                        + (f"\nChange preview available: {preview.get('path')}" if preview else "")
                    ),
                },
            ]
        )
        text = llm.strip_emoji(completion.choices[0].message.content or "").strip()
        if text:
            return text
    except llm.LLMError:
        pass
    return f"I'd like to run **{tool}**. Review the details below and confirm to proceed."


def _wrong_profile_target(ctx, arguments):
    """A profile-README request must write to the user's profile repository,
    never overwrite another repository's README (e.g. the one selected in the
    picker). Returns an error for the model, or None."""
    login = ctx.user_login
    goal = f"{ctx.user_request} {ctx.state.get('goal', '')}".lower()
    if not login or "profile" not in goal:
        return None
    owner, repo, path = arguments.get("owner"), arguments.get("repo"), (arguments.get("path") or "")
    if not owner or not repo or path.lower().lstrip("/") != "readme.md":
        return None
    if f"{owner}/{repo}".lower() == f"{login}/{login}".lower():
        return None
    return (
        f"This is about the user's GitHub profile, whose README is README.md in {login}/{login} — "
        f"not {owner}/{repo}. Write the change there, and never overwrite {owner}/{repo}'s README."
    )


def _off_target_repo(ctx, arguments):
    """A write aimed at a repository other than the selected one is only
    allowed if the user named that repository themselves. Returns an error
    for the model, or None."""
    owner, repo = arguments.get("owner"), arguments.get("repo")
    if not ctx.repo or not owner or not repo:
        return None
    target = f"{owner}/{repo}".lower()
    if target == ctx.repo.lower():
        return None
    request = f"{ctx.user_request} {ctx.state.get('goal', '')}".lower()
    if target in request or f"/{repo.lower()}" in request or f" {repo.lower()}" in f" {request}":
        return None
    # The user's own profile repository, when the goal is about their profile.
    login = (ctx.user_login or "").lower()
    if login and target == f"{login}/{login}" and ("profile" in request or "readme" in request):
        return None
    return (
        f"This change targets {owner}/{repo}, but the selected repository is {ctx.repo} and the "
        f"user never mentioned {owner}/{repo}. Make the change in {ctx.repo}."
    )


def propose(ctx, *, instruction, context_messages, observations_text, evidence):
    """Generator: returns a WriterOutcome with either a validated proposal
    (tool, arguments, preview, description) or a text explanation."""
    yield {"type": "agent", "agent": "writer", "label": "Writer agent: preparing the change"}
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + "\n\n" + prompts.STYLE},
        *context_messages,
        *_evidence_message(evidence),
        {
            "role": "system",
            "content": "Results of the plan's earlier steps:\n" + (observations_text or "(none yet)"),
        },
        {"role": "user", "content": instruction},
    ]
    tools = toolsets.schemas(toolsets.WRITER_TOOLS)
    last_error = None
    for _attempt in range(MAX_PROPOSAL_ATTEMPTS):
        completion = llm.complete(messages, tools=tools)
        msg = completion.choices[0].message
        if not msg.tool_calls:
            return WriterOutcome(None, llm.strip_emoji(msg.content or "The Writer made no change."))

        tc = msg.tool_calls[0]
        messages.append(
            {"role": "assistant", "content": msg.content, "tool_calls": [tc.model_dump()]}
        )
        name = tc.function.name
        try:
            arguments = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            last_error = "Arguments were not valid JSON."
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps({"error": last_error})})
            continue
        if name not in toolsets.WRITER_TOOLS:
            last_error = f"{name} is not a write tool you can use."
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps({"error": last_error})})
            continue
        wrong_repo = _wrong_profile_target(ctx, arguments) or _off_target_repo(ctx, arguments)
        if wrong_repo:
            last_error = wrong_repo
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps({"error": wrong_repo})})
            continue
        try:
            preview = github_tools.preview_write(ctx.token, name, arguments)
            # Replacing an existing file's whole content without having read
            # all of it would silently drop whatever wasn't seen.
            if (
                name == "create_or_update_file"
                and preview
                and not preview.get("new_file")
                and not _read_in_full(evidence, arguments.get("owner", ""), arguments.get("repo", ""), arguments.get("path", ""))
            ):
                where = f"{arguments.get('owner')}/{arguments.get('repo')}/{arguments.get('path')}"
                return WriterOutcome(
                    None,
                    f"I need to read {where} in full before replacing it (or restore it from history with "
                    "restore_file), so nothing in it gets lost.",
                    needs_read=where,
                )
        except github_tools.WriteBlocked as exc:
            return WriterOutcome(None, f"I didn't propose that change: {exc}")
        except (ValueError, KeyError, requests.HTTPError) as exc:
            if is_auth_failure(exc):
                raise GitHubAuthError(str(exc)) from exc
            last_error = str(exc)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(
                        {"error": f"This change can't be applied: {last_error}. Fix it and call the tool again."}
                    ),
                }
            )
            continue
        description = llm.strip_emoji(msg.content or "") or describe(name, arguments, preview)
        return WriterOutcome({"tool": name, "arguments": arguments, "preview": preview}, description)
    return WriterOutcome(None, f"The Writer couldn't produce a change that applies cleanly. Last error: {last_error}")


def execute(token, tool, arguments):
    """Run a write the user has confirmed."""
    return github_tools.run_tool(tool, token, arguments)
