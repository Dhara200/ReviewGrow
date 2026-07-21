import ipaddress

from werkzeug.middleware.proxy_fix import ProxyFix


class TrustedProxyFix:
    """Apply one-hop ProxyFix only when the immediate peer is trusted."""

    def __init__(self, app, trusted_proxy_ips):
        self.app = app
        self.proxy_app = ProxyFix(app, x_for=1)
        self.trusted_proxy_ips = frozenset(
            _normalized_ip(value) for value in trusted_proxy_ips
        )

    def __call__(self, environ, start_response):
        immediate_peer = _normalized_ip(environ.get("REMOTE_ADDR"))
        if immediate_peer in self.trusted_proxy_ips:
            return self.proxy_app(environ, start_response)
        return self.app(environ, start_response)


def _normalized_ip(value):
    try:
        return str(ipaddress.ip_address(value or ""))
    except ValueError:
        return "unknown"
