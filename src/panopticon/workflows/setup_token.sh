echo "Running 'claude setup-token' — follow the prompts to mint a token."
echo
if claude setup-token; then
    echo
    echo "Token minted — marking this task complete."
    curl --silent --show-error --fail --request POST \
        "$PANOPTICON_SERVICE_URL/tasks/$PANOPTICON_TASK_ID/operations/advance" \
        >/dev/null \
        || echo "warning: could not mark the task complete via $PANOPTICON_SERVICE_URL"
    echo
    echo "Copy the token shown above into the repo's env-file as CLAUDE_CODE_OAUTH_TOKEN."
    printf 'Press Enter to close this session. '
    read _
else
    echo "claude setup-token failed or was cancelled — leaving the task unchanged."
fi
