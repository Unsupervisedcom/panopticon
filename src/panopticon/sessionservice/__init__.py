"""The session service (runner): spawns task containers and owns their tmux sessions.

Realizes the execution-backend boundary (ADR 0006/0008). The real runner is a host process
that spawns containers on the host Docker daemon; this package currently ships only a stub
runner for the walking skeleton (no Docker). Must remain LLM-free (the determinism invariant).
"""
