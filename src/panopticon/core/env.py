"""Derived path constants for all panopticon sub-directories.

All paths are expressed relative to three base directories so setting one
variable moves its entire subtree without per-path overrides:

  PANOPTICON_DATA   → $XDG_DATA_HOME/panopticon   → ~/.local/share/panopticon
  PANOPTICON_CACHE  → $XDG_CACHE_HOME/panopticon  → ~/.cache/panopticon
  PANOPTICON_CONFIG → $XDG_CONFIG_HOME/panopticon → ~/.config/panopticon
"""
from __future__ import annotations

from panopticon.core.dirs import user_cache_dir, user_config_dir, user_data_dir

#: SQLite DB URL. PANOPTICON_DB overrides to any SQLAlchemy URL (e.g. postgresql://).
DEFAULT_DB: str = "sqlite:///" + str(user_data_dir() / "panopticon.db")

#: Task artifact store — $PANOPTICON_DATA/artifacts
DEFAULT_ARTIFACTS: str = str(user_data_dir() / "artifacts")

#: Per-task workspace clones — $PANOPTICON_DATA/tasks
DEFAULT_TASKS: str = str(user_data_dir() / "tasks")

#: Per-repo clone cache — $PANOPTICON_CACHE/repos
DEFAULT_CLONE_CACHE: str = str(user_cache_dir() / "repos")

#: Operator-authored Dockerfile layer files — $PANOPTICON_CONFIG/layers
DEFAULT_LAYERS: str = str(user_config_dir() / "layers")
