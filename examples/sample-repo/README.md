# panopticon demo repo

A minimal sample repository used by `panopticon demo` to populate the dashboard
with two spike tasks, so you can watch ≥ 2 agents work at once without needing a
GitHub account.

`panopticon demo` copies these files into a temporary directory, initialises a git
repo there, and registers it with the task service — nothing in this directory is
modified at runtime.
