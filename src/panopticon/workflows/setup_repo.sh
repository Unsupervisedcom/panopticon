# Collect a Claude auth token (`claude setup-token`) for the repo's env-file. Run by the session
# service in a host tmux session (no container); ShellRunner sources the repo's env-file first, so an
# already-configured credential shows up as an env var, and exports PANOPTICON_ENV_FILE (its path)
# and PANOPTICON_GIT_URL (the repo's remote, used to detect a GitHub forge below).
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

# Append clause $1 to the running $summary, space-separating it from anything already there. Each
# step records its own outcome this way, so the order the steps run in doesn't clobber the summary.
add_summary() {
    if [ -n "$summary" ]; then
        summary="$summary $1"
    else
        summary="$1"
    fi
}

# The shared secrets file quickstart registers a repo with (see ensure_secrets_file) — every repo on
# it draws from the same credentials. A repo can instead have its own <repo>.env, isolating it.
_SHARED_ENV_FILE="shared.env"

# While the repo is still on the shared secrets file, offer to give it its own <repo>.env instead,
# before we collect a token — so this repo's credential lands in an isolated file rather than the
# shared one. The repo *begins* on the shared file (quickstart registers it that way) and only moves
# when the operator picks the repo-specific option here; any other answer keeps the shared file.
# Needs PANOPTICON_REPO_ID (the repo to repoint) and PANOPTICON_ENV_FILE (the current, absolute,
# shared-file path). ensure_private_env_file / set_repo_env_file come from setup_repo_lib.sh.
maybe_choose_env_file() {
    [ -n "${PANOPTICON_REPO_ID:-}" ] || return 0
    [ -n "${PANOPTICON_ENV_FILE:-}" ] || return 0
    _mce_dir=$(dirname "$PANOPTICON_ENV_FILE")
    _mce_cur=$(basename "$PANOPTICON_ENV_FILE")
    # Only offer while the repo is on the shared file; one already on its own file keeps it.
    [ "$_mce_cur" = "$_SHARED_ENV_FILE" ] || return 0
    _mce_repo_file="${PANOPTICON_REPO_ID}.env"
    # Nothing to choose when the repo-specific name would just be the shared file again (a repo
    # whose id makes <repo>.env == shared.env, i.e. one literally named "shared").
    [ "$_mce_repo_file" != "$_SHARED_ENV_FILE" ] || return 0
    echo
    echo "This repo is using the shared credentials file ($_mce_cur), which every repo on the shared"
    echo "file draws from. You can keep it, or give this repo its own file ($_mce_repo_file) so its"
    echo "Claude/GitHub credentials are isolated from other repos'."
    echo
    printf 'Use the [s]hared file or a [r]epo-specific one? [S/r] '
    read env_answer
    case "$env_answer" in
        [Rr]*)
            _mce_path="$_mce_dir/$_mce_repo_file"
            # Create the file first (the task service won't point the repo at a missing file), then
            # repoint the repo record so future task containers source it too.
            if ensure_private_env_file "$_mce_path" \
                && set_repo_env_file "$PANOPTICON_SERVICE_URL" "$PANOPTICON_REPO_ID" "$_mce_repo_file"; then
                # Retarget the rest of the script at the new file, and forget the shared file's
                # already-sourced credentials so the check below reflects the (empty) repo file.
                PANOPTICON_ENV_FILE="$_mce_path"
                export PANOPTICON_ENV_FILE
                env_file="$PANOPTICON_ENV_FILE"
                unset CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY
                [ -f "$_mce_path" ] && { set -a; . "$_mce_path"; set +a; }
                echo "Gave this repo its own credentials file: $env_file."
                add_summary "Gave the repo its own credentials file ($_mce_repo_file)."
            else
                echo "Could not switch to a repo-specific file; keeping the shared one ($_mce_cur)."
                add_summary "Kept the shared credentials file ($_mce_cur) — the switch failed."
            fi
            ;;
        *) add_summary "Kept the shared credentials file ($_mce_cur)." ;;
    esac
}

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
        add_summary "'claude setup-token' failed or was cancelled — no token was collected."
    elif [ -n "$_ct_token" ] && [ -n "${PANOPTICON_ENV_FILE:-}" ] \
        && store_oauth_token "$_ct_token" "$PANOPTICON_ENV_FILE"; then
        echo
        echo "Wrote the new token to $env_file as CLAUDE_CODE_OAUTH_TOKEN (any previous one was commented out)."
        add_summary "Minted a new token and wrote it to $env_file (any previous token was commented out)."
    else
        # Minted, but we couldn't capture/extract it or there's no env-file configured — guide the copy.
        echo
        echo "Token minted. Copy the token shown above into $env_file as:"
        echo "    CLAUDE_CODE_OAUTH_TOKEN=<token>"
        add_summary "Minted a new token — copy it into $env_file as CLAUDE_CODE_OAUTH_TOKEN."
    fi
}

# Offer to record a GitHub token in the repo's env-file, but only when it's both wanted and missing:
# the repo is hosted on GitHub (PANOPTICON_GIT_URL), a GH_TOKEN is present in the environment (e.g.
# the operator's own shell — the shell runner inherits the host env), and the env-file doesn't
# already carry an active GH_TOKEN line. Records the outcome in $summary. is_github_url /
# env_file_has_var / append_env_var come from setup_repo_lib.sh (prepended by shell_script()).
maybe_offer_github_token() {
    is_github_url "${PANOPTICON_GIT_URL:-}" || return 0
    [ -n "${GH_TOKEN:-}" ] || return 0
    [ -n "${PANOPTICON_ENV_FILE:-}" ] || return 0
    ! env_file_has_var GH_TOKEN "$PANOPTICON_ENV_FILE" || return 0
    echo
    echo "This repo is hosted on GitHub and a GH_TOKEN is set in your environment, but $env_file"
    echo "has no GH_TOKEN. Adding it lets task containers use 'gh' and push over HTTPS."
    echo
    printf 'Add GH_TOKEN to %s? [y/N] ' "$env_file"
    read gh_answer
    case "$gh_answer" in
        [Yy]*)
            if append_env_var GH_TOKEN "$GH_TOKEN" "$PANOPTICON_ENV_FILE"; then
                echo "Wrote GH_TOKEN to $env_file."
                add_summary "Added GH_TOKEN to $env_file."
            else
                echo "Could not write GH_TOKEN to $env_file."
                add_summary "Could not add GH_TOKEN to $env_file."
            fi
            ;;
        *) add_summary "Left GH_TOKEN out of $env_file." ;;
    esac
}

# Before touching credentials, let the operator move this repo off the shared file onto its own
# (no-op unless it's still on the shared file); a repo-specific choice retargets $env_file below.
maybe_choose_env_file

if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] || [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    echo "A Claude credential is already configured in $env_file."
    echo "To keep using it, drop this task instead (press 'x' in the dashboard)."
    echo
    printf 'Collect a new token anyway? [y/N] '
    read answer
    case "$answer" in
        [Yy]*) collect_token ;;
        *) add_summary "Kept the existing credential in $env_file — nothing collected." ;;
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

# With the Claude credential settled, offer to record a GitHub token too (no-op unless it applies).
maybe_offer_github_token

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
