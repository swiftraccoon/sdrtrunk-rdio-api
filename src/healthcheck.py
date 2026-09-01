"""Container health probe that follows the configured HTTP/TLS endpoint."""

import argparse
import ssl
import sys
import urllib.request

from .config import Config


def check_health(config_path: str) -> bool:
    """Return whether the configured local health endpoint reports success."""
    config = Config.load_from_file(config_path, require_exists=True)
    scheme = "https" if config.server.ssl_cert else "http"
    health_path = config.monitoring.health_check.path
    url = f"{scheme}://localhost:{config.server.port}{health_path}"

    context: ssl.SSLContext | None = None
    if config.server.ssl_cert:
        # Trust the exact configured certificate/chain. Hostname matching is
        # intentionally disabled only for this loopback probe because the
        # public certificate normally names the deployment host, not localhost.
        context = ssl.create_default_context(cafile=config.server.ssl_cert)
        context.check_hostname = False  # nosec B501

    handlers: list[urllib.request.BaseHandler] = [urllib.request.ProxyHandler({})]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    # The URL has a fixed HTTP(S) scheme, loopback host, and no proxy handler.
    with opener.open(url, timeout=5) as response:  # nosec B310
        return 200 <= int(response.status) < 300


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/config.yaml")
    args = parser.parse_args()
    try:
        return 0 if check_health(args.config) else 1
    except Exception as exc:
        print(f"healthcheck failed ({type(exc).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
