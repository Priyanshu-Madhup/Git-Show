"""Rolling conversation summaries, built from the messages table.

The summary records which message it covers up to (summarized_through).
After every assistant message — normal replies, proposals, confirmed-write
results, errors — a job folds in everything newer. If a job fails or lags,
nothing is lost: the unsummarized messages are handed to the agents
verbatim until a later job catches up."""

import logging
import threading

import db
import llm

log = logging.getLogger("gitshow.summaries")

SUMMARY_CHAR_BUDGET = 16000  # ~4k tokens; hard safety cap

SYSTEM_PROMPT = (
    "You maintain a running summary of an ongoing conversation between a user and Git Show, a "
    "GitHub assistant. You'll get the existing summary (possibly empty) and the new messages "
    "since it was written. Rewrite the summary to fold them in: keep facts, decisions, and "
    "specifics that matter later (repos, branches, files, functions, PR/issue numbers, changes "
    "made or declined, what was asked and answered); drop small talk and anything superseded. "
    "Keep it under about 3500 tokens (~14000 characters) — compress or drop the least relevant "
    "older details rather than just appending. Reply with ONLY the updated summary text: no "
    "preamble, headings, or code fences."
)

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(conversation_id) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(str(conversation_id), threading.Lock())


def update(conversation_id) -> None:
    """Fold every unsummarized message into the summary. Serialized per
    chat, and the save is conditional on nobody having advanced the summary
    meanwhile, so two updates can never overwrite each other's work."""
    with _lock_for(conversation_id):
        for _attempt in range(3):
            summary, through, new_messages = db.get_unsummarized(conversation_id)
            if not new_messages:
                return
            transcript = "\n\n".join(f"{m['role'].upper()}: {m['content']}" for m in new_messages)
            try:
                completion = llm.complete(
                    [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": (
                                "EXISTING SUMMARY:\n"
                                f"{summary or '(empty — this is the start of the conversation)'}\n\n"
                                f"NEW MESSAGES:\n{transcript}"
                            ),
                        },
                    ]
                )
            except llm.LLMError:
                log.warning("Summary update failed for %s; will retry after the next message", conversation_id)
                return
            new_summary = (completion.choices[0].message.content or "").strip()[:SUMMARY_CHAR_BUDGET]
            if not new_summary:
                return
            if db.save_summary(conversation_id, new_summary, through, new_messages[-1]["id"]):
                return


def schedule(conversation_id) -> None:
    """Queue a summary update behind any pending message writes (so it sees
    them), then run it on its own thread so the model call doesn't hold up
    other database work."""
    from db import background

    background.enqueue(
        lambda: threading.Thread(target=_safe_update, args=(conversation_id,), daemon=True).start()
    )


def _safe_update(conversation_id):
    try:
        update(conversation_id)
    except Exception:
        log.exception("Summary update crashed for %s", conversation_id)
