"""Built-in workflow definitions.

These live on a path the task service loads via registration (a later slice). The Spike seed
workflow proves the state machine has no hardcoded lifecycle; the Parity workflow reproduces
cloude-cade's lifecycle (PARITY §1) as one configurable workflow among several.
"""

from panopticon.workflows.parity import Parity
from panopticon.workflows.spike import Spike

__all__ = ["Parity", "Spike"]
