# Twitch GTA VI Clips — Cloud starter

This service polls Twitch for public GTA VI clip metadata and stores discoveries in SQLite. It deliberately does **not** download or republish third-party clips automatically. A clip is marked eligible only when it meets the view threshold AND its broadcaster ID is present in `ALLOWED_BROADCASTER_IDS`.

## Why the reuse gate exists
Twitch metadata access is not blanket permission to download/re-edit/repost every creator's video. Twitch's clip-download endpoint has additional authorization requirements. Before processing or publishing a creator's clip, obtain permission/licensing and configure that broadcaster in `ALLOWED_BROADCASTER_IDS`. Do not remove this gate to bypass creator permissions.

## Included
- Docker image suitable for a cloud container host
- FastAPI health endpoint
- Twitch App Access Token via Client ID + Client Secret
- GTA VI game ID lookup and recent clip metadata polling
- SQLite deduplication
- Enforced allowlist gate for creators who have explicitly authorized reuse
- Cursor pagination for collection limits above 100 clips per source
- FFmpeg vertical MP4 editing for **uploaded, authorized** source video files
- Rough audio-activity window selection, animated zoom, colored captions and private MP4 output
- Protected video job submission, status and download APIs
- Backward-compatible `MAX_CLIPS_PER_POLL` / `MAX_CLIPS` configuration

## Not included / not yet connected
- Live deployment is managed separately in Render; this repository does not create or pay for cloud resources.
- Twitch creator authorization for clip downloads
- Direct automatic Twitch clip download (requires a creator/editor OAuth authorization)
- Automatic speech transcription or translation (currently uses optionally supplied caption text)
- Higgsfield generation (not needed for the FFmpeg smoke test)
- Metricool publishing (must only be enabled after a permitted MP4 is produced and a public media URL is available)

## Deploy
1. Connect this repository to Render. The existing web service is named `twitch-gta6-clipping`.
2. Set the environment variables listed below using the **secret/environment settings**, never in GitHub files.
3. Use root directory `twitch_gta6_cloud_starter`, build command `pip install -r requirements.txt` and start command `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
4. Attach a persistent disk to `/data` for the SQLite database.
5. Keep credentials private. Never commit `.env` even when the repository is public.

## Environment variables
- `TWITCH_CLIENT_ID` — Twitch application Client ID
- `TWITCH_CLIENT_SECRET` — Twitch application Client Secret
- `POLL_ADMIN_TOKEN` — optional secret required for manual `POST /poll`; if absent, manual polling is disabled (scheduled polling is unaffected)
- `POLL_INTERVAL_SECONDS` — polling interval, default 900 (15 minutes)
- `MAX_CLIPS_PER_POLL` — max metadata records per category poll, default 100 (up to 1000)
- `MAX_CLIPS` — legacy alias for `MAX_CLIPS_PER_POLL`
- `KNOWN_CLIPS_LIMIT` — max metadata records per followed broadcaster, default 20 (up to 1000)
- `MIN_CLIP_VIEWS` — minimum view count for eligibility, default 100
- `CLIP_LOOKBACK_HOURS` — discovery window, default 24
- `KNOWN_STREAMERS` — comma-separated Twitch logins to monitor
- `ALLOWED_BROADCASTER_IDS` — comma-separated Twitch broadcaster IDs with explicit reuse permission; empty means no clip is eligible for processing
- `DATABASE_PATH` — default `/data/clips.sqlite3`
- `VIDEO_STORAGE_DIR` — private source/output files, default `/data/videos`
- `VIDEO_MAX_UPLOAD_MB` — upload size limit, default 75 MB, cap 120 MB
- `VIDEO_KEEP_HOURS` — completed job retention, default 48 hours; download the MP4 before it expires

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

## Manual polling (secured)
`POST /poll` requires the `X-Admin-Token` HTTP header matching `POLL_ADMIN_TOKEN`. Do not put the token in a URL or share it. Without this variable, leave the background poller to run automatically. `/health` and `/clips` remain read-only and public.

## Validate changes
Run `python -m unittest discover -s tests -v` from `twitch_gta6_cloud_starter`, or review the GitHub Actions workflow. Clips marked eligible are **not** automatically licensed: you are responsible for obtaining the relevant creator permissions and complying with each platform's rules.


## Video workflow (FFmpeg, no additional subscription)

The Render native Python runtime includes `ffmpeg` and `ffprobe`. This
project accepts **only a directly uploaded file** with explicit reuse
confirmation, and never scrapes a Twitch/YouTube stream, evades creator
restrictions, or auto-publishes a clip.

1. In Render environment settings, configure a long random `POLL_ADMIN_TOKEN`
   if not already set. Keep it secret. Restart/redeploy the web service if needed.
2. Open `https://twitch-gta6-clipping.onrender.com/docs`.
3. In **video-workflow**, expand `POST /video/jobs` -> **Try it out**.
4. Set `X-Admin-Token` to the private token, check `authorized=true`
   **only if you own the video or have permission**, choose an MP4/MOV/MKV and
   optionally supply `caption_text` (English words); click Execute.
5. Copy the returned `job_id`. Use `GET /video/jobs/{job_id}` to check the
   `state` (queued, processing, done, failed), with the same token.
6. Once `done`, use `GET /video/jobs/{job_id}/download` in the API docs, with
   the same token, to retrieve your private MP4.

Endpoint `GET /video/ready` checks if FFmpeg is available and whether
`POLL_ADMIN_TOKEN` is configured, without exposing any secrets.

**Current limitations:** plain caption text is split into short cards with
**approximate timing**, not true transcription or speech alignment; the
highlight window is an audio-activity heuristic, not a reliable AI emotional
ranking. FFmpeg outputs a 720x1280, 24fps H.264 MP4. On the current small Render
instance, lengthy media takes time and the disk is capped; use short source
videos for the first test. Old completed video jobs are removed after 48 hours
by the next successful upload request. The workflow processes only uploaded footage;
automatic downloads require separately authorized Twitch broadcaster/editor
OAuth and appropriate licensing. Social publishing remains disabled.
