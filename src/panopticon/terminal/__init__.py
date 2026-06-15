"""The terminal controller: the operator's CLI + dashboard.

A **presentation adapter** (ADR 0002) and a pure **REST client** of the task service (ADR
0006) — it renders task state and drives operations over HTTP, and switches into a task's tmux
on `t`. LLM-free (the determinism invariant): it never calls a model.
"""
