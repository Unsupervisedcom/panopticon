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

# Store a freshly minted value $2 for env var $1 into env-file $3, preserving history:
#   * comment out any existing *active* `<VAR>=…` line (kept as a record, not lost),
#   * drop any placeholder *comment* stub (`# <VAR> =`, or a `<…>` placeholder),
#   * append the new active line (on its own line even if the file lacked a trailing newline).
# Other lines (a different var, blanks, unrelated comments) are left untouched. Atomic replace,
# private perms (the file holds a live credential). Returns nonzero if it can't be written. Shared by
# every token the setup flow writes (CLAUDE_CODE_OAUTH_TOKEN, GH_TOKEN, …). $1 must be a plain env
# var name (`[A-Za-z_][A-Za-z0-9_]*`) — it's interpolated verbatim into the sed/grep patterns, where
# it carries no regex metacharacters.
store_env_token() {
    _set_var=$1
    _set_token=$2
    _set_file=$3
    [ -n "$_set_file" ] || return 1
    umask 077
    mkdir -p "$(dirname "$_set_file")" || return 1
    _set_tmp=$(mktemp "$_set_file.XXXXXX") || return 1
    if [ -f "$_set_file" ]; then
        # 1) comment out an active assignment — a leading '#' means the line no longer starts with
        #    the bare var name, so an already-commented real value is left as-is; then 2) drop
        #    placeholder comment stubs (an empty or `<…>` value).
        sed -E "s/^([[:space:]]*)${_set_var}=/\\1# ${_set_var}=/" "$_set_file" \
            | grep -vE "^[[:space:]]*#[[:space:]]*${_set_var}[[:space:]]*=[[:space:]]*(<[^>]*>)?[[:space:]]*\$" \
            > "$_set_tmp" || true # grep exits 1 when it filters every line — that's fine
    fi
    # Ensure the appended line stands alone even if the kept content didn't end in a newline.
    if [ -s "$_set_tmp" ] && [ -n "$(tail -c 1 "$_set_tmp")" ]; then
        printf '\n' >> "$_set_tmp" || {
            rm -f "$_set_tmp"
            return 1
        }
    fi
    printf '%s=%s\n' "$_set_var" "$_set_token" >> "$_set_tmp" || {
        rm -f "$_set_tmp"
        return 1
    }
    mv "$_set_tmp" "$_set_file" || {
        rm -f "$_set_tmp"
        return 1
    }
    chmod 600 "$_set_file" 2>/dev/null || true
}

# Back-compat wrapper: store a Claude OAuth token. The shared implementation lives in
# store_env_token; this keeps the Claude call site (and its tests) reading clearly.
store_oauth_token() {
    store_env_token CLAUDE_CODE_OAUTH_TOKEN "$1" "$2"
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

# A human label for the repo's source, from its git URL $1 — drives the setup flow's opening summary.
# A GitHub remote (the case that wants a GH_TOKEN); a filesystem path / file:// URL, or a bare ref
# with neither a scheme nor an scp-style host, is a local checkout; anything else is a generic remote.
repo_source_label() {
    if [ -z "$1" ]; then
        printf 'unknown'
    elif is_github_url "$1"; then
        printf 'GitHub remote'
    elif printf '%s' "$1" | grep -qE '^(/|\./|\.\./|~|file://)' \
        || ! printf '%s' "$1" | grep -qE '://|@'; then
        printf 'local checkout'
    else
        printf 'remote'
    fi
}

# True when env-file $2 exists and holds an *active* (uncommented) `$1=` assignment. A commented
# (`# $1=…`) line or a missing file returns nonzero — matching store_env_token's notion of active.
env_file_has_var() {
    [ -f "$2" ] && grep -qE "^[[:space:]]*$1=" "$2"
}

# Print a masked tail of secret $1 for a consent prompt — the last 4 characters, e.g. `...a1b2`, so
# the operator can confirm *which* token without it being shown in full. A value of 4 chars or fewer
# collapses to just `...` (nothing safe to reveal).
mask_last4() {
    if [ "${#1}" -gt 4 ]; then
        printf '...%s' "$(printf '%s' "$1" | tail -c 4)"
    else
        printf '...'
    fi
}
