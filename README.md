# GTA 6 clipper

A network of 18 agents that watches permitted streamers, cuts vertical shorts from their GTA 6 streams,
checks every clip five ways, waits for your approval, then schedules and posts to YouTube Shorts, TikTok and
Instagram Reels and tracks the views. You run it from **Clip Control**, a local web app that also works on your phone.

## Setup (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1
```

Then put your keys in `.env` (copy of `.env.example`) and start everything:

```powershell
.\.venv\Scripts\python.exe run_pipeline.py start
```

Open `http://127.0.0.1:8787` on the PC. The console also prints the address for your phone.

## The agents

| Crew | Agent | Job |
|---|---|---|
| Intake | Scout | Finds new VODs and live marathons from permitted sources, keeps only the GTA 6 hours, queues 2-hour chunks |
| | Downloader | Downloads each chunk and its chat log, newest first |
| | Transcriber | Word-by-word transcript with Whisper |
| | Signal Analyst | Scores every second from audio, chat and speech; proposes candidate moments |
| | Moment Picker | Claude chooses exact cut points, hook, title and caption; weak or overlapping picks are rejected in code |
| | Editor | Renders the 1080x1920 short with facecam, captions, hook and credit |
| Quality | Technical QC | Measures the rendered file: size, length, loudness, black or silent openings, frozen picture |
| | Policy Guard | Permission, leak wording, spoilers, slurs, campaign credit and disclosure, title and caption limits |
| | Originality Checker | Near-duplicate titles and hooks, repeated caption style, too many clips from one stretch |
| | Hook Critic | A second Claude read as a cold viewer; suggests a better title and hook |
| | Reach Forecaster | 0-100 reach score that decides posting order |
| | Gatekeeper | Sums the verdicts into one recommendation on the review card |
| Publishing | Timing Planner | Assigns post times: highest reach first, 60-90 min apart, inside active hours, under daily caps |
| | Publisher | Uploads when the time comes, after re-checking approval and permission |
| | Stats Tracker | Pulls views every 6 hours; notices when a TikTok draft was posted |
| | Growth Analyst | Learns which archetypes and hours win and feeds that back to the picker and forecaster |
| Operations | Janitor | Deletes raw footage after the retention period, warns on low disk |
| | Supervisor | Re-queues stuck work, retries failures once, alerts when an agent keeps failing |

Every agent can be paused, resumed or run on demand from the Agents page, which also shows its log.
More agents does not mean more accounts: this is one channel per platform with human approval, by design.

## Connecting accounts (Connectors page has the same steps)

- **YouTube:** Google Cloud project with YouTube Data API v3, an OAuth client of type *Desktop app*, saved as
  `client_secret.json` in the project folder. In Google Auth Platform, publish the app (Audience), otherwise the login expires
  every 7 days. Uploads start as `private` (`youtube.privacy_status` in `config.yaml`).
- **TikTok:** developer app with Login Kit (Desktop) and Content Posting API, redirect URI
  `http://127.0.0.1:8787/connect/tiktok/callback/`, scopes `user.info.basic,video.upload,video.list`.
  Clips arrive as drafts in your TikTok inbox and you tap Post.
- **Instagram:** Professional account linked to a Facebook Page, a Meta app using Facebook Login, a long-lived
  token in `.env` as `IG_ACCESS_TOKEN` plus `IG_USER_ID`. Reels are uploaded straight from this PC.

Connect from `http://127.0.0.1:8787` on the PC itself; the logins only return to that address.

## What was checked against the platforms' own docs (2026-11-16)

- YouTube no longer forces uploads from unaudited API projects to private (changed 2026-11-02).
  `videos.insert` has its own quota: 100 uploads per day. Shorts can be up to 3 minutes.
- TikTok inbox uploads: at most 5 drafts waiting per 24 hours; chunk rules and the hex-SHA256 PKCE variant are implemented as documented.
- Instagram: uploading a local file needs Facebook Login (not Instagram Login); 100 API posts per 24 hours.

## Testing without real accounts

```powershell
.\.venv\Scripts\python.exe -m tests.test_network
```

Runs the quality, publishing, stats and operations agents on a copy of your database against a local stand-in
for the three platforms that enforces their documented request rules. It needs at least one rendered clip.

## Other commands

```powershell
.\.venv\Scripts\python.exe run_pipeline.py doctor     # environment check
.\.venv\Scripts\python.exe run_pipeline.py review     # control center only, agents off
.\.venv\Scripts\python.exe run_pipeline.py process URL --start 5:35:00 --end 7:35:00   # one VOD range by hand
```

## Safety rules enforced in code

- `unknown` sources are not watched and can never be posted. The Publisher re-checks permission and approval at upload time.
- Leak keywords skip a VOD. Spoiler flags and slurs show on the review card; captions mask strong language.
- Auto-approve is off (`safety.auto_approve_threshold: null`).

## Files

`config.yaml` settings · `sources.yaml` streamers, permission, campaign text · `.env` keys · `data/` database, footage, clips ·
`clipper/agents/` the agents · `clipper/posters/` platform connectors · `review_app/` Clip Control · `prompts/` the two Claude prompts

## Known limits

- Kick has no chat replay here, so Kick clips use audio and speech signals only.
- YouTube blocks some downloads from data-center IPs; on a home connection this is normally fine.
- Facebook Reels, X and Snapchat are not built; they would be further classes in `clipper/posters/`.
- Keep yt-dlp fresh: `.\.venv\Scripts\python.exe -m pip install -U yt-dlp`.
