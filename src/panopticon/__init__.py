"""panopticon — keep an eye on your agents."""

#: The package version, and the single place it is hand-edited — ``pyproject.toml`` derives its
#: ``[project] version`` from this line (``[tool.hatch.version]``). The runner also compares it
#: against a base image's ``org.panopticon.version`` label to decide the image is stale
#: (``sessionservice/images.py``), so it gates what a host rebuilds, not just what PyPI reports.
__version__ = "0.1.1"
