"""Regression tests for proxy resolution and rate-limit identities."""

import logging
from types import SimpleNamespace

from fastapi import FastAPI, Request

from src.middleware.rate_limiter import (
    _SlowAPIRateLimitWarningFilter,
    get_client_identifier,
)
from src.utils.network import get_client_ip, network_abuse_identity


def _request(peer: str = "192.0.2.10", forwarded_for: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode("ascii")))
    app = FastAPI()
    app.state.config = SimpleNamespace(security=SimpleNamespace(trusted_proxies=[]))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
            "client": (peer, 12345),
            "app": app,
        }
    )


def test_untrusted_peer_cannot_spoof_forwarded_for() -> None:
    request = _request(forwarded_for="198.51.100.25")

    assert get_client_ip(request, ["10.0.0.0/8"]) == "192.0.2.10"


def test_forwarded_chain_stops_at_first_untrusted_hop_from_right() -> None:
    request = _request(
        peer="10.0.0.2",
        forwarded_for="203.0.113.99, 198.51.100.25, 10.0.0.1",
    )

    # 203.0.113.99 is attacker-controlled text prepended to a header. The
    # first untrusted address reached from the trusted proxy edge is the real
    # client, 198.51.100.25.
    assert get_client_ip(request, ["10.0.0.0/8"]) == "198.51.100.25"


def test_malformed_forwarded_chain_fails_closed() -> None:
    request = _request(peer="10.0.0.2", forwarded_for="not-an-ip")

    assert get_client_ip(request, ["10.0.0.0/8"]) == "10.0.0.2"


def test_duplicate_forwarded_fields_fail_closed() -> None:
    request = _request(peer="10.0.0.2", forwarded_for="198.51.100.25")
    request.scope["headers"].append((b"x-forwarded-for", b"203.0.113.99"))

    assert get_client_ip(request, ["10.0.0.0/8"]) == "10.0.0.2"


def test_arbitrary_api_key_header_does_not_select_rate_limit_bucket() -> None:
    first = _request()
    second = _request()
    first.scope["headers"].append((b"x-api-key", b"attacker-choice-one"))
    second.scope["headers"].append((b"x-api-key", b"attacker-choice-two"))

    assert get_client_identifier(first) == get_client_identifier(second)


def test_ipv6_privacy_addresses_share_a_64_bit_rate_bucket() -> None:
    first = _request(peer="2001:db8:abcd:1234::1")
    second = _request(peer="2001:db8:abcd:1234:ffff::2")

    assert get_client_identifier(first) == "client:2001:db8:abcd:1234::/64"
    assert get_client_identifier(first) == get_client_identifier(second)


def test_trusted_proxy_ipv6_clients_share_network_abuse_identity() -> None:
    first = _request(peer="10.0.0.2", forwarded_for="2001:db8:abcd:1234::1")
    second = _request(peer="10.0.0.2", forwarded_for="2001:db8:abcd:1234:ffff::2")

    first_client = get_client_ip(first, ["10.0.0.0/8"])
    second_client = get_client_ip(second, ["10.0.0.0/8"])
    assert network_abuse_identity(first_client) == "2001:db8:abcd:1234::/64"
    assert network_abuse_identity(first_client) == network_abuse_identity(second_client)


def test_mapped_ipv4_and_scoped_ipv6_cannot_multiply_abuse_identities() -> None:
    assert network_abuse_identity("::ffff:192.0.2.1") == network_abuse_identity(
        "192.0.2.1"
    )
    assert network_abuse_identity("fe80::1%en0") == network_abuse_identity(
        "fe80::2%en1"
    )


def test_valid_api_key_uses_one_bucket_across_client_ips() -> None:
    first = _request(peer="192.0.2.10")
    second = _request(peer="198.51.100.20")
    security = SimpleNamespace(
        trusted_proxies=[],
        api_keys=[
            SimpleNamespace(
                key="valid-secret-key", identifier="scanner", allowed_ips=[]
            )
        ],
    )
    first.app.state.config.security = security
    second.app.state.config.security = security
    first.scope["headers"].append((b"x-api-key", b"valid-secret-key"))
    second.scope["headers"].append((b"x-api-key", b"valid-secret-key"))

    assert get_client_identifier(first) == "authenticated:scanner"
    assert get_client_identifier(first) == get_client_identifier(second)


def test_slowapi_filter_samples_only_routine_429_warnings() -> None:
    now = [0.0]
    log_filter = _SlowAPIRateLimitWarningFilter(
        maximum_per_window=2,
        window_seconds=60.0,
        clock=lambda: now[0],
    )
    routine_warning = logging.LogRecord(
        name="slowapi",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="ratelimit %s (%s) exceeded at endpoint: %s",
        args=("1/minute", "client:192.0.2.1", "endpoint"),
        exc_info=None,
    )

    assert log_filter.filter(routine_warning)
    assert log_filter.filter(routine_warning)
    assert not log_filter.filter(routine_warning)

    # Storage/backend errors and unrelated warnings must never be hidden.
    error = logging.LogRecord(
        name="slowapi",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="rate-limit storage failed",
        args=(),
        exc_info=None,
    )
    unrelated_warning = logging.LogRecord(
        name="slowapi",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="different warning",
        args=(),
        exc_info=None,
    )
    assert log_filter.filter(error)
    assert log_filter.filter(unrelated_warning)

    now[0] = 61.0
    assert log_filter.filter(routine_warning)
