# Guide the operator through a repo's host-side setup: mint a Claude auth token (`claude
# setup-token`) and — for a GitHub repo — record a `GH_TOKEN`, writing each into the repo's env-file.
# Run by the session service in a host tmux session (no container); ShellRunner sources the repo's
# env-file first (so an already-configured credential shows up as an env var) and exports
# PANOPTICON_ENV_FILE (its path), PANOPTICON_GIT_URL (the repo's remote, used to detect a GitHub
# forge below), and PANOPTICON_REPO_NAME (the repo's label, for the summary).
#
# Whatever route the operator takes, the script converges on a bulleted summary + a prompt to press
# Enter, which completes the task and returns them to the dashboard.

env_file="${PANOPTICON_ENV_FILE:-the repo's env-file}"
repo_name="${PANOPTICON_REPO_NAME:-this repo}"
repo_url="${PANOPTICON_GIT_URL:-}"
repo_label=$(repo_source_label "$repo_url")

# How to get back to the dashboard: detach from this tmux session. Detect the prefix + detach key
# from the running server (the operator may have rebound them), falling back to the tmux defaults.
prefix=$(tmux show-options -gv prefix 2>/dev/null)
[ -n "$prefix" ] || prefix="C-b"
detach=$(tmux list-keys -T prefix 2>/dev/null | awk '$NF == "detach-client" { print $(NF - 1); exit }')
[ -n "$detach" ] || detach="d"
dashboard_hint="To return to the dashboard without finishing, detach: press $prefix then $detach (you can resume this task any time from the dashboard)."

# Show how to get back to the dashboard up front, before anything else.
echo "$dashboard_hint"
echo

# Work out what's already configured and what setting this repo up entails. The Claude credential is
# always needed (the agent runs `claude` regardless); a GH_TOKEN is only needed for a GitHub remote
# (a local checkout has nothing to push). "Configured" means the env-file already carries it.
claude_configured=0
if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] || [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    claude_configured=1
fi
gh_needed=0
gh_configured=0
if is_github_url "$repo_url"; then
    gh_needed=1
    env_file_has_var GH_TOKEN "${PANOPTICON_ENV_FILE:-}" && gh_configured=1
fi

# What we know about the repo, and what its setup entails — two bulleted lists up front.
echo "This repo:"
echo "  • Name: $repo_name"
echo "  • Source: $repo_label"
echo
echo "To set up:"
if [ "$claude_configured" -eq 1 ]; then
    echo "  • Claude credential — already configured"
else
    echo "  • Claude credential — needed"
fi
if [ "$gh_needed" -eq 1 ]; then
    if [ "$gh_configured" -eq 1 ]; then
        echo "  • GH_TOKEN — already configured"
    else
        echo "  • GH_TOKEN — needed (GitHub repo)"
    fi
else
    echo "  • GH_TOKEN — not needed (not a GitHub repo)"
fi
echo

# The closing summary is a bullet per step; each step appends its outcome here.
summary=""
add_summary() {
    if [ -z "$summary" ]; then
        summary="  • $1"
    else
        summary="$summary
  • $1"
    fi
}

# Mint a Claude token and record the outcome. On success, capture the minted token and write it
# straight into the repo's env-file (commenting out any previous one — see store_env_token); fall
# back to on-screen copy instructions when it can't be captured or there's no env-file to write to.
# extract_oauth_token / store_env_token come from setup_repo_lib.sh (prepended by shell_script()).
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
        add_summary "Claude credential: 'claude setup-token' failed or was cancelled — nothing collected."
    elif [ -n "$_ct_token" ] && [ -n "${PANOPTICON_ENV_FILE:-}" ] \
        && store_env_token CLAUDE_CODE_OAUTH_TOKEN "$_ct_token" "$PANOPTICON_ENV_FILE"; then
        echo
        echo "Wrote the new token to $env_file as CLAUDE_CODE_OAUTH_TOKEN (any previous one was commented out)."
        add_summary "Claude credential: minted a new token and wrote it to $env_file (any previous one was commented out)."
    else
        # Minted, but we couldn't capture/extract it or there's no env-file configured — guide the copy.
        echo
        echo "Token minted. Copy the token shown above into $env_file as:"
        echo "    CLAUDE_CODE_OAUTH_TOKEN=<token>"
        add_summary "Claude credential: minted a new token — copy it into $env_file as CLAUDE_CODE_OAUTH_TOKEN."
    fi
}

# Write a GH token into the env-file, or record why we couldn't — shared by both the reuse-from-env
# and `gh auth login` paths (mirrors the Claude token's store step). $1 ok flag, $2 token, $3 the
# source label for the summary. Goes through store_env_token, so an existing GH_TOKEN is commented
# out and replaced just like the Claude token.
store_gh_token() {
    if [ "$1" -eq 0 ]; then
        add_summary "GH_TOKEN: $3 failed or was cancelled — nothing collected."
    elif [ -n "$2" ] && [ -n "${PANOPTICON_ENV_FILE:-}" ] \
        && store_env_token GH_TOKEN "$2" "$PANOPTICON_ENV_FILE"; then
        echo
        echo "Wrote GH_TOKEN to $env_file (any previous one was commented out)."
        add_summary "GH_TOKEN: wrote it to $env_file from $3 (any previous one was commented out)."
    else
        echo
        echo "Couldn't write GH_TOKEN. Add it to $env_file yourself:"
        echo "    GH_TOKEN=<a GitHub token>"
        add_summary "GH_TOKEN: couldn't write it — add GH_TOKEN to $env_file yourself."
    fi
}

# Authenticate to GitHub with `gh auth login`, then capture the token with `gh auth token` and store
# it — the fallback when there's no GH_TOKEN in the environment to reuse. Guarded on `gh` being
# installed; otherwise guides the operator to add one by hand.
collect_gh_token() {
    if ! command -v gh >/dev/null 2>&1; then
        echo
        echo "The 'gh' CLI isn't installed on this host, so I can't run 'gh auth login'."
        echo "Add a token to $env_file yourself instead:"
        echo "    GH_TOKEN=<a GitHub token, e.g. from 'gh auth token'>"
        add_summary "GH_TOKEN: 'gh' not installed — add GH_TOKEN to $env_file yourself."
        return
    fi
    echo
    echo "Running 'gh auth login' — follow the prompts to authenticate to GitHub."
    echo
    _gt_ok=1
    gh auth login || _gt_ok=0
    _gt_token=""
    [ "$_gt_ok" -eq 1 ] && _gt_token=$(gh auth token 2>/dev/null)
    store_gh_token "$_gt_ok" "$_gt_token" "'gh auth login'"
}

# Get a GH_TOKEN into the env-file for a GitHub repo: reuse one already in the environment if the
# operator has it (the shell runner inherits the host env — the fast path), else authenticate with
# `gh auth login`.
setup_gh_token() {
    if [ -n "${GH_TOKEN:-}" ]; then
        echo "A GH_TOKEN is set in your environment. Adding it to $env_file lets task containers use"
        echo "'gh' and push over HTTPS."
        echo
        printf 'Add the GH_TOKEN from your environment to %s? [Y/n] ' "$env_file"
        read gh_answer
        case "$gh_answer" in
            [Nn]*) add_summary "GH_TOKEN: skipped — add GH_TOKEN to $env_file yourself." ;;
            *) store_gh_token 1 "$GH_TOKEN" "your environment" ;;
        esac
    else
        echo "No GH_TOKEN in your environment — a GitHub repo needs one to push and open PRs."
        echo
        echo "Prefer to use your own? Press $prefix then $detach to go to the dashboard, drop this task using 'x' and add"
        echo "    GH_TOKEN=<a GitHub token>"
        echo "to $env_file yourself."
        echo
        printf 'Authenticate to GitHub with gh now? [Y/n] '
        read gh_answer
        case "$gh_answer" in
            [Nn]*) add_summary "GH_TOKEN: skipped — add GH_TOKEN to $env_file yourself." ;;
            *) collect_gh_token ;;
        esac
    fi
}

# --- Claude credential -------------------------------------------------------------------------
if [ "$claude_configured" -eq 1 ]; then
    echo "A Claude credential is already configured in $env_file."
    echo
    printf 'Collect a new token anyway? [y/N] '
    read answer
    case "$answer" in
        [Yy]*) collect_token ;;
        *) add_summary "Claude credential: kept the existing one in $env_file — nothing collected." ;;
    esac
else
    echo "No Claude credential found in $env_file."
    echo "About to collect one with 'claude setup-token'."
    echo
    echo "Prefer to use your own? Press $prefix then $detach to go to the dashboard, drop this task using 'x' and add one of"
    echo "these to $env_file yourself:"
    echo "    CLAUDE_CODE_OAUTH_TOKEN=<token from 'claude setup-token'>"
    echo "    ANTHROPIC_API_KEY=<your Anthropic API key>"
    echo
    printf 'Press Enter to collect a token now. '
    read _
    collect_token
fi

# --- GH_TOKEN (GitHub repos only) --------------------------------------------------------------
if [ "$gh_needed" -eq 1 ]; then
    echo
    if [ "$gh_configured" -eq 1 ]; then
        echo "A GH_TOKEN is already configured in $env_file."
        echo
        printf 'Replace it? [y/N] '
        read answer
        case "$answer" in
            [Yy]*) setup_gh_token ;;
            *) add_summary "GH_TOKEN: kept the existing one in $env_file — nothing collected." ;;
        esac
    else
        setup_gh_token
    fi
fi

# Every route converges here: summarize what happened (a bullet per step), then complete the task on
# Enter (which ends the session and returns the operator to the dashboard; detaching instead — see
# the hint above — leaves it running).
echo
echo "Summary:"
if [ -n "$summary" ]; then
    echo "$summary"
else
    echo "  • Nothing to do — everything was already configured."
fi
echo
printf 'Press Enter to complete this task and return to the dashboard. '
read _
# panopticon_advance is provided by the panopticon shell lib (loaded by the session service).
panopticon_advance || echo "warning: could not mark the task complete via $PANOPTICON_SERVICE_URL"
