"""API-key authentication for read-only API endpoints."""

import hmac
import ipaddress
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from ..config import APIKeyConfig, Config
from ..utils.network import get_client_ip
from .keys import stable_api_key_identifier


@dataclass(frozen=True, slots=True)
class ReadPrincipal:
    """Authenticated identity and its optional system-level read scope."""

    key_id: str | None
    allowed_systems: frozenset[str] | None
    authenticated: bool = True


def _normalize_ip(value: str) -> str | None:
    """Return a canonical IP string, or ``None`` for malformed input."""
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def api_key_allows_client_ip(api_key: APIKeyConfig, client_ip: str) -> bool:
    """Apply one fail-closed IP policy for auth and rate-bucket selection."""
    if not api_key.allowed_ips:
        return True
    normalized_client = _normalize_ip(client_ip)
    if normalized_client is None:
        return False
    return normalized_client in {
        normalized
        for allowed in api_key.allowed_ips
        if (normalized := _normalize_ip(allowed)) is not None
    }


def _match_api_key(config: Config, candidate: str) -> APIKeyConfig | None:
    """Compare against every configured key without a position timing leak."""
    match: APIKeyConfig | None = None
    candidate_bytes = candidate.encode("utf-8")
    for api_key in config.security.api_keys:
        if hmac.compare_digest(api_key.key.encode("utf-8"), candidate_bytes):
            match = api_key
    return match


def _authentication_error(detail: str = "Invalid or missing API key") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "X-API-Key"},
    )


def authenticate_read_request(request: Request) -> ReadPrincipal:
    """Authenticate and authorize a query, audio, or metrics request."""
    config: Config = request.app.state.config
    credential_fields = request.headers.getlist("x-api-key")
    if len(credential_fields) > 1:
        raise _authentication_error()
    candidate = credential_fields[0] if credential_fields else None

    if not candidate:
        if config.security.allow_unauthenticated_reads:
            return ReadPrincipal(key_id=None, allowed_systems=None, authenticated=False)
        raise _authentication_error()
    if len(candidate) > 512:
        raise _authentication_error()

    matched = _match_api_key(config, candidate)
    if matched is None:
        raise _authentication_error()

    api_key = matched
    client_ip = get_client_ip(request, config.security.trusted_proxies)
    if not api_key_allows_client_ip(api_key, client_ip):
        # Keep a valid-but-location-restricted credential indistinguishable
        # from an unknown credential. A distinct 403 would turn the IP
        # allowlist into a remote key-validity oracle.
        raise _authentication_error()

    allowed_systems = (
        frozenset(api_key.allowed_systems) if api_key.allowed_systems else None
    )
    return ReadPrincipal(
        key_id=stable_api_key_identifier(api_key), allowed_systems=allowed_systems
    )
