"""``panopticon request-mix``: classifying the task service's access log."""

from __future__ import annotations

from pathlib import Path

import pytest

from panopticon.terminal.__main__ import main
from panopticon.terminal.request_mix import render, tally

_ID = "8460baaa6b7f457eb947f080e4927aad"
_OTHER = "0123456789abcdef0123456789abcdef"

_LOG = [
    'INFO:     172.17.0.5:43210 - "GET /tasks?since=41&wait=2.0 HTTP/1.1" 200 OK',
    'INFO:     172.17.0.5:43211 - "GET /tasks?since=0 HTTP/1.1" 200 OK',
    'INFO:     172.17.0.5:43212 - "GET /tasks HTTP/1.1" 200 OK',
    f'INFO:     172.17.0.6:5000 - "PUT /tasks/{_ID}/turn HTTP/1.1" 200 OK',
    f'INFO:     172.17.0.7:5000 - "PUT /tasks/{_OTHER}/turn HTTP/1.1" 200 OK',
    f'INFO:     172.17.0.7:5000 - "PUT /tasks/{_OTHER}/lifecycle HTTP/1.1" 500 Internal Server Error',
    f'2026-10-22 21:00:00,000 INFO uvicorn.access: 1.2.3.4:1 - "GET /tasks/{_ID} HTTP/1.1" 200',
    "2026-10-22 21:00:00,000 INFO panopticon.taskservice.service: task x: slug → y",
]


def test_tally_splits_writes_long_polls_and_full_reads() -> None:
    mix = tally(_LOG)
    assert (mix.writes, mix.long_polls, mix.full_reads, mix.other) == (3, 1, 2, 1)
    assert mix.errors == 1
    # Ids fold out of the route, so the chattiest writer tallies as one endpoint.
    assert mix.write_endpoints.most_common(1) == [("PUT /tasks/{id}/turn", 2)]


def test_render_reports_shares_and_busiest_writes() -> None:
    text = render(tally(_LOG))
    assert "writes            3   42.9%" in text
    assert "PUT /tasks/{id}/turn" in text
    assert render(tally([])).startswith("requests            0")  # an empty log doesn't divide by 0


def test_cli_reads_the_log(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log = tmp_path / "service.log"
    log.write_text("\n".join(_LOG))
    assert main(["request-mix", str(log)]) == 0
    assert "long-polls        1" in capsys.readouterr().out
    assert main(["request-mix", str(tmp_path / "missing.log")]) == 1
