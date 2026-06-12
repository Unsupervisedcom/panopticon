"""Built-in workflow definitions.

These live on a path the task service loads via registration (a later slice). For now the
Spike seed workflow proves the state machine has no hardcoded lifecycle.
"""

from panopticon.workflows.spike import Spike

__all__ = ["Spike"]
