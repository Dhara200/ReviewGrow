"""Amazon SES v2 delivery and safe email-template rendering."""

import logging
import re
import uuid
from email.utils import formataddr, parseaddr
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from app.config import Config


logger = logging.getLogger(__name__)
_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_RETRYABLE_CODES = {
    "AccountThrottled", "InternalFailure", "LimitExceededException",
    "RequestTimeout", "ServiceUnavailable", "ThrottlingException",
    "TooManyRequestsException",
}
_template_environment = Environment(
    loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "email_templates"),
    autoescape=select_autoescape(("html", "xml")),
    undefined=StrictUndefined,
)


def _create_ses_client():
    # Lazy import keeps template rendering and SES-disabled development usable
    # before optional runtime dependencies are installed.
    import boto3
    return boto3.client("sesv2", region_name=Config.AWS_REGION)


class EmailDeliveryError(RuntimeError):
    """A sanitized delivery error whose retryability is safe to persist."""

    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = bool(retryable)


def mask_email(address):
    local, separator, domain = (address or "").partition("@")
    if not separator:
        return "[invalid]"
    visible = local[:1]
    return f"{visible}{'*' * max(3, len(local) - 1)}@{domain}"


def _valid_email(address):
    parsed = parseaddr(address or "")[1]
    return parsed == address and bool(_EMAIL_PATTERN.fullmatch(address or ""))


def render_email(template_name, template_data):
    if not template_name or "/" in template_name or "\\" in template_name:
        raise EmailDeliveryError("Invalid email template name.", retryable=False)
    try:
        html = _template_environment.get_template(f"{template_name}.html").render(
            **template_data
        )
        text = _template_environment.get_template(f"{template_name}.txt").render(
            **template_data
        )
    except Exception as error:
        raise EmailDeliveryError(
            f"Email template rendering failed ({error.__class__.__name__}).",
            retryable=False,
        ) from None
    return html, text


def send_email(recipient_email, subject, html_content, text_content, *, client=None):
    sender = Config.SES_FROM_EMAIL
    if not _valid_email(recipient_email):
        raise EmailDeliveryError("Recipient email is invalid.", retryable=False)
    if not _valid_email(sender):
        raise EmailDeliveryError("SES sender configuration is invalid.", retryable=False)
    if not subject or not html_content or not text_content:
        raise EmailDeliveryError("Email content is incomplete.", retryable=False)

    masked_recipient = mask_email(recipient_email)
    if not Config.SES_ENABLED:
        message_id = f"mock-ses-disabled-{uuid.uuid4().hex}"
        logger.info(
            "SES delivery disabled: recipient=%s message_id=%s",
            masked_recipient, message_id,
        )
        return message_id

    request = {
        "FromEmailAddress": formataddr((Config.SES_FROM_NAME, sender)),
        "Destination": {"ToAddresses": [recipient_email]},
        "Content": {
            "Simple": {
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {
                    "Html": {"Data": html_content, "Charset": "UTF-8"},
                    "Text": {"Data": text_content, "Charset": "UTF-8"},
                },
            }
        },
    }
    if Config.SES_REPLY_TO_EMAIL:
        if not _valid_email(Config.SES_REPLY_TO_EMAIL):
            raise EmailDeliveryError("SES reply-to configuration is invalid.", retryable=False)
        request["ReplyToAddresses"] = [Config.SES_REPLY_TO_EMAIL]
    if Config.SES_CONFIGURATION_SET:
        request["ConfigurationSetName"] = Config.SES_CONFIGURATION_SET

    try:
        ses = client or _create_ses_client()
        response = ses.send_email(**request)
        message_id = response.get("MessageId")
        if not message_id:
            raise EmailDeliveryError("SES returned no MessageId.", retryable=True)
        return message_id
    except EmailDeliveryError:
        raise
    except Exception as error:
        try:
            from botocore.exceptions import BotoCoreError, ClientError
        except ImportError:
            BotoCoreError = ClientError = ()
        if isinstance(error, ClientError):
            details = error.response.get("Error", {})
            code = details.get("Code", "ClientError")
            status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
            raise EmailDeliveryError(
                f"SES delivery failed ({code}).",
                retryable=code in _RETRYABLE_CODES or status >= 500,
            ) from None
        if isinstance(error, BotoCoreError):
            raise EmailDeliveryError(
                f"SES transport failed ({error.__class__.__name__}).", retryable=True
            ) from None
        raise EmailDeliveryError(
            f"SES delivery failed ({error.__class__.__name__}).", retryable=True
        ) from None


def send_queued_email(job, *, client=None):
    template_data = dict(job.get("template_data") or {})
    subject = template_data.pop("subject", None)
    html, text = render_email(job["template_name"], template_data)
    return send_email(job["recipient_email"], subject, html, text, client=client)
