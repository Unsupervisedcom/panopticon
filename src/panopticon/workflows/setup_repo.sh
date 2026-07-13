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

# Mint a token and record the outcome in $summary. Runs `claude setup-token` via a host `claude`
# when one is installed, else falls back to running it in the base task-container image (announced
# explicitly, building the image first if it's missing). On success, capture the minted
# token and write it straight into the repo's env-file (commenting out any previous one — see
# store_oauth_token); fall back to on-screen copy instructions when it can't be captured or there's
# no env-file to write to. setup_token_command / base_image_present / extract_oauth_token /
# store_oauth_token come from setup_repo_lib.sh (prepended by shell_script()).
collect_token() {
    _ct_image="${PANOPTICON_BASE_IMAGE:-panopticon-base}"
    # Resolve how to run `claude setup-token`: a host `claude`, or a docker fallback in the base
    # image. A nonzero return means neither route is available.
    if ! _ct_cmd=$(setup_token_command "$_ct_image"); then
        echo
        echo "Can't mint a token: 'claude' isn't installed on this host and Docker isn't available"
        echo "to run it in a container. Install the Claude CLI (https://claude.ai/install.sh), or drop"
        echo "this task (press 'x' in the dashboard) and add a credential to $env_file yourself."
        add_summary "Couldn't collect a token — no 'claude' CLI and no Docker on this host."
        return
    fi

    # No host `claude` means we're taking the docker fallback — be explicit about it, and build the
    # base image first when it's missing (from the package's bundled Dockerfile; just needs Docker).
    if ! command -v claude >/dev/null 2>&1; then
        echo
        echo "'claude' isn't installed on this host — running 'claude setup-token' in a container"
        echo "($_ct_image) via Docker instead."
        if ! base_image_present "$_ct_image"; then
            echo
            echo "The base image '$_ct_image' isn't built yet — building it now."
            echo "This can take a few minutes; the build output follows."
            # PANOPTICON_BUILD_BASE_CMD is injected by the shell runner: it builds the base image
            # from the package's bundled Dockerfile (no source checkout needed — works for pip users).
            if [ -z "${PANOPTICON_BUILD_BASE_CMD:-}" ]; then
                echo
                echo "Can't build the base image automatically here. Build it on the panopticon host"
                echo "('panopticon build'), or drop this task and add a credential to $env_file"
                echo "yourself (see docs/container-auth.md)."
                add_summary "Base image '$_ct_image' missing and no build command available."
                return
            fi
            if ! sh -c "$PANOPTICON_BUILD_BASE_CMD"; then
                echo
                echo "Building '$_ct_image' failed. Build it manually ('panopticon build') or drop"
                echo "this task and add a credential to $env_file yourself."
                add_summary "Couldn't build the base image '$_ct_image' for the container fallback."
                return
            fi
            if ! base_image_present "$_ct_image"; then
                echo
                echo "The base image '$_ct_image' still isn't present after building — aborting."
                add_summary "Base image '$_ct_image' unavailable after a build attempt."
                return
            fi
        fi
    fi

    echo
    echo "Running '$_ct_cmd' — follow the prompts to mint a token."
    echo
    umask 077
    _ct_ok=1
    _ct_token=""
    if command -v script >/dev/null 2>&1; then
        # Wrap the OAuth flow in a pty (`script`) so its interactive prompts still work, while teeing
        # the session to a private log we read the minted token back from. `-e` returns the command's
        # exit status; the log holds the token in plaintext, so remove it as soon as we've read it.
        _ct_log=$(mktemp "${TMPDIR:-/tmp}/panopticon-setup-token.XXXXXX")
        if script -q -e -c "$_ct_cmd" "$_ct_log"; then
            _ct_token=$(extract_oauth_token "$_ct_log")
        else
            _ct_ok=0
        fi
        rm -f "$_ct_log"
    else
        # No `script` to capture with: run it directly (the operator still sees the token on screen).
        eval "$_ct_cmd" || _ct_ok=0
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
