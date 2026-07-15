# Repo hooks — running a script before a task's container starts

A **repo hook** is an executable script the runner runs **on the host**, once per spawn, after it
has prepared a task's workspace but **before** it builds the image and starts the container. Use it
to shape the checkout the agent is about to see — for example, strip host-only config files, drop
in fixtures, or record build state.

It is a per-repo setting: a repo's **`hook_file`** names the script. `None` (the default) means the
repo has no hook.

> **Not to be confused with the in-container `claude` hooks.** panopticon also wires claude's own
> `Stop` / `UserPromptSubmit` hooks *inside* the container to track whose turn it is
> (`container/hooks.py`). Those are an internal mechanism, run in the container, and are unrelated
> to repo hooks. This doc is only about repo hooks — the host-side, pre-launch script.

## When it runs

For each task the runner spawns (the Docker container path), in order:

1. Prepare the per-task workspace — a `git clone --local` of the repo, mounted at `/workspace`.
2. **Run the repo hook** (if the repo has one).
3. Compose the task image (base → workflow → repo) and `docker run`.

The hook runs on **whichever host runs the task** (the session service / runner), not inside the
container — the container does not exist yet. Like a repo's `env_file`, the hook is resolved
against that host's own config, so a remote runner uses its own copy of the script.

## The execution contract

- **Working directory:** the per-task workspace checkout, so relative paths in the hook resolve
  against the code the agent will work on.
- **Environment:** the runner's environment, plus:
  - `PANOPTICON_TASK_ID` — the task's id.
  - `PANOPTICON_REPO_NAME` — the repo's name.
  - `PANOPTICON_WORKSPACE` — the absolute path to the checkout (same as the cwd).
- **A nonzero exit aborts the spawn.** The hook is a gate: if it fails, the container is never
  started and the task surfaces as `failed` (with the hook's exit status in the detail).
- **A missing or non-executable script is silently skipped.** This lets you register a `hook_file`
  before the script exists (or `chmod -x` it to disable it) without breaking spawns.

## Where the script lives

A `hook_file` is a **name relative to the runner's hooks directory** —
`$PANOPTICON_CONFIG/hooks` (by default `~/.config/panopticon/hooks/`). This mirrors how a repo's
`env_file` resolves against the secrets dir: the stored value is just a name, and each runner
resolves it against its **own** host's hooks dir, so the value stays host-agnostic and works for
remote runners. Names that escape the hooks dir (an absolute path, or `..`) are rejected.

So, to add a hook:

1. **Write the script** under the hooks dir and make it executable:

   ```sh
   mkdir -p ~/.config/panopticon/hooks
   $EDITOR ~/.config/panopticon/hooks/strip-host-config.sh
   chmod +x ~/.config/panopticon/hooks/strip-host-config.sh
   ```

   If you run tasks on more than one host, put the script on each host that will run this repo.

2. **Point the repo's `hook_file` at its name.** The dashboard repo form doesn't expose
   `hook_file` yet, so set it over the REST API — its **name**, not a full path:

   ```sh
   gh api --method PATCH /repos/<repo-id> --input - <<'JSON'
   { "id": "<repo-id>", "hook_file": "strip-host-config.sh" }
   JSON
   ```

   or with `curl` against the task service:

   ```sh
   curl --request PATCH "$PANOPTICON_SERVICE_URL/repos/<repo-id>" \
     --header 'content-type: application/json' \
     --data '{"id": "<repo-id>", "hook_file": "strip-host-config.sh"}'
   ```

   Clear it by setting `hook_file` back to `null`.

## Example hook

A hook that removes a host-only settings file so it never reaches the agent's checkout:

```sh
#!/usr/bin/env bash
# ~/.config/panopticon/hooks/strip-host-config.sh
set -euo pipefail

# cwd is the task's checkout ($PANOPTICON_WORKSPACE).
rm -f .env.local config/host-only.yaml

echo "prepared workspace for task $PANOPTICON_TASK_ID ($PANOPTICON_REPO_NAME)"
```

`stdout`/`stderr` go to the runner's logs. Exit nonzero to abort the spawn.

## Troubleshooting

- **The task went straight to `failed` after preparing.** The hook exited nonzero; check the
  runner's logs for the `repo hook … exited <code>` message and the script's own output.
- **The hook didn't run.** The script is missing or not executable on the host that ran the task,
  or the repo's `hook_file` is unset. A missing/non-executable script is skipped silently by
  design — confirm the path (`$PANOPTICON_CONFIG/hooks/<name>`) and `chmod +x`.
- **The spawn failed with "escapes the hooks dir".** `hook_file` must be a plain name under the
  hooks dir — not an absolute path or one containing `..`.
