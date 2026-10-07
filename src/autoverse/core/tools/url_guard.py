"""URL safety guard for outbound network tools (docs/reference/tools.md §12, SSRF hardening).

`check_url` is a pure predicate used by `web_fetch` / `browser_navigate` before
any URL is handed to a backend. It rejects:
  - non-http(s) schemes (notably ``file://``, ``ftp://``, ``gopher://`` …);
  - hosts that resolve to a loopback / private / link-local / reserved address
    (127.0.0.0/8, 10/8, 172.16/12, 192.168/16, 169.254/16, ::1, fc00::/7 …),
    which is the classic SSRF pivot toward cloud metadata and internal services.

It is a pure function (no DNS lookups) so it stays testable and side-effect
free; literal IP hosts are checked directly, and obvious local hostnames are
rejected by name. Returns (ok, reason): reason is empty when ok is True.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_LOOPBACK_HOSTNAMES = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})
_BLOCKED_HOSTNAMES = frozenset({"metadata.google.internal"})
_BLOCKED_HOST_SUFFIXES = (".local", ".internal", ".lan")


def _literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse canonical or legacy numeric IP spelling without doing DNS."""

    candidate = host
    # Strip IPv6 brackets, e.g. "[::1]" -> "::1".
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        try:
            return ipaddress.ip_address(socket.inet_aton(candidate))
        except OSError:
            return None


def _host_is_blocked_ip(host: str) -> bool:
    """True if `host` is a literal IP in a loopback/private/reserved range."""

    ip = _literal_ip(host)
    if ip is None:
        return False
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def check_url(url: str, *, allow_loopback: bool = False) -> tuple[bool, str]:
    """Validate `url` for outbound use. Returns (ok, reason).

    Native, user-visible browser hosts may opt into literal loopback and the
    conventional ``localhost`` names for local application testing.  Private,
    link-local, reserved, multicast and metadata destinations remain blocked.
    """
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        return (
            False,
            f"scheme {scheme or '(none)'!r} is not allowed; only http/https are "
            "permitted (file://, ftp:// and similar are blocked).",
        )
    host = (parsed.hostname or "").lower()
    if not host:
        return (False, "URL has no host.")
    is_named_loopback = host in _LOOPBACK_HOSTNAMES or host.endswith(".localhost")
    if is_named_loopback:
        if allow_loopback:
            return (True, "")
        return (False, f"host {host!r} is a loopback address; blocked.")
    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_HOST_SUFFIXES):
        return (
            False,
            f"host {host!r} is an internal-network name; blocked to prevent "
            "access to internal services and cloud metadata (SSRF).",
        )
    if _host_is_blocked_ip(host):
        if allow_loopback:
            ip = _literal_ip(host)
            if ip is not None and ip.is_loopback:
                return (True, "")
        return (
            False,
            f"host {host!r} is a private/loopback/link-local address; blocked "
            "to prevent access to internal services and cloud metadata (SSRF).",
        )
    return (True, "")


__all__ = ["check_url"]
