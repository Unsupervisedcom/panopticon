# Set up a repo's per-repo credentials for task containers: a Claude token, and — for a GitHub repo —
# a GH_TOKEN, written into the repo's env-file. Run by the session service in a host tmux session (no
# container); ShellRunner sources the repo's env-file first (so a configured credential shows up as
# an env var) and exports PANOPTICON_ENV_FILE (its path), PANOPTICON_GIT_URL (the repo's remote, used
# to detect a GitHub forge below), and PANOPTICON_REPO_NAME (the repo's label, for the summary).
#
# Each credential can be adopted from the operator's own environment, pasted, or (for Claude) minted
# with `claude setup-token`. Whatever route the operator takes, the script converges on a bulleted
# summary + a prompt to press Enter, which completes the task and returns them to the dashboard.

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

# Open by explaining what this does and that the operator stays in control — task containers get
# per-repo tokens from the env-file (not the operator's own session), and they can opt out entirely.
echo "Setting up '$repo_name'. Task containers use per-repo credentials from this repo's env-file — a"
echo "Claude token, and a GH_TOKEN for GitHub repos — so the agent runs on its own tokens, not your"
echo "personal session. You can skip this and set up your own secrets by editing $env_file yourself."
echo

# Show how to get back to the dashboard, up front before any prompts.
echo "$dashboard_hint"
echo

# Work out what's already set up and what this repo needs. The Claude credential is always needed
# (the agent runs `claude` regardless); a GH_TOKEN is only needed for a GitHub remote (a local
# checkout has nothing to push). "Configured" means the **env-file** carries it — the only thing the
# container sees (it's launched with `--env-file`, not the host env), so a token that lives only in
# the operator's shell doesn't count as done.
claude_configured=0
if env_file_has_var CLAUDE_CODE_OAUTH_TOKEN "${PANOPTICON_ENV_FILE:-}" \
    || env_file_has_var ANTHROPIC_API_KEY "${PANOPTICON_ENV_FILE:-}"; then
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
    echo "  • Claude credential — already in $env_file"
else
    echo "  • Claude credential — needed"
fi
if [ "$gh_needed" -eq 1 ]; then
    if [ "$gh_configured" -eq 1 ]; then
        echo "  • GH_TOKEN — already in $env_file"
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

# Write token $2 for var $1 into the env-file, echoing + summarizing the outcome. $3 is the source
# label, $4 the credential label for the summary. Goes through store_env_token, so any existing value
# is commented out and replaced. The shared write step for every path (adopt / paste / mint).
store_token() {
    if [ -n "$2" ] && [ -n "${PANOPTICON_ENV_FILE:-}" ] \
        && store_env_token "$1" "$2" "$PANOPTICON_ENV_FILE"; then
        echo
        echo "Wrote $1 to $env_file (any previous one was commented out)."
        add_summary "$4: wrote $1 to $env_file from $3 (any previous one was commented out)."
    else
        echo
        echo "Couldn't write $1. Add it to $env_file yourself."
        add_summary "$4: couldn't write it — add $1 to $env_file yourself."
    fi
}

# Mint a Claude token with `claude setup-token` and store it — the leaf used when the operator has no
# token to adopt or paste. On success, capture the minted token and write it into the env-file; fall
# back to on-screen copy instructions when it can't be captured. extract_oauth_token / store_env_token
# come from setup_repo_lib.sh (prepended by shell_script()).
mint_claude_token() {
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
        add_summary "Claude credential: 'claude setup-token' failed or was cancelled — nothing set up."
    elif [ -n "$_ct_token" ] && [ -n "${PANOPTICON_ENV_FILE:-}" ] \
        && store_env_token CLAUDE_CODE_OAUTH_TOKEN "$_ct_token" "$PANOPTICON_ENV_FILE"; then
        echo
        echo "Wrote the new token to $env_file as CLAUDE_CODE_OAUTH_TOKEN (any previous one was commented out)."
        add_summary "Claude credential: minted a new token and wrote it to $env_file (any previous one was commented out)."
    else
        # Minted, but we couldn't capture/extract it or there's no env-file — guide the copy.
        echo
        echo "Token minted. Copy the token shown above into $env_file as:"
        echo "    CLAUDE_CODE_OAUTH_TOKEN=<token>"
        add_summary "Claude credential: minted a new token — copy it into $env_file as CLAUDE_CODE_OAUTH_TOKEN."
    fi
}

# Set up the Claude credential: offer to adopt a token from the operator's own environment (fast path
# for an already-authenticated operator — masked for consent, default-No), else let them paste one,
# else mint a fresh one with `claude setup-token`. Only offers to adopt a var that isn't already the
# env-file's own (so replacing a configured token goes straight to paste/mint).
setup_claude_token() {
    _sct_var=""
    _sct_val=""
    if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] \
        && ! env_file_has_var CLAUDE_CODE_OAUTH_TOKEN "${PANOPTICON_ENV_FILE:-}"; then
        _sct_var=CLAUDE_CODE_OAUTH_TOKEN
        _sct_val=$CLAUDE_CODE_OAUTH_TOKEN
    elif [ -n "${ANTHROPIC_API_KEY:-}" ] \
        && ! env_file_has_var ANTHROPIC_API_KEY "${PANOPTICON_ENV_FILE:-}"; then
        _sct_var=ANTHROPIC_API_KEY
        _sct_val=$ANTHROPIC_API_KEY
    fi
    if [ -n "$_sct_val" ]; then
        echo "A $_sct_var is set in your environment (ending $(mask_last4 "$_sct_val"))."
        printf 'Add it to %s for task containers to use? [y/N] ' "$env_file"
        read answer
        case "$answer" in
            [Yy]*)
                store_token "$_sct_var" "$_sct_val" "your environment" "Claude credential"
                return
                ;;
        esac
    fi
    echo
    echo "Paste a Claude token to store it (a CLAUDE_CODE_OAUTH_TOKEN or an ANTHROPIC_API_KEY),"
    echo "or press Enter to mint one with 'claude setup-token'."
    printf '> '
    read pasted
    if [ -n "$pasted" ]; then
        _pv=CLAUDE_CODE_OAUTH_TOKEN
        case "$pasted" in sk-ant-api*) _pv=ANTHROPIC_API_KEY ;; esac
        store_token "$_pv" "$pasted" "the token you pasted" "Claude credential"
    else
        mint_claude_token
    fi
}

# Set up the GH_TOKEN for a GitHub repo: adopt one from the operator's environment (masked, default-No)
# if present and not already the env-file's own, else let them paste one, else skip (guide them to add
# it themselves). We don't mint a GitHub token here.
setup_gh_token() {
    if [ -n "${GH_TOKEN:-}" ] && ! env_file_has_var GH_TOKEN "${PANOPTICON_ENV_FILE:-}"; then
        echo "A GH_TOKEN is set in your environment (ending $(mask_last4 "$GH_TOKEN"))."
        echo "Adding it to $env_file lets task containers use 'gh' and push over HTTPS."
        printf 'Add it to %s? [y/N] ' "$env_file"
        read gh_answer
        case "$gh_answer" in
            [Yy]*)
                store_token GH_TOKEN "$GH_TOKEN" "your environment" "GH_TOKEN"
                return
                ;;
        esac
    fi
    echo
    echo "Paste a GitHub token to store it, or press Enter to skip (add GH_TOKEN to $env_file yourself)."
    printf '> '
    read pasted
    if [ -n "$pasted" ]; then
        store_token GH_TOKEN "$pasted" "the token you pasted" "GH_TOKEN"
    else
        add_summary "GH_TOKEN: none set up — add GH_TOKEN to $env_file yourself."
    fi
}

# --- Claude credential -------------------------------------------------------------------------
if [ "$claude_configured" -eq 1 ]; then
    echo "A Claude credential is already set in $env_file."
    echo
    printf 'Replace it? [y/N] '
    read answer
    case "$answer" in
        [Yy]*) setup_claude_token ;;
        *) add_summary "Claude credential: kept the existing one in $env_file." ;;
    esac
else
    setup_claude_token
fi

# --- GH_TOKEN (GitHub repos only) --------------------------------------------------------------
if [ "$gh_needed" -eq 1 ]; then
    echo
    if [ "$gh_configured" -eq 1 ]; then
        echo "A GH_TOKEN is already set in $env_file."
        echo
        printf 'Replace it? [y/N] '
        read answer
        case "$answer" in
            [Yy]*) setup_gh_token ;;
            *) add_summary "GH_TOKEN: kept the existing one in $env_file." ;;
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
    echo "  • Nothing to do — everything was already set up."
fi
echo
printf 'Press Enter to complete this task and return to the dashboard. '
read _
# panopticon_advance is provided by the panopticon shell lib (loaded by the session service).
panopticon_advance || echo "warning: could not mark the task complete via $PANOPTICON_SERVICE_URL"
