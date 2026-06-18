"""How callers reach the task service: a TCP base URL or a Unix-domain socket.

The control plane is reachable two ways, selected by the **shape of the service URL** so the
single ``PANOPTICON_SERVICE_URL`` knob covers both:

* ``http://host:port`` — the default; a TCP port (containers reach the host via
  ``host.docker.internal``).
* ``unix://<path>`` — a Unix-domain socket. Used under ``make panopticon`` where the whole
  system shares one host: no port is provisioned, and the socket file is bind-mounted into each
  task container at the **same path**, so the one ``unix://<path>`` URL is valid host-side and
  in-container alike (no ``host.docker.internal`` hop).

This is the one place that knows the ``unix://`` convention; every client builds its
:class:`httpx.Client` through :func:`make_http_client`. LLM-free.
"""

from __future__ import annotations

import httpx

#: Scheme that marks a service URL as a Unix-domain socket path (``unix://<path>``).
UNIX_SCHEME = "unix://"

#: Dummy authority for socket-backed clients — the UDS transport picks the connection, so the
#: host only fills the ``Host`` header; the path it carries is irrelevant.
_SOCKET_BASE_URL = "http://panopticon"


def is_socket_url(service_url: str) -> bool:
    """Whether ``service_url`` addresses a Unix-domain socket (``unix://<path>``)."""
    return service_url.startswith(UNIX_SCHEME)


def socket_path(service_url: str) -> str:
    """The filesystem path carried by a ``unix://<path>`` service URL."""
    return service_url[len(UNIX_SCHEME) :]


def make_http_client(service_url: str) -> httpx.Client:
    """Build the :class:`httpx.Client` for ``service_url``.

    A ``unix://<path>`` URL yields a client over a UDS transport bound to that socket; any other
    URL yields a plain base-URL client. The :class:`~panopticon.client.TaskServiceClient` wraps
    whichever one comes back — its REST surface is identical over either transport.
    """
    if is_socket_url(service_url):
        transport = httpx.HTTPTransport(uds=socket_path(service_url))
        return httpx.Client(transport=transport, base_url=_SOCKET_BASE_URL)
    return httpx.Client(base_url=service_url)
