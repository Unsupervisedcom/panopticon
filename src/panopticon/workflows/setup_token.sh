# Collect a Claude auth token (`claude setup-token`) for the repo's env-file. Run by the session
# service in a host tmux session (no container); ShellRunner sources the repo's env-file first, so an
# already-configured credential shows up as an env var, and exports PANOPTICON_ENV_FILE (its path).

env_file="${PANOPTICON_ENV_FILE:-the repo's env-file}"

# How to get back to the dashboard: detach from this tmux session. Detect the prefix + detach key
# from the running server (the operator may have rebound them), falling back to the tmux defaults.
prefix=$(tmux show-options -gv prefix 2>/dev/null)
[ -n "$prefix" ] || prefix="C-b"
detach=$(tmux list-keys -T prefix 2>/dev/null | awk '$NF == "detach-client" { print $(NF - 1); exit }')
[ -n "$detach" ] || detach="d"
dashboard_hint="To return to the dashboard, detach from this session: press $prefix then $detach."

# Mint a token, record where it goes, and mark the task complete.
collect_token() {
    echo
    echo "Running 'claude setup-token' — follow the prompts to mint a token."
    echo
    if claude setup-token; then
        echo
        echo "Token minted. Copy the token shown above into $env_file as:"
        echo "    CLAUDE_CODE_OAUTH_TOKEN=<token>"
        echo
        echo "Marking this task complete."
        curl --silent --show-error --fail --request POST \
            "$PANOPTICON_SERVICE_URL/tasks/$PANOPTICON_TASK_ID/operations/advance" \
            >/dev/null \
            || echo "warning: could not mark the task complete via $PANOPTICON_SERVICE_URL"
    else
        echo "claude setup-token failed or was cancelled — leaving the task unchanged."
    fi
    echo
    echo "$dashboard_hint"
    printf 'Press Enter to close this session. '
    read _
}

if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] || [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    echo "A Claude credential is already configured in $env_file."
    echo "If you want to keep using it, drop this task (press 'x' in the dashboard)."
    echo
    printf 'Collect a new token anyway? [y/N] '
    read answer
    case "$answer" in
        [Yy]*) collect_token ;;
        *)
            echo "Keeping the existing credential — nothing collected."
            echo "Drop this task (press 'x' in the dashboard) when you're done."
            echo "$dashboard_hint"
            ;;
    esac
else
    echo "No Claude credential found in $env_file."
    echo "About to collect one with 'claude setup-token'."
    echo
    echo "Prefer to use your own? Drop this task (press 'x' in the dashboard) and add one of"
    echo "these to $env_file yourself:"
    echo "    CLAUDE_CODE_OAUTH_TOKEN=<token from 'claude setup-token'>"
    echo "    ANTHROPIC_API_KEY=<your Anthropic API key>"
    echo
    printf "Press Enter to collect a token now (or detach — %s then %s — and drop the task to add your own). " "$prefix" "$detach"
    read _
    collect_token
fi
