from dataclasses import dataclass


OAUTH_EMAIL_MISMATCH_MESSAGE = (
    "The selected Google account does not match your registered ReviewGrow email. "
    "Please authenticate using your registered Google Business Profile email."
)


@dataclass(frozen=True)
class OAuthEmailValidationResult:
    allowed: bool
    admin_override: bool
    registered_email: str
    provider_email: str
    message: str = ""


def normalize_email(email):
    return (email or "").strip().lower()


def validate_oauth_email(registered_email, provider_email, user_role):
    """Validate that an OAuth identity belongs to the logged-in SaaS account.

    Admin users may intentionally connect a different provider account for
    controlled demos/support. Normal users must match exactly after trimming and
    lowercasing.
    """
    registered = normalize_email(registered_email)
    provider = normalize_email(provider_email)
    is_admin = user_role == "admin"

    if not registered or not provider:
        return OAuthEmailValidationResult(
            allowed=False,
            admin_override=False,
            registered_email=registered,
            provider_email=provider,
            message=OAUTH_EMAIL_MISMATCH_MESSAGE
        )

    if registered == provider:
        return OAuthEmailValidationResult(
            allowed=True,
            admin_override=False,
            registered_email=registered,
            provider_email=provider
        )

    if is_admin:
        return OAuthEmailValidationResult(
            allowed=True,
            admin_override=True,
            registered_email=registered,
            provider_email=provider
        )

    return OAuthEmailValidationResult(
        allowed=False,
        admin_override=False,
        registered_email=registered,
        provider_email=provider,
        message=OAUTH_EMAIL_MISMATCH_MESSAGE
    )
