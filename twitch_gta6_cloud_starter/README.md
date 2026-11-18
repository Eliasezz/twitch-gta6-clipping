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


## No-upload demo (real MP4 technical test)

Once `POLL_ADMIN_TOKEN` is configured in Render, open
`https://twitch-gta6-clipping.onrender.com/docs`, then use
`POST /video/demo` (under **video-workflow**):

1. Click **Try it out**.
2. Set `clip_seconds=65` and paste your **admin token** into
   `X-Admin-Token` (not your Twitch Client Secret).
3. Click **Execute**. No video file and no `authorized` checkbox required.
4. Copy the returned `job_id`.
5. Use `GET /video/jobs/{job_id}` with the same admin token.
   Once the state becomes `done`, call
   `GET /video/jobs/{job_id}/download` to download the MP4.

This creates a synthetic test-pattern video with a test audio tone, and
runs it through the real editor. For demo workloads it renders a small
360x640 preview to limit CPU and memory consumption. It is **not** GTA VI,
a streamer clip, licensed media, or monetizable content. For real clips,
`POST /video/jobs` still requires an authorized video source upload.

### Optional one-time server self-test

Setting `VIDEO_SELFTEST_ONCE=1` in Render triggers one private, 65-second
synthetic video render on startup. It creates a persistent `selftest.json`
manifest so a successful self-test is not repeated after restarts.
Check `GET /video/ready` and its `production_selftest.state` field; state
`done` and `duration_seconds` ~65 verify a video was successfully rendered
inside Render. Its MP4 can be retrieved using the usual authenticated
`GET /video/jobs/{job_id}/download` endpoint. No third-party clip is fetched.


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
   optionally supply `caption_text` (English words); use `clip_seconds=65`
   for a video longer than one minute (source footage must be at least 65 seconds);
   click Execute.
5. Copy the returned `job_id`. Use `GET /video/jobs/{job_id}` to check the
   `state` (queued, processing, done, failed), with the same token.
6. Once `done`, use `GET /video/jobs/{job_id}/download` in the API docs, with
   the same token, to retrieve your private MP4.

Endpoint `GET /video/ready` checks if FFmpeg is available and whether
`POLL_ADMIN_TOKEN` is configured, without exposing any secrets.

**Duration and monetization:** `clip_seconds` defaults to 65 (allowed: 3–90).
A source shorter than the chosen 61+-second duration is rejected rather than
silently padded or repeated. A 45-second edit is possible but **does not meet**
TikTok Creator Rewards' >1-minute duration requirement. Length alone does
not ensure eligibility: the content must be original, high quality, and the
account must qualify for the program. Re-edited streamer footage can be
ineligible even with permission. YouTube Shorts have a different revenue model
and reused-content policies.

**Current limitations:** plain caption text is split into short cards with
**approximate timing**, not true transcription or speech alignment; the
highlight window is an audio-activity heuristic, not a reliable AI emotional
ranking. FFmpeg outputs a 720x1280, 24fps H.264 MP4. On the current small Render
instance, lengthy media takes time and the disk is capped; use short source
videos for the first test. Old completed video jobs are removed after 48 hours
by the next successful upload request. The workflow processes only uploaded footage;
automatic downloads require separately authorized Twitch broadcaster/editor
OAuth and appropriate licensing. Social publishing remains disabled.


## English captions and reaction selection (optional speech service)

- The **existing** `POST /video/jobs` endpoint now also accepts
  `auto_subtitles=true`. This runs speech transcription, uses likely reaction
  expressions and spoken-word density to choose a highlight in the first
  180 seconds, then overlays English subtitle words with colored active-word
  karaoke effects. It will not claim to detect guaranteed viral moments.
- Add `OPENAI_API_KEY` to **Render Environment** to enable this feature.
  Use an OpenAI Platform API key created for this project; **ChatGPT Plus
  does not include API usage**. Nothing is charged by this application when
  `auto_subtitles=false`.
- For English speech, Whisper's verbose JSON word timestamps support
  near-word-level synchronization. For other languages, the translation API
  creates **English captions timed approximately to spoken segments**, not
  exact English per-word alignment. Review subtitles before publishing.
- `whisper-1` is deprecated and presently scheduled to stop
  **2027-02-26** (per OpenAI's API documentation). Replace the transcription
  adapter with a supported word-timestamp model before that date.
- Without an API key, you can still render videos with manually supplied
  `caption_text` (approximate timing) and run `POST /video/demo` for free.
  A request asking for `auto_subtitles=true` without the key fails early
  with HTTP 503 instead of producing misleading unsynchronized captions.

## Metricool publishing handoff — gated

The TikTok and YouTube accounts may already be connected in the Metricool
plugin in ChatGPT, but this **does not grant the Render server** direct access
to its plugin connection or publishing tokens. We do not store those tokens.

1. Upload a permitted real video using `POST /video/jobs`, confirm
   `authorized=true`, optionally enable `auto_subtitles=true`, and wait for
   the MP4 job to reach `done`. The test/demo route cannot be published.
2. Call authenticated `GET /video/jobs/{job_id}/publication` to retrieve
   the draft TikTok caption, YouTube title and description and review checklist.
3. **Only after personally checking the edited video and rights**, call
   authenticated `POST /video/jobs/{job_id}/approve`, setting
   `rights_confirmed=true` and `editorial_reviewed=true`.
4. The response includes a time-limited direct MP4 URL (`signed_media_url`)
   valid for 1 hour and the prepared post metadata. Give this URL and the
   desired publishing time to Metricool via ChatGPT, or schedule it yourself
   in Metricool. The temporary URL grants access to whoever has it until it
   expires; keep it private.
5. YouTube and TikTok publishing are **not** performed by the Render server.
   Scheduling/publishing require the Metricool API/app workflow and platform
   consent. No video will be posted without an explicit publishing action.

Videos of 65 seconds can meet a duration threshold, but a streamer's video
with only cropping, zooms and captions can still fail originality or reuse
policies. Creator permission and substantive original added value are critical.


## Autonomous official Twitch -> edit -> TikTok + YouTube (opt-in)

**Status:** the code is installed, but real automatic posting is gated by
confirmed technical platform authorization and available source clips.
Creator reuse permission by itself does not grant Twitch's API download
scope. No scraping or fabricated Twitch download URLs are used.

1. Complete **Twitch user OAuth** as an approved channel editor or
   broadcaster with scope `editor:manage:clips` or `channel:manage:clips`.
   Store the resulting user OAuth token as `TWITCH_EDITOR_ACCESS_TOKEN`
   and its matching user ID as `TWITCH_EDITOR_ID`, in **Render Environment**.
   App Client ID/Secret alone do not grant access.
2. Set `ALLOWED_BROADCASTER_IDS` to the numeric Twitch creator IDs that
   granted licensing permission. An empty list blocks all automatic edits.
3. Set `OPENAI_API_KEY` in Render to permit paid transcription. It
   generates English timed captions (translation of non-English speech
   has approximate segment-based timing), while the speech keyword scoring
   selects likely reactions.
4. In **Metricool Settings > API**, obtain the account's API token (only
   if the plan offers it), then set `METRICOOL_API_TOKEN` in Render.
   The already linked account IDs are `METRICOOL_USER_ID=6375809` and
   `METRICOOL_BLOG_ID=8109476`. You do not need to reconnect TikTok/YouTube
   in ChatGPT; the cloud server still needs its independent API token.
5. Set `AUTO_PIPELINE_ENABLED=1` and `METRICOOL_AUTO_PUBLISH=1`.
   The background poller attempts one autonomous batch every 15 minutes,
   with a default maximum of **one** finished batch per UTC day.

### What the autonomous pipeline does

- Discover clips for allowlisted Twitch creators, ordered by views.
- Use the **official** Twitch `helix/clips/downloads` API with the editor
  OAuth token, without guessing video CDN URLs.
- Select **two to five clips of the same creator** with enough genuine
  footage for 65 seconds; normalize/concatenate them into a source video.
- Select a speech/reaction-rich window using ASR keyword heuristics.
- Render **65-second 720×1280, 24fps MP4**, original synthesized electronic
  backing music mixed quietly beneath the creator's voice, a brief on-screen
  title hook, and timed colored English subtitle words.
- Submit the MP4 for immediate normalization onto Metricool hosting using
  an unlisted, high-entropy media-transfer URL; revoke it after import.
- Schedule the same licensed edit to both TikTok and YouTube via Metricool.
  Each platform's account rules and the Metricool API plan apply.
- Keep a SQLite table of used clip IDs, so automatic polls do not
  repost the same source clips. No more than one batch/day by default.

### Safety, monitoring, and costs

Check `GET /automation/status` for **boolean configuration readiness**;
it never exposes secrets. `POST /automation/run` can manually trigger
a pass with the existing `X-Admin-Token`. Missing OAuth or API credentials
returns an explicit blocker instead of submitting any post.

Metricool API requires an eligible paid plan. The server must host video
long enough for Metricool's normalize endpoint to ingest it; if Metricool
does not return an independent video URL, scheduling fails closed. Actual
publication is **not guaranteed** by a successful scheduler response;
check the Metricool planner and platform status. Do not enable unattended
posting until the official OAuth, media normalization, and a complete
real-video posting test have been confirmed. Automatic music is an
**original synth instrumental** rather than a copyrighted trending song;
trend music licensing cannot be assumed. Remixing other creators' clips
may also be ineligible for monetization under originality rules.

For a manual permitted upload via `POST /video/jobs`, new form fields
`music_enabled=true` and `hook_text=GTA VI | BIG REACTION` are
available. The existing `POST /video/demo` remains synthetic and must
not be scheduled.
