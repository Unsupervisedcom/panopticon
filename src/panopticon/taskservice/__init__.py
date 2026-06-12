"""The task service: the deterministic control plane.

Owns the store (the sole DB authority, ADR 0006), hosts the workflow registry, and
drives task lifecycle. This package must remain LLM-free (the determinism invariant).
"""
