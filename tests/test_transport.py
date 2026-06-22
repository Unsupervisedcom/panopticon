"""The service-URL transport selector: TCP base URL vs. ``unix://<path>`` socket.

Unit tests pin the ``unix://`` parsing and which httpx transport each URL shape yields; the
end-to-end proof that a socket-bound service actually answers over the client lives in
``test_server.py``. No LLM, no Docker.
"""

from __future__ import annotations

from panopticon.transport import is_socket_url, make_http_client, socket_path


def test_is_socket_url_distinguishes_the_two_shapes() -> None:
    assert is_socket_url("unix:///run/panopticon.sock")
    assert not is_socket_url("http://localhost:8000")
    assert not is_socket_url("http://host.docker.internal:8000")


def test_socket_path_strips_the_scheme() -> None:
    assert socket_path("unix:///run/panopticon.sock") == "/run/panopticon.sock"
    assert socket_path("unix://relative.sock") == "relative.sock"


def test_make_http_client_keeps_a_tcp_base_url() -> None:
    client = make_http_client("http://svc:8000")
    assert str(client.base_url) == "http://svc:8000"


def test_make_http_client_uses_a_uds_transport_for_a_socket_url() -> None:
    client = make_http_client("unix:///run/panopticon.sock")
    # The request URL's authority is irrelevant over a socket (the transport picks the
    # connection), so a socket-backed client carries the dummy authority — proof it took the
    # UDS branch rather than treating "unix://…" as an ordinary host.
    assert str(client.base_url) == "http://panopticon"
