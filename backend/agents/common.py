"""Shared machinery for the agents: the per-run context (budget, loop
detection, trace), and the tool-calling loop used by the Reader agent.

Agent functions are generators: they yield progress events for the UI and
`return` their result, so the orchestrator or Planning agent drives them with
`result = yield from agent(...)`."""

import json
import time
import uuid
from dataclasses import dataclass, field

import requests

import llm
from agents import toolsets
from db import background
from db import runs as run_store

# There is no fixed cap on rounds or plan revisions. Runs stop when the goal
# is met, when loop detection sees the same call or step repeating, or when
# this many tool calls have been made since the run last started or resumed,
# at which point it pauses and the user can press Continue.
RUN_TOOL_BUDGET = 100

# Identical tool calls (same tool, same arguments) are answered from cache
# after the first; at this count the agent is told to stop and answer.
REPEAT_CALL_LIMIT = 3


class GitHubAuthError(Exception):
    """GitHub rejected the user's token (expired or revoked). Retrying or
    working around it can't help, so it ends the run and asks the user to
    sign in again."""


def is_auth_failure(exc) -> bool:
    response = getattr(exc, "response", None)
    return isinstance(exc, requests.HTTPError) and response is not None and response.status_code == 401


@dataclass
class AgentResult:
    text: str
    evidence: list = field(default_factory=list)
    exhausted: bool = False


@dataclass
class RunContext:
    run_id: str
    conversation_id: str
    user_id: int | None
    token: str | None
    session_hash: str | None
    user_login: str | None
    repo: str | None
    user_request: str
    state: dict
    # Agent hand-offs and tool calls shown above the assistant message being built.
    timeline: list = field(default_factory=list)
    tool_cache: dict = field(default_factory=dict)
    # Conversation memory and repository overview, as system messages.
    context_messages: list = field(default_factory=list)
    repo_messages: list = field(default_factory=list)

    @property
    def owner_repo(self):
        if self.repo and "/" in self.repo:
            owner, _, name = self.repo.partition("/")
            return owner, name
        return None, None

    # --- budget -------------------------------------------------------------

    @property
    def tool_calls_used(self) -> int:
        return self.state.setdefault("tool_calls_used", 0)

    def budget_exhausted(self) -> bool:
        limit = self.state.setdefault("budget_limit", RUN_TOOL_BUDGET)
        return self.tool_calls_used >= limit

    def extend_budget(self):
        self.state["budget_limit"] = self.tool_calls_used + RUN_TOOL_BUDGET

    # --- trace --------------------------------------------------------------

    def start_step(self, agent, instruction, plan_step_id=None) -> str:
        step_id = str(uuid.uuid4())
        self.state["step_counter"] = self.state.get("step_counter", 0) + 1
        background.enqueue(
            run_store.start_step,
            step_id,
            self.run_id,
            self.state["step_counter"],
            agent,
            instruction,
            self.state.get("plan_version"),
            plan_step_id,
        )
        return step_id

    def finish_step(self, step_id, status, result):
        background.enqueue(run_store.finish_step, step_id, status, result)

    def record_tool_call(self, step_id, name, arguments, result, success, duration_ms, tool_call_id=None) -> str:
        tool_call_id = tool_call_id or str(uuid.uuid4())
        background.enqueue(
            run_store.record_tool_call,
            tool_call_id,
            self.run_id,
            step_id,
            name,
            arguments,
            result,
            success,
            duration_ms,
        )
        return tool_call_id


def status_event(label):
    return {"type": "status", "label": label}


def run_tool_agent(ctx: RunContext, *, agent, step_id, system_prompt, context_messages, instruction, allowed_tools):
    """The tool-calling loop for an agent. No round cap: it ends when the
    model answers, when it keeps repeating itself, or when the run's tool
    budget is spent (then it's asked to answer with what it has)."""
    tools = toolsets.schemas(allowed_tools) if (ctx.token and allowed_tools) else None
    messages = [{"role": "system", "content": system_prompt}, *context_messages, {"role": "user", "content": instruction}]
    evidence = []

    def wrap_up(note):
        messages.append({"role": "system", "content": note})
        completion = llm.complete(messages, tools=None)
        return llm.strip_emoji(completion.choices[0].message.content or "")

    while True:
        if tools and ctx.budget_exhausted():
            text = wrap_up(
                "The tool budget for this run is used up. Do not request tools. Answer now "
                "with what you have gathered, and say plainly what you could not get to."
            )
            return AgentResult(text, evidence, exhausted=True)

        completion = llm.complete(messages, tools=tools)
        msg = completion.choices[0].message
        if not msg.tool_calls:
            return AgentResult(llm.strip_emoji(msg.content or ""), evidence)

        messages.append(
            {"role": "assistant", "content": msg.content, "tool_calls": [tc.model_dump() for tc in msg.tool_calls]}
        )
        stuck = False
        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                arguments = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": json.dumps({"error": "Arguments were not valid JSON."})}
                )
                continue

            key = f"{name}:{json.dumps(arguments, sort_keys=True)}"
            counts = ctx.state.setdefault("call_counts", {})
            counts[key] = counts.get(key, 0) + 1
            if key in ctx.tool_cache:
                # The note is aimed at the model: a plain repeated result
                # doesn't signal "stop doing this" the way it does.
                content = (
                    "[Repeated call — you already made this exact call in this run. Do not call "
                    "it again; use the result below.]\n"
                    + toolsets.serialize_result(name, ctx.tool_cache[key])
                )
                if counts[key] >= REPEAT_CALL_LIMIT:
                    stuck = True
            else:
                label = toolsets.step_label(name, arguments)
                ctx.timeline.append({"kind": "tool", "agent": agent, "tool": name, "label": label})
                yield {"type": "step", "agent": agent, "tool": name, "label": label}
                started = time.monotonic()
                try:
                    result = toolsets.run(name, ctx, arguments, allowed_tools)
                    success = not (isinstance(result, dict) and "error" in result)
                except Exception as exc:
                    if is_auth_failure(exc):
                        raise GitHubAuthError(str(exc)) from exc
                    result, success = {"error": str(exc)}, False
                duration_ms = int((time.monotonic() - started) * 1000)
                ctx.state["tool_calls_used"] = ctx.tool_calls_used + 1
                ctx.record_tool_call(step_id, name, arguments, result, success, duration_ms)
                ctx.tool_cache[key] = result
                if success and name in toolsets.CONTENT_TOOLS:
                    evidence.append({"tool": name, "arguments": arguments, "result": result})
                content = toolsets.serialize_result(name, result)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})

        if stuck:
            text = wrap_up(
                "You are repeating the same tool calls without making progress. Stop calling "
                "tools and answer now with what you have."
            )
            return AgentResult(text, evidence)


def conversation_messages(context: dict) -> list:
    """The chat so far, as one system message: the rolling summary plus the
    turns it doesn't cover yet, verbatim."""
    parts = []
    if context.get("summary"):
        parts.append(f"Summary of the conversation so far:\n{context['summary']}")
    if context.get("recent"):
        turns = "\n\n".join(f"{m['role'].upper()}: {m['content']}" for m in context["recent"])
        parts.append(f"Most recent messages (verbatim, oldest first):\n{turns}")
    if not parts:
        return []
    return [{"role": "system", "content": "\n\n".join(parts)}]


def repo_context_message(ctx: RunContext, overview: dict | None) -> list:
    if not ctx.repo:
        return []
    if not overview:
        return [
            {
                "role": "system",
                "content": (
                    f"The user's selected repository is {ctx.repo}. Use it as the default owner/repo "
                    "for tool calls unless they name another. (Its index couldn't be loaded.)"
                ),
            }
        ]
    return [
        {
            "role": "system",
            "content": (
                f"The user's selected repository is {ctx.repo} — use it as the default owner/repo "
                "for tool calls unless they name another.\n"
                f"Index: branch {overview['branch']} at commit {overview['commit_sha'][:12]}, "
                f"{overview['file_count']} entries"
                f"{' (index truncated by GitHub)' if overview['truncated'] else ''}.\n"
                f"Top level: {', '.join(overview['top_level']) or '(empty)'}\n"
                f"Directories (two levels): {', '.join(overview['directories'][:150]) or '(none)'}\n"
                f"File types: {json.dumps(overview['file_types'])}\n"
                "This is only an overview. Use find_files / list_directory to locate files."
            ),
        }
    ]
