"""Per-user directories for panopticon (XDG Base Directory spec) and the paths derived from them.

Two layers live here. The ``user_*_dir()`` functions **resolve the three base directories** by
walking the override chain:

  ``PANOPTICON_DATA/CACHE/CONFIG`` (top-level override) →
  ``XDG_DATA_HOME/XDG_CACHE_HOME/XDG_CONFIG_HOME`` →
  ``~/.local/share`` / ``~/.cache`` / ``~/.config``

The module-level constants below then **compose panopticon's well-known sub-paths** onto those
bases (artifacts, tasks, the clone cache, layer files). Setting one base-dir variable moves
its entire subtree — there are no per-path overrides. Code that needs a base for an ad-hoc subpath
not listed here (e.g. ``workflows/``, ``hooks/``) calls the ``user_*_dir()`` functions directly.
"""

from __future__ import annotations

import os
from pathlib import Path


def user_data_dir() -> Path:
    """Return the panopticon data directory (``~/.local/share/panopticon`` by default).

    Resolution: ``$PANOPTICON_DATA`` → ``$XDG_DATA_HOME/panopticon`` → ``~/.local/share/panopticon``.
    Does **not** create the directory — callers that write to it must mkdir themselves.
    """
    override = os.environ.get("PANOPTICON_DATA")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "panopticon"


def user_cache_dir() -> Path:
    """Return the panopticon cache directory (``~/.cache/panopticon`` by default).

    Resolution: ``$PANOPTICON_CACHE`` → ``$XDG_CACHE_HOME/panopticon`` → ``~/.cache/panopticon``.
    Does **not** create the directory — callers that write to it must mkdir themselves.
    """
    override = os.environ.get("PANOPTICON_CACHE")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "panopticon"


def user_config_dir() -> Path:
    """Return the panopticon config directory (``~/.config/panopticon`` by default).

    Resolution: ``$PANOPTICON_CONFIG`` → ``$XDG_CONFIG_HOME/panopticon`` → ``~/.config/panopticon``.
    Does **not** create the directory — callers that write to it must mkdir themselves.
    """
    override = os.environ.get("PANOPTICON_CONFIG")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "panopticon"


#: Task artifact store — $PANOPTICON_DATA/artifacts
ARTIFACTS_DIR: str = str(user_data_dir() / "artifacts")

#: Per-task workspace clones — $PANOPTICON_DATA/tasks
TASKS_DIR: str = str(user_data_dir() / "tasks")

#: Per-repo clone cache — $PANOPTICON_CACHE/repos
CLONE_CACHE_DIR: str = str(user_cache_dir() / "repos")

#: Operator-authored Dockerfile layer files — $PANOPTICON_CONFIG/layers
LAYERS_DIR: str = str(user_config_dir() / "layers")

#: Per-repo secret env-files — $PANOPTICON_CONFIG/secrets. A repo's ``env_file`` is a name
#: *relative to this dir*; each runner resolves it against its **own** local secrets dir at
#: launch (so a remote runner uses its own host's secrets), and the file's content never crosses
#: the wire (ADR 0007). Mirrors ``LAYERS_DIR``.
SECRETS_DIR: str = str(user_config_dir() / "secrets")

#: Per-repo pre-launch hook scripts — $PANOPTICON_CONFIG/hooks. A repo's ``hook_file`` is a name
#: *relative to this dir*; each runner resolves it against its **own** local hooks dir when it
#: spawns a task's container (so a remote runner uses its own host's script). Mirrors
#: ``SECRETS_DIR`` — see ``docs/hooks.md``.
HOOKS_DIR: str = str(user_config_dir() / "hooks")


def _secrets_dir() -> Path:
    """The secrets dir, resolved dynamically (so ``$PANOPTICON_CONFIG``/XDG overrides take effect).

    :data:`SECRETS_DIR` is the same path as a module-level constant for the common case; this is
    the callable form for runtime resolution and tests that override the config dir."""
    return user_config_dir() / "secrets"


def _layers_dir() -> Path:
    """The layers dir, resolved dynamically (so ``$PANOPTICON_CONFIG``/XDG overrides take effect).

    :data:`LAYERS_DIR` is the same path as a module-level constant for the common case; this is
    the callable form for runtime resolution and tests that override the config dir."""
    return user_config_dir() / "layers"


def relativize_layers_file(path: str, *, layers_dir: str | Path | None = None) -> str:
    """Normalize a user-entered image-layer ``path`` to a name relative to the layers dir.

    A repo's ``image_layer_file`` is a name relative to the layers dir (see :data:`LAYERS_DIR`),
    mirroring how ``env_file`` names resolve against the secrets dir. Used when accepting operator
    input (the dashboard's custom-path field); accepts an absolute or relative path and always
    yields a stored *relative name*:

    - a path inside the layers dir → its subpath relative to the dir (nested names allowed);
    - any other absolute path → its basename (so it lands as a bare name, resolved against each
      runner's own layers dir at spawn);
    - a relative path → returned unchanged (already a name under the dir).

    An empty/whitespace input yields ``""``. ``layers_dir`` defaults to this host's.
    """
    path = path.strip()
    if not path:
        return ""
    p = Path(path)
    if p.is_absolute():
        root = (Path(layers_dir) if layers_dir is not None else _layers_dir()).resolve()
        resolved = p.resolve()
        if resolved == root or root in resolved.parents:
            return str(resolved.relative_to(root))
        return p.name
    return path


def secrets_file_path(name: str | None, *, secrets_dir: str | Path | None = None) -> str | None:
    """Resolve a stored ``env_file`` *name* to an absolute path under the secrets dir.

    ``name`` is a path relative to the secrets dir (see :data:`SECRETS_DIR`); ``secrets_dir``
    defaults to this host's (resolved dynamically). Returns ``None`` for a falsy name; otherwise
    joins it onto the root and returns the absolute path, **refusing anything that escapes the
    root** (``..`` or an absolute name) — the same guard the layer store applies. The runner calls
    this to build the ``docker run --env-file`` argument against its own host's secrets dir.
    """
    if not name:
        return None
    root = (Path(secrets_dir) if secrets_dir is not None else _secrets_dir()).resolve()
    path = (root / name).resolve()
    if path != root and root not in path.parents:
        raise ValueError(f"env_file name {name!r} escapes the secrets dir")
    return str(path)


def _hooks_dir() -> Path:
    """The hooks dir, resolved dynamically (so ``$PANOPTICON_CONFIG``/XDG overrides take effect).

    :data:`HOOKS_DIR` is the same path as a module-level constant for the common case; this is the
    callable form for runtime resolution and tests that override the config dir."""
    return user_config_dir() / "hooks"


def hook_file_path(name: str | None, *, hooks_dir: str | Path | None = None) -> str | None:
    """Resolve a stored ``hook_file`` *name* to an absolute path under the hooks dir.

    ``name`` is a path relative to the hooks dir (see :data:`HOOKS_DIR`); ``hooks_dir`` defaults to
    this host's (resolved dynamically). Returns ``None`` for a falsy name; otherwise joins it onto
    the root and returns the absolute path, **refusing anything that escapes the root** (``..`` or
    an absolute name) — the same guard :func:`secrets_file_path` applies. The runner calls this to
    resolve a repo's pre-launch hook against its own host's hooks dir.
    """
    if not name:
        return None
    root = (Path(hooks_dir) if hooks_dir is not None else _hooks_dir()).resolve()
    path = (root / name).resolve()
    if path != root and root not in path.parents:
        raise ValueError(f"hook_file name {name!r} escapes the hooks dir")
    return str(path)


def relativize_secrets_file(path: str, *, secrets_dir: str | Path | None = None) -> str:
    """Normalize a user-entered env-file ``path`` to a name relative to the secrets dir.

    Inverse of :func:`secrets_file_path`, used when accepting operator input (the dashboard's
    custom-path field). Accepts an absolute or relative path and always yields a stored *relative
    name*:

    - a path inside the secrets dir → its subpath relative to the dir;
    - any other absolute path → its basename (so it lands as a bare name, resolved against each
      runner's own secrets dir at launch);
    - a relative path → returned unchanged (already a name under the dir).

    An empty/whitespace input yields ``""``. ``secrets_dir`` defaults to this host's.
    """
    path = path.strip()
    if not path:
        return ""
    p = Path(path)
    if p.is_absolute():
        root = (Path(secrets_dir) if secrets_dir is not None else _secrets_dir()).resolve()
        resolved = p.resolve()
        if resolved == root or root in resolved.parents:
            return str(resolved.relative_to(root))
        return p.name
    return path


def relativize_hook_file(path: str, *, hooks_dir: str | Path | None = None) -> str:
    """Normalize a user-entered hook ``path`` to a name relative to the hooks dir.

    The hook analogue of :func:`relativize_secrets_file` (used by the dashboard repo form's
    custom-path input). Accepts an absolute or relative path and always yields a stored *relative
    name*:

    - a path inside the hooks dir → its subpath relative to the dir;
    - any other absolute path → its basename (so it lands as a bare name, resolved against each
      runner's own hooks dir at spawn);
    - a relative path → returned unchanged (already a name under the dir).

    An empty/whitespace input yields ``""``. ``hooks_dir`` defaults to this host's.
    """
    path = path.strip()
    if not path:
        return ""
    p = Path(path)
    if p.is_absolute():
        root = (Path(hooks_dir) if hooks_dir is not None else _hooks_dir()).resolve()
        resolved = p.resolve()
        if resolved == root or root in resolved.parents:
            return str(resolved.relative_to(root))
        return p.name
    return path
