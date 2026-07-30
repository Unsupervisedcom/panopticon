"""Pure, LLM-free transcript gap-analysis: where an agent's task time went.

See :mod:`panopticon.profiler.parse` for the algorithm and :mod:`panopticon.profiler.categories`
for the (single-place, easily-extended) tool category table.
"""

from __future__ import annotations

from panopticon.profiler.parse import profile_transcripts

__all__ = ["profile_transcripts"]
