import logging
import time
from datetime import datetime, timedelta
from urllib.parse import quote
from urllib.parse import urlencode, urlsplit, urlunsplit

import requests

from app.config import Config


GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
ACCOUNT_MANAGEMENT_BASE_URL = "https://mybusinessaccountmanagement.googleapis.com/v1"
BUSINESS_INFORMATION_BASE_URL = "https://mybusinessbusinessinformation.googleapis.com/v1"
REVIEWS_BASE_URL = "https://mybusiness.googleapis.com/v4"
MEDIA_UPLOAD_BASE_URL = "https://mybusiness.googleapis.com/upload/v1/media"
REQUIRED_GOOGLE_OAUTH_SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/business.manage"
]
logger = logging.getLogger(__name__)


class GoogleBusinessError(Exception):
    pass


class GoogleQuotaError(GoogleBusinessError):
    pass


def _safe_url(url):
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _google_error_payload(response):
    try:
        data = response.json()
    except ValueError:
        return {}, response.text[:800]

    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return error, str(error)[:800]

    return {}, str(data)[:800]


def _api_error_message(response):
    error, _ = _google_error_payload(response)
    google_status = error.get("status")
    google_message = error.get("message", "")

    if response.status_code in {401, 403}:
        if google_status == "ACCESS_TOKEN_SCOPE_INSUFFICIENT":
            return "Google permission is missing. Please reconnect Google Business Profile and approve review access."

        return (
            "Google rejected the Business Profile request. Make sure the Google account "
            "owns or manages the business profile and the Google Business Profile APIs are enabled."
        )

    if response.status_code == 404:
        return "Google Business Profile location was not found. Please reconnect and select the correct location."

    if response.status_code == 400:
        if google_message:
            return f"Google Business Profile request was invalid: {google_message}"

        return "Google Business Profile request was invalid. Please reconnect and try again."

    return "Google Business Profile API request failed. Please try again later."


def _quota_error_message(response):
    error, _ = _google_error_payload(response)
    google_message = error.get("message")
    google_status = error.get("status")
    retry_after = _retry_after_seconds(response)
    parts = ["Google API quota or rate limit was reached."]

    if google_status:
        parts.append(f"Status: {google_status}.")

    if google_message:
        parts.append(f"Google says: {google_message}")

    if retry_after:
        parts.append(f"Try again after about {retry_after} seconds.")
    else:
        parts.append("Wait a few minutes, then try again.")

    return " ".join(parts)


def _retry_after_seconds(response):
    retry_after = response.headers.get("Retry-After")

    if not retry_after:
        return None

    try:
        return max(0, int(retry_after))
    except ValueError:
        return None


def _scope_value():
    configured_scopes = (Config.GOOGLE_SCOPES or "").split()
    scopes = []

    for scope in [*REQUIRED_GOOGLE_OAUTH_SCOPES, *configured_scopes]:
        if scope and scope not in scopes:
            scopes.append(scope)

    return " ".join(scopes)


def is_google_configured():
    return all([
        Config.GOOGLE_CLIENT_ID,
        Config.GOOGLE_CLIENT_SECRET,
        Config.GOOGLE_REDIRECT_URI
    ])


def build_oauth_url(state):
    if not is_google_configured():
        raise GoogleBusinessError("Google OAuth is not configured.")

    query = urlencode({
        "client_id": Config.GOOGLE_CLIENT_ID,
        "redirect_uri": Config.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": _scope_value(),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state
    })

    return f"{GOOGLE_AUTH_URL}?{query}"


def exchange_code_for_tokens(code):
    response = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": Config.GOOGLE_CLIENT_ID,
            "client_secret": Config.GOOGLE_CLIENT_SECRET,
            "redirect_uri": Config.GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code"
        },
        timeout=20
    )

    if not response.ok:
        error, body = _google_error_payload(response)
        logger.warning(
            "Google OAuth token exchange failed: status=%s google_status=%s error=%s",
            response.status_code,
            error.get("status"),
            body
        )
        raise GoogleBusinessError("Google OAuth token exchange failed. Check the redirect URI and try connecting again.")

    data = response.json()

    if "access_token" not in data:
        raise GoogleBusinessError("Google did not return an access token.")

    expires_in = int(data.get("expires_in", 3600))
    data["token_expiry"] = datetime.utcnow() + timedelta(seconds=expires_in)

    return data


def fetch_google_account_profile(access_token):
    response = requests.get(
        "https://www.googleapis.com/oauth2/v3/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20
    )

    if not response.ok:
        logger.warning(
            "Google OAuth userinfo request failed: status=%s",
            response.status_code
        )
        return {}

    data = response.json()
    logger.info(
        "Google OAuth userinfo returned keys: %s",
        sorted(data.keys())
    )

    return {
        "email": data.get("email"),
        "email_verified": data.get("email_verified"),
        "google_oauth_account_id": data.get("sub")
    }


def fetch_google_account_email(access_token):
    return fetch_google_account_profile(access_token).get("email")


def refresh_access_token(refresh_token):
    if not refresh_token:
        raise GoogleBusinessError("Refresh token is missing. Please reconnect Google Business Profile.")

    response = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "client_id": Config.GOOGLE_CLIENT_ID,
            "client_secret": Config.GOOGLE_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token"
        },
        timeout=20
    )

    if not response.ok:
        error, body = _google_error_payload(response)
        logger.warning(
            "Google token refresh failed: status=%s google_status=%s error=%s",
            response.status_code,
            error.get("status"),
            body
        )
        raise GoogleBusinessError("Google token refresh failed. Please reconnect Google Business Profile.")

    data = response.json()
    expires_in = int(data.get("expires_in", 3600))

    return {
        "access_token": data["access_token"],
        "token_expiry": datetime.utcnow() + timedelta(seconds=expires_in),
        "scope": data.get("scope")
    }


def api_get(access_token, url, params=None):
    response = None

    for attempt in range(3):
        response = requests.get(
            url,
            params=params or {},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30
        )

        if response.status_code != 429:
            break

        retry_after = _retry_after_seconds(response)
        wait_seconds = retry_after if retry_after is not None else 2 ** attempt

        logger.warning(
            "Google Business Profile API quota response: status=429 url=%s retry_after=%s attempt=%s",
            _safe_url(response.url),
            retry_after,
            attempt + 1
        )

        if attempt < 2 and wait_seconds <= 10:
            time.sleep(wait_seconds)
            continue

        break

    if response.status_code == 429:
        raise GoogleQuotaError(_quota_error_message(response))

    if not response.ok:
        error, body = _google_error_payload(response)
        logger.warning(
            "Google Business Profile API request failed: status=%s google_status=%s url=%s error=%s",
            response.status_code,
            error.get("status"),
            _safe_url(response.url),
            body
        )
        raise GoogleBusinessError(_api_error_message(response))

    return response.json()


def api_put(access_token, url, payload=None):
    response = requests.put(
        url,
        json=payload or {},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30
    )

    if response.status_code == 429:
        raise GoogleQuotaError(_quota_error_message(response))

    if not response.ok:
        error, body = _google_error_payload(response)
        logger.warning(
            "Google Business Profile API PUT failed: status=%s google_status=%s url=%s error=%s",
            response.status_code,
            error.get("status"),
            _safe_url(response.url),
            body
        )
        raise GoogleBusinessError(_api_error_message(response))

    if not response.text:
        return {}

    return response.json()


def api_post(access_token, url, payload=None):
    response = requests.post(
        url,
        json=payload or {},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30
    )

    if response.status_code == 429:
        raise GoogleQuotaError(_quota_error_message(response))

    if not response.ok:
        error, body = _google_error_payload(response)
        logger.warning(
            "Google Business Profile API POST failed: status=%s google_status=%s url=%s error=%s",
            response.status_code,
            error.get("status"),
            _safe_url(response.url),
            body
        )
        raise GoogleBusinessError(_api_error_message(response))

    if not response.text:
        return {}

    return response.json()


def api_upload_media(access_token, resource_name, file_bytes, content_type):
    encoded_resource_name = quote(resource_name, safe="")
    response = requests.post(
        f"{MEDIA_UPLOAD_BASE_URL}/{encoded_resource_name}",
        params={"uploadType": "media"},
        data=file_bytes,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": content_type
        },
        timeout=60
    )

    if response.status_code == 429:
        raise GoogleQuotaError(_quota_error_message(response))

    if not response.ok:
        error, body = _google_error_payload(response)
        logger.warning(
            "Google Business Profile media upload failed: status=%s google_status=%s url=%s error=%s",
            response.status_code,
            error.get("status"),
            _safe_url(response.url),
            body
        )
        raise GoogleBusinessError(_api_error_message(response))

    if not response.text:
        return {}

    return response.json()


def list_accounts(access_token):
    accounts = []
    page_token = None

    while True:
        params = {}
        if page_token:
            params["pageToken"] = page_token

        data = api_get(
            access_token,
            f"{ACCOUNT_MANAGEMENT_BASE_URL}/accounts",
            params=params
        )
        accounts.extend(data.get("accounts", []))
        page_token = data.get("nextPageToken")

        if not page_token:
            break

    return accounts


def list_locations(access_token, account_name):
    locations = []
    page_token = None

    while True:
        params = {
            "readMask": "name,title,metadata,storefrontAddress"
        }

        if page_token:
            params["pageToken"] = page_token

        data = api_get(
            access_token,
            f"{BUSINESS_INFORMATION_BASE_URL}/{account_name}/locations",
            params=params
        )
        locations.extend(data.get("locations", []))
        page_token = data.get("nextPageToken")

        if not page_token:
            break

    return locations


def list_all_locations(access_token):
    accounts = list_accounts(access_token)

    if not accounts:
        raise GoogleBusinessError("No Google Business Profile account was found.")

    all_locations = []

    for account in accounts:
        account_name = account.get("name")

        if not account_name:
            continue

        for location in list_locations(access_token, account_name):
            all_locations.append({
                "account_id": account_name,
                "location_id": location.get("name"),
                "location_name": location.get("title") or location.get("name"),
                "raw": location
            })

    if not all_locations:
        raise GoogleBusinessError("No Google Business Profile location was found.")

    return all_locations


def review_parent(account_id, location_id):
    if location_id.startswith("accounts/"):
        return location_id

    if location_id.startswith("locations/"):
        return f"{account_id}/{location_id}"

    return f"{account_id}/locations/{location_id}"


def list_reviews(access_token, account_id, location_id):
    parent = review_parent(account_id, location_id)
    reviews = []
    page_token = None

    while True:
        params = {}
        if page_token:
            params["pageToken"] = page_token

        data = api_get(
            access_token,
            f"{REVIEWS_BASE_URL}/{parent}/reviews",
            params=params
        )
        reviews.extend(data.get("reviews", []))
        page_token = data.get("nextPageToken")

        if not page_token:
            break

    return reviews


def post_review_reply(access_token, account_id, location_id, google_review_id, reply_text):
    parent = review_parent(account_id, location_id)
    return api_put(
        access_token,
        f"{REVIEWS_BASE_URL}/{parent}/reviews/{google_review_id}/reply",
        payload={"comment": reply_text}
    )


def upload_location_photo(access_token, account_id, location_id, file_bytes, content_type, category):
    parent = review_parent(account_id, location_id)
    data_ref = api_post(
        access_token,
        f"{REVIEWS_BASE_URL}/{parent}/media:startUpload"
    )
    resource_name = data_ref.get("resourceName")

    if not resource_name:
        raise GoogleBusinessError("Google did not return a media upload reference.")

    api_upload_media(
        access_token,
        resource_name,
        file_bytes,
        content_type
    )

    return api_post(
        access_token,
        f"{REVIEWS_BASE_URL}/{parent}/media",
        payload={
            "mediaFormat": "PHOTO",
            "locationAssociation": {
                "category": category
            },
            "dataRef": {
                "resourceName": resource_name
            }
        }
    )
