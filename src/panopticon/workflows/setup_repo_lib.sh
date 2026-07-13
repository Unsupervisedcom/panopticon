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
