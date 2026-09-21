"""Composing the message delivered to a task's agent for an *ask* (ask-the-author).

Pure and LLM-free — shared by the session service (which delivers the message) and the container
Stop hook (which locates the agent's reply in the transcript by the same marker). Keeping it here,
in ``core``, means the wording + marker have one definition and can be unit-tested without a runner.
"""

from __future__ import annotations

#: Prefix of the marker embedded at the top of every delivered ask. Stable and greppable so the
#: container Stop hook can find the user message that carried *this* ask and collect the assistant
#: reply that follows it (see :func:`ask_marker`).
ASK_MARKER_PREFIX = "panopticon-ask"


def ask_marker(ask_id: str) -> str:
    """The marker for one ask — embedded in the delivered message and matched in the transcript."""
    return f"[[{ASK_MARKER_PREFIX}:{ask_id}]]"


def compose_ask_message(question: str, context: str, *, terminal: bool, ask_id: str) -> str:
    """The full message delivered to the agent for an ask: the marker, a framing, and the question.

    ``terminal`` selects the framing. For a **terminal** task (COMPLETE/DROPPED) the guardrail is
    strict — the agent is answering about merged/proposed work and must not modify anything or touch
    the branch (the memo's requirement). For a task still in flight it's a lighter note that a
    reviewer is asking a question, without forbidding changes (a reviewer's question may legitimately
    prompt a fix mid-review). The marker is always first so the Stop hook can anchor the reply.
    """
    marker = ask_marker(ask_id)
    lines = [marker, ""]
    if terminal:
        lines += [
            "A reviewer is asking a question about your merged or proposed work on this task.",
            "You are ONLY answering a question — this is not a request to change anything.",
            "Do not modify files, run git, create commits, or use workflow tools. Answer from your",
            "session memory and, if needed, read-only inspection of the code.",
        ]
    else:
        lines += [
            "A reviewer looking at this task is asking you a question.",
            "Answer from your knowledge of this work — you need not change anything to reply.",
        ]
    lines += ["", "Reviewer's question:", question.strip()]
    if context.strip():
        lines += ["", "Context from the reviewer:", context.strip()]
    return "\n".join(lines)
