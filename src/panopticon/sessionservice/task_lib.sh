# panopticon shell lib — drive the task service from a `runner_type = "shell"` workflow's script.
#
# ShellRunner loads these functions into the script's shell and exports PANOPTICON_SERVICE_URL /
# PANOPTICON_TASK_ID, so a shell workflow can drive its own task's lifecycle over REST without
# hand-rolling curl. Every function acts on the current task ($PANOPTICON_TASK_ID) and returns
# curl's exit status (nonzero on an HTTP error). POSIX sh; needs `curl` (and `sed` for escaping).

# Internal: call the current task's REST API. $1=method $2=path-under-/tasks/<id> [extra curl args].
_panopticon_api() {
    _pan_method=$1
    _pan_path=$2
    shift 2
    curl --silent --show-error --fail --request "$_pan_method" \
        "$PANOPTICON_SERVICE_URL/tasks/$PANOPTICON_TASK_ID$_pan_path" "$@"
}

# Internal: JSON-escape a value (backslash + double-quote; values are expected to be single-line).
_panopticon_json_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

# Internal: PUT a one-field JSON body. $1=path $2=field $3=raw JSON value (quoted for a string).
_panopticon_put_json() {
    _panopticon_api PUT "$1" --header "Content-Type: application/json" \
        --data "{\"$2\": $3}" >/dev/null
}

# Apply a named workflow operation (e.g. advance, drop).
panopticon_operation() { _panopticon_api POST "/operations/$1" >/dev/null; }

# Advance the task along its workflow's happy path (e.g. RUNNING → COMPLETE).
panopticon_advance() { panopticon_operation advance; }

# Drop (abandon) the task.
panopticon_drop() { panopticon_operation drop; }

# Move the task directly to a state (a free move). $1=state label.
panopticon_set_state() { _panopticon_put_json "/state" state "\"$(_panopticon_json_escape "$1")\""; }

# Set the task's human slug. $1=slug.
panopticon_set_slug() { _panopticon_put_json "/slug" slug "\"$(_panopticon_json_escape "$1")\""; }

# Record an external URL (PR, issue) on the task. $1=url.
panopticon_set_url() { _panopticon_put_json "/url" url "\"$(_panopticon_json_escape "$1")\""; }

# Mark the task blocked / unblocked (a deliberate "waiting" marker).
panopticon_block() { _panopticon_put_json "/blocked" blocked true; }
panopticon_unblock() { _panopticon_put_json "/blocked" blocked false; }

# Write a task artifact from a file. $1=artifact name $2=path to the content file.
panopticon_put_artifact() { _panopticon_api PUT "/artifacts/$1" --data-binary "@$2" >/dev/null; }
