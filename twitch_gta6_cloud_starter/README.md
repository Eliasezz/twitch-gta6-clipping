# Twitch GTA VI Clips — Cloud starter

This is a **starter project**, not a deployed service. It polls Twitch for public GTA VI clip metadata and stores discoveries in SQLite. It deliberately does **not** download or republish third-party clips automatically.

## Why the reuse gate exists
Twitch metadata access is not blanket permission to download/re-edit/repost every creator's video. Twitch's clip-download endpoint has additional authorization requirements. Before processing or publishing a creator's clip, obtain permission/licensing and configure that broadcaster in `ALLOWED_BROADCASTER_IDS`. Do not remove this gate to bypass creator permissions.

## Included
- Docker image suitable for a cloud container host
- FastAPI health endpoint
- Twitch App Access Token via Client ID + Client Secret
- GTA VI game ID lookup and recent clip metadata polling
- SQLite deduplication
- Allowlist gate for creators who have explicitly authorized reuse

## Not included / not yet connected
- Cloud deployment (requires your hosting account and its environment variables)
- Twitch creator authorization for clip downloads
- Actual video download, editing, subtitle rendering or Higgsfield generation
- Metricool publishing (must only be enabled after a permitted MP4 is produced and a public media URL is available)

## Deploy
1. Create a private Git repository and upload this project.
2. Create a Docker web service on your cloud provider.
3. Set the environment variables listed below in the provider's secret/environment settings.
4. Set the service command to `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
5. Keep the repository private. Never commit `.env`.

## Environment variables
- `TWITCH_CLIENT_ID` — Twitch application Client ID
- `TWITCH_CLIENT_SECRET` — Twitch application Client Secret
- `POLL_INTERVAL_SECONDS` — polling interval, default 900 (15 minutes)
- `MAX_CLIPS_PER_POLL` — max metadata records per poll, default 100
- `ALLOWED_BROADCASTER_IDS` — comma-separated Twitch broadcaster IDs with explicit reuse permission; empty means no clip is eligible for processing
- `DATABASE_PATH` — default `/data/clips.sqlite3`

## Local run
```bash
cp .env.example .env
# Fill secrets locally; never send them in chat or commit the file.
docker compose up --build
```
Then open `/health`.

## Twitch authorization details
- Public clip metadata can be retrieved using an App Access Token.
- Twitch's clip-download endpoint has separate requirements and may require the broadcaster/editor to have authorized your app with the appropriate scopes.
- Creating a clip on a broadcaster's channel requires a user token with `clips:edit`; this project does not attempt that.
