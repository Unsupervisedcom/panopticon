# panopticon

**Agents write the code, you own what ships.**

That's easy with one agent. Run a fleet of them and it breaks down: the fleet stalls
waiting on you, and you lose track of which agent is doing what. Panopticon gives you
one place to watch them all.

- **A live dashboard** of all your tasks — which agents are working, and which are blocked
  waiting on you — so you stop cycling through terminals to find the one that's stuck.
- **Configurable workflows** that set the line between what an agent may do alone and what
  needs your sign-off — so agents run unattended without running unchecked. Other tools show
  you which agent is blocked; Panopticon decides when it blocks.
- **Sandboxed by default** — each agent works in its own container on its own branch
  (secrets and environment handled per repo), so it can work freely and nothing reaches
  main without your review.

Self-hosted and terminal-native — your infrastructure, your secrets,
your repos. A ground-up rewrite of the [cloude-cade](https://github.com/tildesrc/cloude-cade)
prototype.
