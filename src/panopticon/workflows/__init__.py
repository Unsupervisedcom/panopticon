"""Built-in workflow definitions.

These live on a path the task service loads via registration (a later slice). The Spike seed
workflow proves the state machine has no hardcoded lifecycle; the GithubPeerReviewed workflow
reproduces cloude-cade's lifecycle (PARITY §1) as one configurable workflow among several.
"""

from panopticon.workflows.github_peer_reviewed import GithubPeerReviewed
from panopticon.workflows.spike import Spike

__all__ = ["GithubPeerReviewed", "Spike"]
