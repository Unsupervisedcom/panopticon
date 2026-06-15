"""In-container code: the task-service client and the entrypoint protocol.

This is the *only* package permitted to call an LLM (the agent runs here) — the
determinism invariant exempts it. The entrypoint (``python -m panopticon.container``) runs the
real connect/register/slug/heartbeat protocol; the agent step is still a stay-alive
placeholder (no LLM yet), wired up in a later slice.
"""
