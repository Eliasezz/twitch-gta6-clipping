"""Secure Twitch OAuth connection and creator permissions for the overnight editor.

Twitch's official Get Clips Download supports an app access token *after*
the editor/broadcaster has granted editor:manage:clips or channel:manage:clips.
The callback stores only the authorized editor's numeric ID, NOT OAuth tokens.
Users must separately have permission to reuse each creator's footage.
"""
import hashlib
import os
import re
import secrets
import sqlite3
import time
from urllib.parse import urlencode

import requests
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/automation", tags=["twitch-oauth-connection"])


def _db():
    from .main import get_connection
    return get_connection()


def init_connection_db():
    db = _db()
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS twitch_oauth_nonces (
            digest TEXT PRIMARY KEY, expires_at INTEGER NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS twitch_connected_editors (
            id TEXT PRIMARY KEY, login TEXT NOT NULL,
            granted_at TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS twitch_authorized_creators (
            broadcaster_id TEXT PRIMARY KEY,
            login TEXT NOT NULL, attested_at TEXT NOT NULL
        )""")
        db.commit()
    finally:
        db.close()


def editor_connection():
    db = _db()
    try:
        row = db.execute(
            "SELECT id, login FROM twitch_connected_editors ORDER BY granted_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    finally:
        db.close()


def allowed_creators():
    configured = {
        item.strip() for item in os.getenv("ALLOWED_BROADCASTER_IDS", "").split(",")
        if item.strip().isdigit()
    }
    db = _db()
    try:
        rows = db.execute(
            "SELECT broadcaster_id FROM twitch_authorized_creators"
        ).fetchall()
        configured.update(r["broadcaster_id"] for r in rows)
        return configured
    finally:
        db.close()


def allowed_creator_details():
    db = _db()
    try:
        rows = db.execute(
            "SELECT broadcaster_id, login, attested_at FROM twitch_authorized_creators "
            "ORDER BY attested_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


def _redirect_uri():
    base = os.getenv(
        "RENDER_EXTERNAL_URL", "https://twitch-gta6-clipping.onrender.com"
    ).rstrip("/")
    if not base.startswith("https://") or "/" in base[8:]:
        raise HTTPException(503, "Invalid public HTTPS Render origin")
    return base + "/automation/twitch/callback"


@router.post("/twitch/connect")
def initiate_twitch_oauth(x_admin_token: str | None = Header(default=None)):
    from .video_routes import _authorize
    _authorize(x_admin_token)
    client_id = os.getenv("TWITCH_CLIENT_ID", "").strip()
    if not client_id or not os.getenv("TWITCH_CLIENT_SECRET", "").strip():
        raise HTTPException(503, "Configure TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET on Render")
    init_connection_db()
    state = secrets.token_urlsafe(32)
    digest = hashlib.sha256(state.encode()).hexdigest()
    db = _db()
    try:
        db.execute("DELETE FROM twitch_oauth_nonces WHERE expires_at < ?", (int(time.time()),))
        db.execute("INSERT INTO twitch_oauth_nonces (digest,expires_at) VALUES (?,?)",
                   (digest, int(time.time()) + 600))
        db.commit()
    finally:
        db.close()
    url = "https://id.twitch.tv/oauth2/authorize?" + urlencode({
        "client_id": client_id,
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": "editor:manage:clips",
        "state": state,
    })
    return {
        "authorize_url": url,
        "required_redirect_uri": _redirect_uri(),
        "instructions": (
            "First add the callback URI in https://dev.twitch.tv/console/apps, "
            "then open authorize_url and grant the app editor:manage:clips."
        ),
    }


@router.get("/twitch/callback", response_class=HTMLResponse, include_in_schema=False)
def twitch_oauth_callback(code: str = "", state: str = "", error: str = ""):
    if error or not code or not state:
        raise HTTPException(400, "Twitch authorization was cancelled or invalid")
    if len(state) > 200 or len(code) > 500:
        raise HTTPException(400, "Invalid OAuth response")
    init_connection_db()
    db = _db()
    try:
        digest = hashlib.sha256(state.encode()).hexdigest()
        consumed = db.execute(
            "DELETE FROM twitch_oauth_nonces WHERE digest=? AND expires_at>=?",
            (digest, int(time.time())),
        ).rowcount
        db.commit()
        if consumed != 1:
            raise HTTPException(403, "OAuth session expired; retry from the private studio")
    finally:
        db.close()
    try:
        response = requests.post(
            "https://id.twitch.tv/oauth2/token",
            data={
                "client_id": os.environ["TWITCH_CLIENT_ID"],
                "client_secret": os.environ["TWITCH_CLIENT_SECRET"],
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": _redirect_uri(),
            }, timeout=20,
        )
        if not response.ok:
            raise HTTPException(403, "Twitch OAuth code exchange was rejected")
        token = response.json()
        if "editor:manage:clips" not in (token.get("scope") or []):
            raise HTTPException(403, "Twitch did not grant editor:manage:clips")
        user = requests.get(
            "https://api.twitch.tv/helix/users",
            headers={
                "Authorization": "Bearer " + token["access_token"],
                "Client-Id": os.environ["TWITCH_CLIENT_ID"],
            }, timeout=20,
        )
        if not user.ok or not (user.json().get("data") or []):
            raise HTTPException(403, "Could not verify the Twitch editor identity")
        identity = user.json()["data"][0]
        editor_id, login = str(identity["id"]), str(identity["login"])
        if not editor_id.isdigit() or not re.fullmatch(r"[A-Za-z0-9_]{3,25}", login):
            raise HTTPException(403, "Unrecognized Twitch account")
    except (KeyError, ValueError, requests.RequestException) as exc:
        raise HTTPException(503, "Could not complete the Twitch OAuth connection") from exc

    # No refresh token or user access token is persisted. The Twitch app
    # access token can use the prior OAuth grant, if the user is an editor.
    from datetime import datetime, timezone
    db = _db()
    try:
        db.execute("DELETE FROM twitch_connected_editors")
        db.execute(
            "INSERT INTO twitch_connected_editors (id,login,granted_at) VALUES (?,?,?)",
            (editor_id, login, datetime.now(timezone.utc).isoformat()),
        )
        db.commit()
    finally:
        db.close()
    return HTMLResponse(
        "<!doctype html><html lang='fr'><meta charset='utf-8'>"
        "<title>Twitch connecté</title><body style='font-family:system-ui'>"
        "<h2>Connexion Twitch enregistrée</h2>"
        "<p>Le serveur a reçu l'autorisation de l'éditeur Twitch. "
        "Le téléchargement fonctionnera seulement pour les chaînes où ce compte "
        "dispose réellement du rôle autorisé.</p>"
        "<a href='/video/uploader'>Retourner au studio GTA VI</a>"
        "</body></html>"
    )


class CreatorConsent(BaseModel):
    login: str = Field(pattern=r"^[A-Za-z0-9_]{3,25}$")
    reuse_permission_confirmed: bool = False


@router.post("/twitch/creators", status_code=201)
def register_creator(data: CreatorConsent,
                     x_admin_token: str | None = Header(default=None)):
    from .video_routes import _authorize
    _authorize(x_admin_token)
    if not data.reuse_permission_confirmed:
        raise HTTPException(403, "Confirm permission to reuse this creator's videos")
    from .main import twitch_get
    try:
        response = twitch_get("users", {"login": data.login.lower()})
        users = response.get("data") or []
        if not users:
            raise HTTPException(404, "Twitch creator not found")
        profile = users[0]
        broadcaster_id, login = str(profile["id"]), str(profile["login"]).lower()
    except requests.RequestException as exc:
        raise HTTPException(503, "Could not identify creator via Twitch") from exc
    init_connection_db()
    from datetime import datetime, timezone
    db = _db()
    try:
        db.execute(
            "INSERT OR REPLACE INTO twitch_authorized_creators "
            "(broadcaster_id,login,attested_at) VALUES (?,?,?)",
            (broadcaster_id, login, datetime.now(timezone.utc).isoformat()),
        )
        db.commit()
    finally:
        db.close()
    return {"creator": login, "broadcaster_id": broadcaster_id,
            "reuse_permission_attested": True,
            "editor_role_must_also_be_granted": True}


@router.delete("/twitch/creators/{broadcaster_id}")
def revoke_creator(broadcaster_id: str,
                   x_admin_token: str | None = Header(default=None)):
    from .video_routes import _authorize
    _authorize(x_admin_token)
    if not broadcaster_id.isdigit():
        raise HTTPException(400, "Broadcaster ID must be numeric")
    db = _db()
    try:
        db.execute(
            "DELETE FROM twitch_authorized_creators WHERE broadcaster_id=?",
            (broadcaster_id,),
        )
        db.commit()
    finally:
        db.close()
    return {"revoked_creator_id": broadcaster_id,
            "note": "Render ALLOWED_BROADCASTER_IDS, if set, is managed separately"}
