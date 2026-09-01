"""Rate limiting middleware for the API."""

import hmac
import logging
import threading
import time
from collections.abc import Callable
from typing import Any, cast

from fastapi import FastAPI, Request
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.routing import Match

from ..config import Config
from ..security.auth import api_key_allows_client_ip
from ..utils.network import get_client_ip, network_abuse_identity

logger = logging.getLogger(__name__)


class _SlowAPIRateLimitWarningFilter(logging.Filter):
    """Sample only SlowAPI's routine rejection warning with fixed-size state."""

    _RATE_LIMIT_WARNING_TEMPLATE = "ratelimit %s (%s) exceeded at endpoint: %s"

    def __init__(
        self,
        *,
        maximum_per_window: int = 10,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        if maximum_per_window < 1 or window_seconds <= 0:
            raise ValueError("SlowAPI warning sample bounds must be positive")
        self._maximum = maximum_per_window
        self._window_seconds = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._window_started = clock()
        self._emitted = 0

    def filter(self, record: logging.LogRecord) -> bool:
        """Drop excess 429 warnings while preserving all other log records."""
        if not (
            record.name == "slowapi"
            and record.levelno == logging.WARNING
            and record.msg == self._RATE_LIMIT_WARNING_TEMPLATE
        ):
            return True

        now = self._clock()
        with self._lock:
            if now - self._window_started >= self._window_seconds:
                self._window_started = now
                self._emitted = 0
            if self._emitted >= self._maximum:
                return False
            self._emitted += 1
            return True


def get_client_identifier(request: Request) -> str:
    """Get a server-controlled client identifier for rate limiting.

    An unverified API-key header must never select the rate-limit bucket: a
    caller could otherwise rotate arbitrary values to bypass every limit.
    """
    config = getattr(request.app.state, "config", None)
    security = getattr(config, "security", None)
    trusted_proxies = security.trusted_proxies if security else ()
    client_ip = get_client_ip(request, trusted_proxies)

    # A *validated* header credential is a server-controlled bucket and keeps
    # one stolen key from multiplying its allowance across source IPs. Unknown
    # values never select buckets, so rotating attacker strings cannot bypass
    # the IP limit. Upload credentials remain form fields and therefore use IP.
    credential_fields = request.headers.getlist("x-api-key")
    if len(credential_fields) == 1 and security is not None:
        candidate = credential_fields[0]
        if len(candidate) <= 512:
            candidate_bytes = candidate.encode("utf-8")
            matched_key: Any | None = None
            for configured_key in getattr(security, "api_keys", ()):
                if hmac.compare_digest(
                    configured_key.key.encode("utf-8"), candidate_bytes
                ):
                    matched_key = configured_key
            if matched_key is not None and api_key_allows_client_ip(
                matched_key, client_ip
            ):
                return f"authenticated:{matched_key.identifier}"

    return f"client:{network_abuse_identity(client_ip)}"


# Create the limiter instance
limiter = Limiter(key_func=get_client_identifier)
if not any(
    isinstance(log_filter, _SlowAPIRateLimitWarningFilter)
    for log_filter in limiter.logger.filters
):
    limiter.logger.addFilter(_SlowAPIRateLimitWarningFilter())


def get_limiter() -> Limiter:
    """Get the configured limiter instance."""
    return limiter


def account_route_validation_failure(
    request: Request, *, route_aware: bool = True
) -> None:
    """Charge a validation failure exactly once.

    FastAPI validates typed path/query parameters before invoking the decorated
    endpoint, so SlowAPI's wrapper never sees those failed requests. This helper
    evaluates the same route limits for those failures. Outer ASGI metadata
    rejections pass ``route_aware=False`` because authentication has not run;
    they use a dedicated IP-only bucket that also covers unknown paths and
    cannot turn a credential header into an authentication oracle.
    """
    app = request.scope.get("app")
    if (
        app is None
        or getattr(getattr(app, "state", None), "rate_limiter", None) is None
    ):
        # RequestValidationMiddleware is reusable by small embedded/test apps
        # that did not install this service's limiter configuration.
        return
    if getattr(request.state, "_rate_limiting_complete", False) or getattr(
        request.state, "_route_validation_rate_accounted", False
    ):
        return
    request.state._route_validation_rate_accounted = True
    endpoint = None
    if route_aware:
        route = request.scope.get("route")
        endpoint = getattr(route, "endpoint", None)
    if route_aware and endpoint is None:
        routes = getattr(app, "routes", ())
        for candidate_route in routes:
            # FastAPI 0.128+ retains included routers as lightweight route
            # branches. Match their effective (prefix-aware) contexts rather
            # than stopping at a branch that has no endpoint of its own.
            effective_contexts = getattr(
                candidate_route, "effective_route_contexts", None
            )
            candidates = (
                effective_contexts()
                if callable(effective_contexts)
                else (candidate_route,)
            )
            for candidate in candidates:
                match, child_scope = candidate.matches(request.scope)
                if match == Match.FULL:
                    endpoint = child_scope.get("endpoint") or getattr(
                        candidate, "endpoint", None
                    )
                    if endpoint is not None:
                        break
            if endpoint is not None:
                break
    if endpoint is None:
        endpoint = _early_validation_rejection
    limiter._check_request_limit(request, endpoint, in_middleware=False)


# Active limit string in slowapi multi-window format. Endpoints reference
# it through get_active_limits (a callable), so slowapi re-reads the value
# configured at app creation on every request instead of baking in a
# hardcoded string at import time.
_DEFAULT_LIMITS = "600/minute;10000/hour;100000/day"
_active_limits = _DEFAULT_LIMITS


def get_active_limits() -> str:
    """Current rate limit string derived from configuration."""
    return _active_limits


def _set_active_limits(value: str) -> None:
    global _active_limits
    _active_limits = value


def _unauthenticated_client_identifier(request: Request) -> str:
    """Key pre-routing failures only by the normalized trusted client network."""
    config = getattr(request.app.state, "config", None)
    security = getattr(config, "security", None)
    trusted_proxies = security.trusted_proxies if security else ()
    client_ip = get_client_ip(request, trusted_proxies)
    return f"client:{network_abuse_identity(client_ip)}"


@limiter.limit(get_active_limits, key_func=_unauthenticated_client_identifier)
def _early_validation_rejection(request: Request) -> None:
    """Synthetic endpoint whose only purpose is pre-routing rate accounting."""


class RateLimitMiddleware:
    """Applies rate limit configuration to the shared limiter."""

    def __init__(self, app: FastAPI, config: Config):
        """Initialize rate limiting middleware.

        Args:
            app: FastAPI application instance
            config: Application configuration
        """
        self.app = app
        self.config = config

        rate_limit = config.security.rate_limit
        if rate_limit.enabled:
            limiter.enabled = True
            _set_active_limits(
                f"{rate_limit.max_requests_per_minute}/minute;"
                f"{rate_limit.max_requests_per_hour}/hour;"
                f"{rate_limit.max_requests_per_day}/day"
            )

            # Add the limiter to the app state
            app.state.limiter = limiter

            # Register the rate limit exceeded handler
            app.add_exception_handler(
                RateLimitExceeded, cast(Any, _rate_limit_exceeded_handler)
            )

            logger.info(f"Rate limiting enabled: {get_active_limits()}")
        else:
            limiter.enabled = False
            logger.info("Rate limiting disabled")
