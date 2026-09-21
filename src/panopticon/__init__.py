"""panopticon — keep an eye on your agents."""

#: The package version, and the **only** place it is hand-edited: ``pyproject.toml`` derives its
#: ``[project] version`` from this line (``[tool.hatch.version]``). Bumping it is what tells a host
#: its base task-container image is stale, since the runner compares this against the image's
#: ``org.panopticon.version`` label (``sessionservice/images.py``) — so bump it whenever a change
#: ships *inside* the image (the bundled Dockerfile or entrypoint).
__version__ = "0.1.1"
