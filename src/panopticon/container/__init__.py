"""In-container code: the task-service client and the entrypoint protocol.

This is the *only* package permitted to call an LLM (the agent runs here) — the
determinism invariant exempts it. In this slice there is no LLM yet — the entrypoint is a
faithful stub of the connect/register/slug protocol.
"""
