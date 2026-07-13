# Helpers for the setup-repo workflow's script, kept in a sourceable file (no side effects at load)
# so they can be unit-tested in isolation. The ShellRunner runs `shell_script()` = this lib +
# setup_repo.sh concatenated, so these functions are defined before the interactive flow calls
# them. POSIX sh; needs `grep`, `sed`, `mktemp`.

# Print the last Claude OAuth token (sk-ant-oat01-…) found in capture file $1, or nothing. Robust to
# surrounding ANSI colour codes: the token's character class never overlaps an escape sequence, so a
# plain grep of the contiguous run works without stripping the escapes first.
extract_oauth_token() {
    grep -oaE 'sk-ant-oat01-[A-Za-z0-9_-]+' "$1" 2>/dev/null | tail -n 1
}

# Store a freshly minted token $1 into env-file $2, preserving history:
#   * comment out any existing *active* `CLAUDE_CODE_OAUTH_TOKEN=…` line (kept as a record, not lost),
#   * drop any placeholder *comment* stub (`# CLAUDE_CODE_OAUTH_TOKEN =`, or a `<…>` placeholder),
#   * append the new active line.
# Other lines (ANTHROPIC_API_KEY, GH_TOKEN, blanks, unrelated comments) are left untouched. Atomic
# replace, private perms (the file holds a live credential). Returns nonzero if it can't be written.
store_oauth_token() {
    _sot_token=$1
    _sot_file=$2
    [ -n "$_sot_file" ] || return 1
    umask 077
    mkdir -p "$(dirname "$_sot_file")" || return 1
    _sot_tmp=$(mktemp "$_sot_file.XXXXXX") || return 1
    if [ -f "$_sot_file" ]; then
        # 1) comment out an active assignment — a leading '#' means the line no longer starts with
        #    the bare var name, so an already-commented real token (with an sk-ant-… value) is left
        #    as-is; then 2) drop placeholder comment stubs (an empty or `<…>` value).
        sed -E 's/^([[:space:]]*)CLAUDE_CODE_OAUTH_TOKEN=/\1# CLAUDE_CODE_OAUTH_TOKEN=/' "$_sot_file" \
            | grep -vE '^[[:space:]]*#[[:space:]]*CLAUDE_CODE_OAUTH_TOKEN[[:space:]]*=[[:space:]]*(<[^>]*>)?[[:space:]]*$' \
            > "$_sot_tmp" || true # grep exits 1 when it filters every line — that's fine
    fi
    printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$_sot_token" >> "$_sot_tmp" || {
        rm -f "$_sot_tmp"
        return 1
    }
    mv "$_sot_tmp" "$_sot_file" || {
        rm -f "$_sot_tmp"
        return 1
    }
    chmod 600 "$_sot_file" 2>/dev/null || true
}

# True when URL $1 names github.com as its host, in either form the repo's `git_url` is stored:
# HTTPS (`https://github.com/owner/repo.git`) or SSH (`git@github.com:owner/repo.git`). An empty or
# non-GitHub URL (incl. a GitHub Enterprise host) returns nonzero.
is_github_url() {
    case "$1" in
        *github.com/*|*github.com:*) return 0 ;;
        *) return 1 ;;
    esac
}

# True when env-file $2 exists and holds an *active* (uncommented) `$1=` assignment. A commented
# (`# $1=…`) line or a missing file returns nonzero — matching store_oauth_token's notion of active.
env_file_has_var() {
    [ -f "$2" ] && grep -qE "^[[:space:]]*$1=" "$2"
}

# Create env-file $1 as an empty private (0600) file (making its parent dir) when it doesn't already
# exist; a no-op when it does. Used when switching a repo to a fresh repo-specific credentials file:
# the file must exist before the task service will point the repo at it (it validates env_file
# exists). Returns nonzero if it can't be created.
ensure_private_env_file() {
    _epef_file=$1
    [ -n "$_epef_file" ] || return 1
    umask 077
    mkdir -p "$(dirname "$_epef_file")" || return 1
    [ -f "$_epef_file" ] || : > "$_epef_file" || return 1
    chmod 600 "$_epef_file" 2>/dev/null || true
}

# Point repo $2's env_file at name $3 via the task service at $1 (PATCH /repos/<id>). The name is
# relative to the secrets dir (ADR 0007); the file must already exist (the service validates it —
# see ensure_private_env_file). Returns curl's exit status (nonzero on an HTTP error). Needs `curl`.
set_repo_env_file() {
    _sref_url=$1
    _sref_id=$2
    _sref_name=$3
    [ -n "$_sref_url" ] && [ -n "$_sref_id" ] || return 1
    curl --silent --show-error --fail --request PATCH \
        "$_sref_url/repos/$_sref_id" \
        --header "Content-Type: application/json" \
        --data "{\"env_file\": \"$_sref_name\"}" >/dev/null
}

# Append `$1=$2` to env-file $3, creating it (and its parent dir) private (0600) if needed. Adds a
# separating newline first when the file is non-empty and its last byte isn't one, so the appended
# line always stands alone. Only ever called when the var is absent (see env_file_has_var), so it
# needs no comment-out logic. Returns nonzero if it can't be written.
append_env_var() {
    _aev_var=$1
    _aev_val=$2
    _aev_file=$3
    [ -n "$_aev_file" ] || return 1
    umask 077
    mkdir -p "$(dirname "$_aev_file")" || return 1
    # `$(…)` strips trailing newlines, so a non-empty result means the last byte isn't a newline.
    if [ -s "$_aev_file" ] && [ -n "$(tail -c 1 "$_aev_file")" ]; then
        printf '\n' >> "$_aev_file" || return 1
    fi
    printf '%s=%s\n' "$_aev_var" "$_aev_val" >> "$_aev_file" || return 1
    chmod 600 "$_aev_file" 2>/dev/null || true
}
