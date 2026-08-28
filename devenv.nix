{ pkgs, ... }:

{
  # The tools panopticon shells out to, so a contributor needs none of them installed globally.
  # Everything here is something the code actually executes by name — not a general-purpose kit.
  packages = [
    pkgs.uv # every entry point is `uv run`; it also provisions the interpreter in .venv
    pkgs.tmux # the session supervisor: a task's pane, and the dashboard's `t` attach, live here
    pkgs.git # per-task clones and branches (ADR 0011)
    pkgs.gh # the github-* workflows' forge CLI
    pkgs.curl # how a shell workflow's script drives its own task over REST (task_lib.sh)
    pkgs.gnumake
  ];

  # Two deliberate omissions.
  #
  # The **container runtime** is the host's. The local runner talks to whatever daemon the machine
  # runs — Docker, or a podman shim answering to `docker` — and a client packaged here would shadow
  # that one and point at a socket which is not there. Install a runtime on the host; `make build`
  # and `make start` use it.
  #
  # **Python** is uv's. `pyproject.toml` states the interpreter range and uv resolves it into
  # `.venv`, so pinning a second one here would leave two answers to the same question. `make sync`
  # is the first command in a fresh checkout.

  # uv's wheels are built against the manylinux C++ runtime: `greenlet` (SQLAlchemy's async
  # bridge) dlopens `libstdc++.so.6`, which a Nix shell does not put on the loader path — so every
  # store-backed test failed with "the greenlet library is required" until the runtime was named.
  env.LD_LIBRARY_PATH = "${pkgs.stdenv.cc.cc.lib}/lib";

  # There are no `processes` either: panopticon *is* a process supervisor. `make start` runs the
  # task service and the host daemon in its own `-L panopticon` tmux server — the same server that
  # holds every task session, and what `make stop` tears down. Declaring those as devenv processes
  # would put two supervisors on one set of sessions, and the one that owns them is panopticon.

  # `devenv test`: the toolchain is the whole contract this shell exists to provide, so assert it.
  enterTest = ''
    set -e
    uv --version
    tmux -V
    git --version
    gh --version | head -1
    curl --version | head -1
  '';
}
