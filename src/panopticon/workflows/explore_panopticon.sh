# Open a throwaway clone of panopticon plus an interactive `claude` to help the operator understand
# and navigate the codebase. Run by the session service in a host tmux session (no container);
# ShellRunner sources the repo's env-file first (so `claude` finds a CLAUDE_CODE_OAUTH_TOKEN /
# ANTHROPIC_API_KEY), loads the panopticon shell lib (panopticon_advance, …), and exports
# PANOPTICON_SERVICE_URL / PANOPTICON_TASK_ID. shell_script() prepends REPO_URL (the canonical
# panopticon remote) so a packaged install with no local checkout still has something to clone.
#
# The clone lives in a `mktemp -d` temp dir removed by a trap on exit — "automatically cleaned up"
# whichever way the script ends. We check out the version the operator is running (the remote's
# `v<version>` tag), falling back to the default branch when there's no matching tag. When the
# operator quits `claude`, the task advances to COMPLETE and they return to the dashboard.

# How to get back to the dashboard: detach from this tmux session. Detect the prefix + detach key
# from the running server (the operator may have rebound them), falling back to the tmux defaults.
prefix=$(tmux show-options -gv prefix 2>/dev/null)
[ -n "$prefix" ] || prefix="C-b"
detach=$(tmux list-keys -T prefix 2>/dev/null | awk '$NF == "detach-client" { print $(NF - 1); exit }')
[ -n "$detach" ] || detach="d"

echo "Opening a throwaway clone of panopticon plus Claude to help you explore the codebase."
echo "Nothing is changed — it's a read-only guided tour. The clone is deleted when you quit Claude."
echo
echo "To return to the dashboard without finishing, detach: press $prefix then $detach (you can resume this task any time from the dashboard)."
echo

# Work out the version the operator is running so we can clone the matching release. Try python3
# then python; an empty version just means we fall back to the default branch below.
version=$(python3 -c 'import panopticon; print(panopticon.__version__)' 2>/dev/null)
[ -n "$version" ] || version=$(python -c 'import panopticon; print(panopticon.__version__)' 2>/dev/null)

# A self-cleaning temp dir for the clone: the trap removes it however the script exits (quit,
# detach-then-kill, or error), so nothing is left behind.
tmp=$(mktemp -d "${TMPDIR:-/tmp}/panopticon-explore.XXXXXX") || {
    echo "Couldn't create a temporary directory. Nothing to clean up."
    panopticon_advance || echo "warning: could not mark the task complete via $PANOPTICON_SERVICE_URL"
    exit 0
}
trap 'rm -rf "$tmp"' EXIT INT TERM

clone="$tmp/panopticon"
echo "Cloning $REPO_URL ..."
if ! git clone --quiet "$REPO_URL" "$clone"; then
    echo "Couldn't clone $REPO_URL. Check your network and that the repo is reachable, then try again."
    panopticon_advance || echo "warning: could not mark the task complete via $PANOPTICON_SERVICE_URL"
    exit 0
fi

# Check out the tag that matches the running version (e.g. v0.0.2). If there's no version or no such
# tag, stay on the default branch so the tour still works — just against the latest code.
if [ -n "$version" ] && git -C "$clone" checkout --quiet "v$version" 2>/dev/null; then
    echo "Checked out v$version (the version you're running)."
else
    branch=$(git -C "$clone" rev-parse --abbrev-ref HEAD 2>/dev/null)
    if [ -n "$version" ]; then
        echo "No v$version tag found — exploring the default branch (${branch:-HEAD}) instead."
    else
        echo "Couldn't determine your running version — exploring the default branch (${branch:-HEAD})."
    fi
fi
echo

# The guide framing for Claude: a friendly orientation for someone learning the codebase, pointed at
# the design docs and the module map, and told this is a read-only tour (no changes).
guide_prompt="You are a friendly guide helping the user understand and navigate the panopticon \
codebase, which is checked out in the current directory. panopticon is a self-hosted control plane \
for running a fleet of coding agents in isolated containers, governed by configurable workflows. \
Orient yourself from AGENTS.md (the operating manual, with the module map and glossary) and the \
design docs under docs/design/ (GOALS, ARCHITECTURE, PARITY, ROADMAP, and the ADRs in \
docs/design/decisions/). Answer the user's questions about how the system is structured and how the \
pieces fit together, citing concrete files and symbols (path:line) and reading the code to ground \
your answers. This is a read-only tour to build understanding — do not modify, commit, or run the \
project; just explain and help them find their way around."

cd "$clone" || {
    echo "Couldn't enter the clone at $clone."
    panopticon_advance || echo "warning: could not mark the task complete via $PANOPTICON_SERVICE_URL"
    exit 0
}

echo "Starting Claude — ask it anything about how panopticon works. Quit Claude when you're done."
echo
# Plain interactive Claude (no panopticon MCP wiring, no skills) with the guide framing appended.
claude --append-system-prompt "$guide_prompt"

# Claude has exited: the trap removes the temp clone, and we complete the task (returning the operator
# to the dashboard). Detaching instead — see the hint above — leaves the session running.
echo
echo "Done exploring — cleaning up the temporary clone."
# panopticon_advance is provided by the panopticon shell lib (loaded by the session service).
panopticon_advance || echo "warning: could not mark the task complete via $PANOPTICON_SERVICE_URL"
