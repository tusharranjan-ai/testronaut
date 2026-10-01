"""
Guards for the two places user input reaches the network or the Docker daemon.

`fetch_spec_url` is server-side request forgery waiting to happen if it is a bare
`httpx.get(url)`: the backend can reach loopback, the LAN and cloud metadata
endpoints that the person on the other end of the API cannot. So it only talks to
public addresses, and it re-checks every redirect hop.

`validate_docker_network` stops `--network host` (and `container:<id>`) arriving
from the API, which would put generated code on the host's network stack.

Set TESTRONAUT_ALLOW_PRIVATE_URLS=1 to fetch specs from private addresses, e.g.
an API on your own LAN or `localhost`. Off by default.
"""

import ipaddress
import os
import re
import socket
from typing import Optional
from urllib.parse import urljoin, urlsplit

import httpx

MAX_SPEC_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5
_NETWORK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_BLOCKED_NETWORKS = {"host", "container"}


class UnsafeURL(ValueError):
    """The URL points somewhere the backend should not fetch from."""


def _allow_private() -> bool:
    return os.getenv("TESTRONAUT_ALLOW_PRIVATE_URLS", "").lower() in {"1", "true", "yes"}


def _is_public(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified)


def check_url(url: str) -> None:
    """Raise UnsafeURL unless `url` is http(s) and every address it resolves to is public."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise UnsafeURL("Only http and https URLs are allowed.")
    if not parts.hostname:
        raise UnsafeURL("URL has no host.")
    if _allow_private():
        return
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise UnsafeURL(f"Could not resolve {parts.hostname}: {e}")
    for info in infos:
        if not _is_public(ipaddress.ip_address(info[4][0])):
            raise UnsafeURL(
                f"{parts.hostname} resolves to a private or reserved address. "
                "Set TESTRONAUT_ALLOW_PRIVATE_URLS=1 if you really mean to fetch from your own network.")


async def fetch_spec_url(url: str, client: Optional[httpx.AsyncClient] = None) -> str:
    """GET a spec, following redirects by hand so each hop is checked, with a size cap."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=30.0, follow_redirects=False)
    try:
        for _ in range(MAX_REDIRECTS + 1):
            check_url(url)
            async with client.stream("GET", url) as resp:
                if resp.is_redirect and resp.headers.get("location"):
                    url = urljoin(url, resp.headers["location"])
                    continue
                resp.raise_for_status()
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_SPEC_BYTES:
                        raise UnsafeURL(f"Spec is larger than {MAX_SPEC_BYTES // (1024 * 1024)} MB.")
                return bytes(body).decode("utf-8", errors="replace")
        raise UnsafeURL("Too many redirects.")
    finally:
        if own:
            await client.aclose()


def validate_docker_network(network: Optional[str]) -> Optional[str]:
    """Allow a plain network name (bridge, none, a user-defined one); refuse host/container modes."""
    if network is None or network == "":
        return None
    if network.lower().split(":")[0] in _BLOCKED_NETWORKS or not _NETWORK_NAME.match(network):
        raise ValueError(
            f"Network {network!r} is not allowed. Use a named Docker network, 'bridge' or 'none'.")
    return network
