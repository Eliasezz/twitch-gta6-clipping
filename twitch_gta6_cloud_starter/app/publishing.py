"""Metadata and short-lived, manually approved media handoff to Metricool.

This module NEVER publishes to any platform. A signed link only permits a
temporary download of an already-licensed, editor-approved clip.
"""
import re
import hashlib
import hmac
import os
import time
from urllib.parse import urlencode


def publication_package(job_id, job_state):
    """Content and compliance checklist for human-approved scheduling."""
    source = str(job_state.get("original_filename") or "authorized clip")
    source = source.rsplit("/", 1)[-1].rsplit("\\", 1)[-1][:100]
    title = str(job_state.get("publication_title") or "GTA VI | Gaming highlight")[:100]
    description = (
        f"GTA VI gaming highlight. Edited from authorized source: {source}. "
        "Comment your favorite moment! #GTAVI #GTA6 #Gaming #Shorts"
    )
    origin = job_state.get("origin") or {}
    if origin:
        creator = origin.get("broadcaster_name") or "Twitch"
        vision = (job_state.get("result", {}).get("highlight_analysis") or {}).get("vision") or {}
        evidence = vision.get("reason") or ("Extrait du stream « " + str(origin.get("title") or "GTA VI") + " ».")
        tag = re.sub(r"[^A-Za-z0-9_]", "", creator)
        description = f"{creator} sur GTA VI. {evidence} #GTA6 #GTAVI #TwitchClips" + (" #"+tag if tag else "")
    return {
        "origin": origin,
        "source_shots": (job_state.get("result") or {}).get("source_shots", []),
        "job_id": job_id,
        "title": title,
        "tiktok_caption": description[:2000],
        "youtube_title": title + " #Shorts",
        "youtube_description": description,
        "channels": ["tiktok", "youtube"],
        "ready": job_state.get("state") == "done"
                 and job_state.get("authorized") is True
                 and job_state.get("demo") is not True,
        "status": job_state.get("publication_status", "review_required"),
        "automatic_publication": False,
        "requirements": [
            "source creator/licensing permission verified",
            "human review of copyright, audio, editing and accuracy",
            "originality and monetization rules of each platform checked",
            "Metricool account and media transfer configured",
        ],
    }


def sign_share_link(job_id, secret, *, lifetime_seconds=3600, now=None):
    if not secret or lifetime_seconds < 60 or lifetime_seconds > 7200:
        raise ValueError("Invalid token or duration")
    now = int(time.time()) if now is None else int(now)
    expires = now + lifetime_seconds
    signed = f"video-share:{job_id}:{expires}".encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    base = os.getenv(
        "RENDER_EXTERNAL_URL", "https://twitch-gta6-clipping.onrender.com"
    ).rstrip("/")
    path = f"/video/shared/{job_id}"
    url = base + path + "?" + urlencode({"expires": expires, "signature": signature})
    return {"url": url, "expires_unix": expires}


def verify_share_link(job_id, secret, expires, signature, *, now=None):
    if not secret or not signature or len(signature) != 64:
        return False
    now = int(time.time()) if now is None else int(now)
    try:
        expires = int(expires)
    except (ValueError, TypeError):
        return False
    if expires <= now or expires > now + 7200:
        return False
    signed = f"video-share:{job_id}:{expires}".encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
