"""Network-address helpers shared by authentication and rate limiting."""

from __future__ import annotations

from collections.abc import Iterable
from ipaddress import IPv6Address, ip_address, ip_network

from fastapi import Request

_MAX_FORWARDED_FOR_LENGTH = 2048
_MAX_PROXY_HOPS = 32


def network_abuse_identity(address: str) -> str:
    """Collapse one network origin to a stable rate/admission identity.

    IPv6 privacy addresses are routinely rotated within a delegated prefix.
    Treating each address as a separate client would let one ordinary /64
    bypass per-client request and concurrency limits. Non-IP peer names occur
    only in embedded ASGI/test transports and remain bounded.
    """
    try:
        parsed = ip_address(address)
    except ValueError:
        return address[:256]
    if isinstance(parsed, IPv6Address):
        # Dual-stack listeners and proxies may spell one IPv4 origin either
        # natively or as an IPv4-mapped IPv6 address. They must share a bucket.
        if parsed.ipv4_mapped is not None:
            return str(parsed.ipv4_mapped)
        # A link-local scope identifies a local interface, not a remote abuse
        # principal. Drop it explicitly before deriving the network bucket so
        # varying zone strings cannot multiply admission identities.
        if parsed.scope_id is not None:
            parsed = IPv6Address(int(parsed))
        return ip_network(f"{parsed}/64", strict=False).with_prefixlen
    return str(parsed)


def _is_trusted_proxy(address: str, trusted_proxies: Iterable[str]) -> bool:
    """Return whether an address matches an exact host or configured IP network."""
    for configured_proxy in trusted_proxies:
        configured_proxy = configured_proxy.strip()
        if not configured_proxy:
            continue
        if address == configured_proxy:
            return True
        try:
            if ip_address(address) in ip_network(configured_proxy, strict=False):
                return True
        except ValueError:
            # Non-IP peer names are useful to ASGI test transports. They only
            # match exactly and never participate in a CIDR comparison.
            continue
    return False


def get_client_ip(request: Request, trusted_proxies: Iterable[str] = ()) -> str:
    """Resolve the originating client without trusting attacker-supplied hops.

    ``X-Forwarded-For`` is considered only when the direct peer is trusted. The
    chain is then walked from right to left, stopping at the first untrusted
    address. This handles proxy-appended headers without accepting a spoofed
    leftmost value.
    """
    direct_ip = request.client.host if request.client else "unknown"
    trusted = tuple(trusted_proxies)
    if not trusted or not _is_trusted_proxy(direct_ip, trusted):
        return direct_ip

    # Multiple field-lines create parser/proxy differentials (some components
    # select the first, others append). Accept one canonical field only.
    forwarded_fields = request.headers.getlist("x-forwarded-for")
    if len(forwarded_fields) != 1:
        return direct_ip
    forwarded_for = forwarded_fields[0]
    if not forwarded_for or len(forwarded_for) > _MAX_FORWARDED_FOR_LENGTH:
        return direct_ip

    forwarded_hops = [hop.strip() for hop in forwarded_for.split(",")]
    if (
        not forwarded_hops
        or len(forwarded_hops) > _MAX_PROXY_HOPS
        or any(not hop for hop in forwarded_hops)
    ):
        return direct_ip

    # Forwarded client/proxy entries must be literal IP addresses. A malformed
    # chain fails closed to the transport peer rather than becoming a bucket or
    # access-control identifier chosen by the caller.
    try:
        forwarded_hops = [str(ip_address(hop)) for hop in forwarded_hops]
    except ValueError:
        return direct_ip

    resolved_ip = direct_ip
    for hop in reversed(forwarded_hops):
        if not _is_trusted_proxy(resolved_ip, trusted):
            break
        resolved_ip = hop
    return resolved_ip
