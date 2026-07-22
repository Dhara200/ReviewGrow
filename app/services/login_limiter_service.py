"""Login-specific policy over the reusable limiter service."""

from dataclasses import dataclass

from app.services.limiter_service import LimiterService, LimitStatus


THROTTLED_LOGIN_MESSAGE = "Too many login attempts. Please try again later."
LOGIN_LIMITER_UNAVAILABLE_MESSAGE = (
    "Login is temporarily unavailable. Please try again later."
)


@dataclass(frozen=True)
class LoginLimiterPolicy:
    ip_threshold: int
    ip_window_seconds: int
    ip_block_seconds: int
    account_threshold: int
    account_window_seconds: int
    account_block_seconds: int
    ip_account_threshold: int
    ip_account_window_seconds: int
    ip_account_block_seconds: int

    @classmethod
    def from_config(cls, config):
        return cls(
            ip_threshold=config.get("LOGIN_IP_MAX_ATTEMPTS", 20),
            ip_window_seconds=config.get("LOGIN_IP_WINDOW_SECONDS", 900),
            ip_block_seconds=config.get("LOGIN_IP_BLOCK_SECONDS", 900),
            account_threshold=config.get("LOGIN_ACCOUNT_MAX_ATTEMPTS", 15),
            account_window_seconds=config.get("LOGIN_ACCOUNT_WINDOW_SECONDS", 900),
            account_block_seconds=config.get("LOGIN_ACCOUNT_BLOCK_SECONDS", 900),
            ip_account_threshold=config.get("LOGIN_IP_ACCOUNT_MAX_ATTEMPTS", 5),
            ip_account_window_seconds=config.get("LOGIN_IP_ACCOUNT_WINDOW_SECONDS", 900),
            ip_account_block_seconds=config.get("LOGIN_IP_ACCOUNT_BLOCK_SECONDS", 900),
        )


class LoginLimiter:
    """Coordinates the three independent login limiter identities."""

    def __init__(self, policy: LoginLimiterPolicy, limiter=None):
        self._policy = policy
        self._limiter = limiter or LimiterService()

    def check_ip(self, ip_address):
        return self._limiter.check_limit("ip", ip_address)

    def check_account_and_pair(self, email, ip_address):
        return (
            self._limiter.check_limit("account", email),
            self._limiter.check_limit("ip_account", (ip_address, email)),
        )

    def record_failure(self, email, ip_address):
        """Record in broad-to-specific order; each operation is DB-atomic."""
        return (
            self._limiter.record_failure(
                "ip", ip_address,
                threshold=self._policy.ip_threshold,
                window_seconds=self._policy.ip_window_seconds,
                block_seconds=self._policy.ip_block_seconds,
            ),
            self._limiter.record_failure(
                "account", email,
                threshold=self._policy.account_threshold,
                window_seconds=self._policy.account_window_seconds,
                block_seconds=self._policy.account_block_seconds,
            ),
            self._limiter.record_failure(
                "ip_account", (ip_address, email),
                threshold=self._policy.ip_account_threshold,
                window_seconds=self._policy.ip_account_window_seconds,
                block_seconds=self._policy.ip_account_block_seconds,
            ),
        )

    def record_ip_failure(self, ip_address):
        """Record a request with no usable account identity."""
        return self._limiter.record_failure(
            "ip", ip_address,
            threshold=self._policy.ip_threshold,
            window_seconds=self._policy.ip_window_seconds,
            block_seconds=self._policy.ip_block_seconds,
        )

    def reset_after_success(self, email, ip_address):
        account_reset = self._limiter.reset("account", email)
        pair_reset = self._limiter.reset("ip_account", (ip_address, email))
        return account_reset, pair_reset


def longest_retry_after(statuses):
    return max(
        (max(1, status.retry_after_seconds) for status in statuses if status.blocked),
        default=0,
    )


def validate_login_limiter_config(app):
    ranges = {
        "LOGIN_IP_MAX_ATTEMPTS": (1, 1000),
        "LOGIN_IP_WINDOW_SECONDS": (1, 86400),
        "LOGIN_IP_BLOCK_SECONDS": (1, 604800),
        "LOGIN_ACCOUNT_MAX_ATTEMPTS": (1, 1000),
        "LOGIN_ACCOUNT_WINDOW_SECONDS": (1, 86400),
        "LOGIN_ACCOUNT_BLOCK_SECONDS": (1, 604800),
        "LOGIN_IP_ACCOUNT_MAX_ATTEMPTS": (1, 1000),
        "LOGIN_IP_ACCOUNT_WINDOW_SECONDS": (1, 86400),
        "LOGIN_IP_ACCOUNT_BLOCK_SECONDS": (1, 604800),
    }
    for name, (minimum, maximum) in ranges.items():
        value = app.config.get(name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise RuntimeError(f"{name} must be an integer.")
        if not minimum <= value <= maximum:
            raise RuntimeError(
                f"{name} must be between {minimum} and {maximum}."
            )
