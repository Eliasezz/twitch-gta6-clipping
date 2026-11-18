"""Official Metricool scheduling client: fail-closed, idempotency-friendly.

Requires a Metricool Advanced/Custom API token. Unlike ChatGPT's connector,
the Render web service needs its own API access. Uploaded MP4 media is
normalized onto Metricool hosting BEFORE it is referenced by a scheduled post.
"""
import os
from datetime import datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

API = "https://app.metricool.com/api"


class MetricoolPublishingError(RuntimeError):
    pass


def settings():
    return {
        "token": os.getenv("METRICOOL_API_TOKEN", "").strip(),
        "user_id": os.getenv("METRICOOL_USER_ID", "6375809").strip(),
        "blog_id": os.getenv("METRICOOL_BLOG_ID", "8109476").strip(),
        "enabled": os.getenv("METRICOOL_AUTO_PUBLISH", "0").strip() == "1",
    }


def configured():
    cfg = settings()
    return bool(cfg["token"] and cfg["user_id"].isdigit() and
                cfg["blog_id"].isdigit())


def _headers(cfg):
    return {"X-Mc-Auth": cfg["token"], "Accept": "application/json"}


def _query(cfg):
    return {"userId": cfg["user_id"], "blogId": cfg["blog_id"]}


def normalize_media(original_mp4_url):
    """Use Metricool's official URL-normalization endpoint to host media.

    The source URL must be unlisted but publicly accessible while normalized.
    Do not ever schedule a post with a private/temporary source URL directly.
    """
    cfg = settings()
    if not configured():
        raise MetricoolPublishingError("Metricool API credentials missing")
    parsed = urlparse(original_mp4_url)
    if parsed.scheme != "https" or parsed.hostname != urlparse(
        os.getenv("RENDER_EXTERNAL_URL", "https://twitch-gta6-clipping.onrender.com")
    ).hostname:
        raise MetricoolPublishingError("Only this service's approved MP4 may be transferred")
    try:
        response = requests.get(
            API + "/actions/normalize/image/url",
            params={**_query(cfg), "url": original_mp4_url},
            headers=_headers(cfg), timeout=180,
        )
    except requests.RequestException as exc:
        raise MetricoolPublishingError("Metricool media upload network error") from exc
    if not response.ok:
        raise MetricoolPublishingError(
            f"Metricool media normalization refused (HTTP {response.status_code})"
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise MetricoolPublishingError("Metricool media normalization did not return JSON") from exc
    # Swagger currently declares an untyped response; accept only a returned
    # HTTPS URL hosted elsewhere, never silently pass original signed media.
    if isinstance(data, dict):
        value = data.get("data", data)
        if isinstance(value, dict):
            normalized = value.get("url") or value.get("normalizedUrl")
        else:
            normalized = value if isinstance(value, str) else None
    elif isinstance(data, str):
        normalized = data
    else:
        normalized = None
    parsed_normalized = urlparse(str(normalized or ""))
    if (parsed_normalized.scheme != "https" or not parsed_normalized.hostname
            or parsed_normalized.hostname == parsed.hostname):
        raise MetricoolPublishingError("Metricool did not return an independent hosted media URL")
    return normalized


def _post_payload(media_url, caption, title, *, now=None):
    tz = ZoneInfo("Europe/Paris")
    current = now or datetime.now(tz)
    when = current.astimezone(tz) + timedelta(minutes=15)
    if not (0 < len(caption) <= 1800 and 0 < len(title) <= 90):
        raise MetricoolPublishingError("Invalid post text/title")
    return {
        "publicationDate": {
            "dateTime": when.strftime("%Y-%m-%dT%H:%M:%S"),
            "timezone": "Europe/Paris",
        },
        "providers": [{"network": "tiktok"}, {"network": "youtube"}],
        "media": [media_url],
        "text": caption,
        "autoPublish": True,
        "draft": False,
        "tiktokData": {
            "privacyOption": "PUBLIC_TO_EVERYONE",
            "autoAddMusic": False,
            "title": title[:90],
        },
        "youtubeData": {
            "type": "short", "title": title[:90],
            "privacy": "public", "madeForKids": False,
            "category": "GAMING",
        },
    }


def schedule_both_networks(normalized_media_url, caption, title):
    """Schedule to both connected accounts. Never silently submit without media."""
    cfg = settings()
    if not configured() or not cfg["enabled"]:
        raise MetricoolPublishingError("Metricool automatic scheduling is not enabled")
    url = urlparse(normalized_media_url)
    if url.scheme != "https" or not url.hostname or (
        url.hostname == urlparse(os.getenv(
            "RENDER_EXTERNAL_URL", "https://twitch-gta6-clipping.onrender.com"
        )).hostname
    ):
        raise MetricoolPublishingError("A Metricool-hosted media URL is required")
    payload = _post_payload(
        normalized_media_url, caption, title,
    )
    try:
        response = requests.post(
            API + "/v2/scheduler/posts",
            params=_query(cfg),
            headers={**_headers(cfg), "Content-Type": "application/json"},
            json=payload, timeout=90,
        )
    except requests.RequestException as exc:
        raise MetricoolPublishingError("Scheduling network outcome unknown: inspect Metricool manually") from exc
    if not response.ok:
        raise MetricoolPublishingError(f"Metricool scheduling rejected: HTTP {response.status_code}")
    try:
        result = response.json()
    except ValueError:
        result = {}
    return {
        "state": "scheduled_request_accepted",
        "provider": "metricool",
        "planned_time": payload["publicationDate"],
        "response_id": (result.get("id") or (result.get("data") or {}).get("id"))
                        if isinstance(result, dict) else None,
        "networks": ["tiktok", "youtube"],
        "verification_required": "Confirm post in Metricool planner",
    }
