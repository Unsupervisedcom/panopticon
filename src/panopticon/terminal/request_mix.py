"""``panopticon request-mix`` — what the task service spends its requests on, read from its log.

The control plane's load is shaped by three kinds of request: **writes** (each one bumps the change
feed and wakes every long-poller), **long-polls** (``GET /tasks?wait=…`` — parked, then a full list
read when woken) and **full reads** (an immediate ``GET /tasks``). Their ratio says where a load
problem comes from: many writes per minute fan out into that many list reads per long-poller. This
tallies them from the uvicorn access lines in the service's log (``make start`` writes it to
``/tmp/panopticon-service.log``), plus the busiest write endpoints with ids folded out, so the
chattiest writer stands out.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

DEFAULT_LOG = "/tmp/panopticon-service.log"

#: A uvicorn access line: ``… "PUT /tasks/<id>/turn HTTP/1.1" 200 …``. Matched loosely (anywhere in
#: the line) so it reads both uvicorn's own format and one re-prefixed by ``logging.basicConfig``.
_ACCESS = re.compile(r'"(?P<method>[A-Z]+) (?P<target>\S+) HTTP/[\d.]+" (?P<status>\d{3})')

#: Path segments that are ids, not routes — folded to ``{id}`` so ``PUT /tasks/{id}/turn`` tallies
#: as one endpoint however many tasks call it. Task/registration ids are 32-hex uuids.
_ID_SEGMENT = re.compile(r"^[0-9a-f]{32}$")

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass
class RequestMix:
    writes: int = 0
    long_polls: int = 0
    full_reads: int = 0
    other: int = 0
    errors: int = 0  # 5xx responses, any kind
    write_endpoints: Counter[str] = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return self.writes + self.long_polls + self.full_reads + self.other


def _route(path: str) -> str:
    return "/".join("{id}" if _ID_SEGMENT.match(seg) else seg for seg in path.split("/"))


def tally(lines: Iterable[str]) -> RequestMix:
    """Classify every access line in ``lines`` (non-access lines are skipped)."""
    mix = RequestMix()
    for line in lines:
        match = _ACCESS.search(line)
        if match is None:
            continue
        method, target = match["method"], match["target"]
        url = urlsplit(target)
        if match["status"].startswith("5"):
            mix.errors += 1
        if method in _WRITE_METHODS:
            mix.writes += 1
            mix.write_endpoints[f"{method} {_route(url.path)}"] += 1
        elif method == "GET" and url.path == "/tasks":
            if "wait" in parse_qs(url.query):
                mix.long_polls += 1
            else:
                mix.full_reads += 1
        else:
            mix.other += 1
    return mix


def render(mix: RequestMix, *, top: int = 10) -> str:
    """The tally as a short plain-text report."""

    def share(n: int) -> str:
        return f"{100 * n / mix.total:5.1f}%" if mix.total else "    –"

    lines = [
        f"requests     {mix.total:>8}",
        f"  writes     {mix.writes:>8}  {share(mix.writes)}",
        f"  long-polls {mix.long_polls:>8}  {share(mix.long_polls)}",
        f"  full reads {mix.full_reads:>8}  {share(mix.full_reads)}",
        f"  other      {mix.other:>8}  {share(mix.other)}",
        f"5xx          {mix.errors:>8}",
    ]
    if mix.write_endpoints:
        lines.append("busiest writes:")
        lines += [f"  {n:>8}  {route}" for route, n in mix.write_endpoints.most_common(top)]
    return "\n".join(lines)


def report(path: str) -> int:
    """Print the request mix of the log at ``path``; 1 if it can't be read."""
    try:
        with open(path, encoding="utf-8", errors="replace") as log:
            print(render(tally(log)))
    except OSError as exc:
        print(f"panopticon request-mix: {exc}")
        return 1
    return 0
