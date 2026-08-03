import logging
import math
from urllib.parse import urlparse

import requests

from app.config import Config


SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"
PLACE_FIELDS = "places.id,places.displayName,places.formattedAddress,places.location,places.primaryType,places.types,places.rating,places.userRatingCount,places.businessStatus,places.googleMapsUri"
DETAIL_FIELDS = "id,displayName,formattedAddress,location,primaryType,types,rating,userRatingCount,businessStatus,googleMapsUri"
logger = logging.getLogger(__name__)


class PlacesError(Exception): pass
class PlacesConfigurationError(PlacesError): pass
class PlacesQuotaError(PlacesError): pass
class PlacesPermissionError(PlacesError): pass
class PlacesTemporaryError(PlacesError): pass


def discover_competitors(source, radius_meters=None, query=None, min_review_count=None, limit=20, session=requests):
    _require_key()
    latitude, longitude = source.get("latitude"), source.get("longitude")
    if latitude is None or longitude is None:
        raise PlacesConfigurationError("Competitor discovery is unavailable because this business does not yet have verified location coordinates.")
    if not source.get("google_place_id"):
        raise PlacesConfigurationError("Competitor discovery is unavailable because this business does not yet have a verified Google Place ID.")
    radius = clamp_radius(radius_meters)
    search_query = " ".join(str(query or source.get("primary_category") or source.get("business_type") or "").split())
    if not search_query:
        raise PlacesConfigurationError("Add a business category before searching for competitors.")
    payload = {"textQuery": search_query, "pageSize": min(max(int(limit), 1), 20), "locationBias": {"circle": {"center": {"latitude": float(latitude), "longitude": float(longitude)}, "radius": float(radius)}}}
    try:
        response = session.post(SEARCH_URL, json=payload, headers=_headers(PLACE_FIELDS), timeout=(3.05, 10))
    except (requests.Timeout, requests.ConnectionError) as error:
        raise PlacesTemporaryError("Google Places is temporarily unavailable.") from error
    data = _response_json(response)
    own_place_id = str(source.get("google_place_id") or "")
    threshold = Config.COMPETITOR_MIN_REVIEW_COUNT if min_review_count is None else max(0, int(min_review_count))
    candidates = []
    for place in data.get("places", []):
        candidate = normalize_place(place, source)
        if not candidate or candidate["google_place_id"] == own_place_id: continue
        if candidate["business_status"] == "CLOSED_PERMANENTLY": continue
        if candidate["user_rating_count"] < threshold: continue
        if candidate["distance_meters"] > radius: continue
        candidate["category_match"] = _category_match(search_query, candidate)
        candidate["ranking_score"] = _ranking_score(candidate)
        candidates.append(candidate)
    candidates.sort(key=lambda item: (-item["ranking_score"], item["distance_meters"], -item["user_rating_count"]))
    return {"source_business": {key: source.get(key) for key in ("business_id", "name", "google_place_id", "latitude", "longitude", "primary_category")},
            "search": {"radius_meters": radius, "category": source.get("primary_category"), "query": search_query}, "candidates": candidates[:20]}


def get_place_details(place_id, source=None, session=requests):
    _require_key()
    safe_id = validate_place_id(place_id)
    try:
        response = session.get(DETAILS_URL.format(place_id=safe_id), headers=_headers(DETAIL_FIELDS), timeout=(3.05, 10))
    except (requests.Timeout, requests.ConnectionError) as error:
        raise PlacesTemporaryError("Google Places is temporarily unavailable.") from error
    data = _response_json(response)
    candidate = normalize_place(data, source or {})
    if not candidate: raise PlacesError("Google Places did not return usable business details.")
    return candidate


def normalize_place(place, source):
    place_id = place.get("id"); location = place.get("location") or {}; name = place.get("displayName") or {}
    if not place_id or not name.get("text"): return None
    latitude, longitude = location.get("latitude"), location.get("longitude")
    distance = _distance_meters(source.get("latitude"), source.get("longitude"), latitude, longitude)
    return {"google_place_id": str(place_id), "name": str(name["text"])[:255], "formatted_address": str(place.get("formattedAddress") or "")[:500],
            "latitude": latitude, "longitude": longitude, "primary_type": str(place.get("primaryType") or "")[:120],
            "types": [str(value)[:120] for value in (place.get("types") or [])[:20]], "rating": float(place["rating"]) if place.get("rating") is not None else None,
            "user_rating_count": int(place.get("userRatingCount") or 0), "business_status": str(place.get("businessStatus") or "OPERATIONAL")[:50],
            "google_maps_url": _safe_maps_url(place.get("googleMapsUri")), "distance_meters": distance,
            "category_match": False, "ranking_score": 0}


def clamp_radius(value):
    try: radius = int(value)
    except (TypeError, ValueError): radius = Config.COMPETITOR_SEARCH_RADIUS_METERS
    return min(20000, max(500, radius))


def validate_place_id(value):
    value = str(value or "").strip()
    if not value or len(value) > 255 or not all(character.isalnum() or character in "_-" for character in value):
        raise ValueError("Invalid Google Place ID.")
    return value


def _ranking_score(candidate):
    """Category 45%, distance 30%, review volume 20%, rating 5%."""
    category = 45 if candidate["category_match"] else 12
    distance = max(0, 30 * (1 - min(candidate["distance_meters"], 20000) / 20000))
    volume = min(20, math.log10(max(candidate["user_rating_count"], 1)) / 4 * 20)
    rating = (candidate["rating"] or 0) / 5 * 5
    return round(category + distance + volume + rating, 3)


def _category_match(query, candidate):
    tokens = {token for token in query.lower().replace("_", " ").split() if len(token) > 2}
    haystack = " ".join([candidate["primary_type"], *candidate["types"]]).lower().replace("_", " ")
    return bool(tokens and any(token in haystack for token in tokens))


def _distance_meters(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2): return 20000
    phi1, phi2 = math.radians(float(lat1)), math.radians(float(lat2)); delta_phi = math.radians(float(lat2) - float(lat1)); delta_lambda = math.radians(float(lon2) - float(lon1))
    a = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    return int(round(6371000 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))))


def _safe_maps_url(value):
    value = str(value or "").strip()[:1000]
    parsed = urlparse(value)
    return value if parsed.scheme == "https" and parsed.hostname in {"google.com", "www.google.com", "maps.google.com"} else ""


def _headers(field_mask):
    return {"Content-Type": "application/json", "X-Goog-Api-Key": Config.GOOGLE_PLACES_API_KEY, "X-Goog-FieldMask": field_mask}


def _require_key():
    if not Config.GOOGLE_PLACES_API_KEY: raise PlacesConfigurationError("Competitor discovery is not configured yet.")


def _response_json(response):
    if response.status_code == 429: raise PlacesQuotaError("Google Places quota is temporarily unavailable. Please try again later.")
    if response.status_code in {401, 403}: raise PlacesPermissionError("Google Places access is not enabled for this application.")
    if response.status_code >= 500: raise PlacesTemporaryError("Google Places is temporarily unavailable.")
    if not response.ok: raise PlacesError("Google Places could not complete this request.")
    try: return response.json()
    except ValueError as error: raise PlacesTemporaryError("Google Places returned an invalid response.") from error
