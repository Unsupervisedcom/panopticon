# Collect a Claude auth token (`claude setup-token`) for the repo's env-file. Run by the session
# service in a host tmux session (no container); ShellRunner sources the repo's env-file first, so an
# already-configured credential shows up as an env var, and exports PANOPTICON_ENV_FILE (its path).
#
# Whatever route the operator takes, the script converges on a summary + a prompt to press Enter,
# which completes the task and returns them to the dashboard.

env_file="${PANOPTICON_ENV_FILE:-the repo's env-file}"

# How to get back to the dashboard: detach from this tmux session. Detect the prefix + detach key
# from the running server (the operator may have rebound them), falling back to the tmux defaults.
prefix=$(tmux show-options -gv prefix 2>/dev/null)
[ -n "$prefix" ] || prefix="C-b"
detach=$(tmux list-keys -T prefix 2>/dev/null | awk '$NF == "detach-client" { print $(NF - 1); exit }')
[ -n "$detach" ] || detach="d"
dashboard_hint="To return to the dashboard without finishing, detach: press $prefix then $detach (the task stays running)."

# Show how to get back to the dashboard up front, before anything else.
echo "$dashboard_hint"
echo

summary=""

# Mint a token and record the outcome in $summary. On success, capture the minted token and write it
# straight into the repo's env-file (commenting out any previous one — see store_oauth_token); fall
# back to on-screen copy instructions when it can't be captured or there's no env-file to write to.
# extract_oauth_token / store_oauth_token come from setup_repo_lib.sh (prepended by shell_script()).
collect_token() {
    echo
    echo "Running 'claude setup-token' — follow the prompts to mint a token."
    echo
    umask 077
    _ct_ok=1
    _ct_token=""
    if command -v script >/dev/null 2>&1; then
        # Wrap the OAuth flow in a pty (`script`) so its interactive prompts still work, while teeing
        # the session to a private log we read the minted token back from. `-e` returns claude's exit
        # status; the log holds the token in plaintext, so remove it as soon as we've read it.
        _ct_log=$(mktemp "${TMPDIR:-/tmp}/panopticon-setup-token.XXXXXX")
        if script -q -e -c 'claude setup-token' "$_ct_log"; then
            _ct_token=$(extract_oauth_token "$_ct_log")
        else
            _ct_ok=0
        fi
        rm -f "$_ct_log"
    else
        # No `script` to capture with: run it directly (the operator still sees the token on screen).
        claude setup-token || _ct_ok=0
    fi

    if [ "$_ct_ok" -eq 0 ]; then
        summary="'claude setup-token' failed or was cancelled — no token was collected."
    elif [ -n "$_ct_token" ] && [ -n "${PANOPTICON_ENV_FILE:-}" ] \
        && store_oauth_token "$_ct_token" "$PANOPTICON_ENV_FILE"; then
        echo
        echo "Wrote the new token to $env_file as CLAUDE_CODE_OAUTH_TOKEN (any previous one was commented out)."
        summary="Minted a new token and wrote it to $env_file (any previous token was commented out)."
    else
        # Minted, but we couldn't capture/extract it or there's no env-file configured — guide the copy.
        echo
        echo "Token minted. Copy the token shown above into $env_file as:"
        echo "    CLAUDE_CODE_OAUTH_TOKEN=<token>"
        summary="Minted a new token — copy it into $env_file as CLAUDE_CODE_OAUTH_TOKEN."
    fi
}

if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] || [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    echo "A Claude credential is already configured in $env_file."
    echo "To keep using it, drop this task instead (press 'x' in the dashboard)."
    echo
    printf 'Collect a new token anyway? [y/N] '
    read answer
    case "$answer" in
        [Yy]*) collect_token ;;
        *) summary="Kept the existing credential in $env_file — nothing collected." ;;
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
    printf 'Press Enter to collect a token now. '
    read _
    collect_token
fi

# Every route converges here: summarize what happened, then complete the task on Enter (which ends
# the session and returns the operator to the dashboard; detaching instead — see the hint above —
# leaves it running).
echo
echo "Summary: $summary"
echo
printf 'Press Enter to complete this task and return to the dashboard. '
read _
# panopticon_advance is provided by the panopticon shell lib (loaded by the session service).
panopticon_advance || echo "warning: could not mark the task complete via $PANOPTICON_SERVICE_URL"
