# GTA 6 clipper

A network of **120 agents** that watches permitted streamers, cuts vertical shorts from their GTA 6 streams,
checks every clip 49 ways, waits for your approval, then writes per-platform posts, schedules and publishes them to
YouTube Shorts, TikTok and Instagram Reels, keeps paid campaigns compliant and submitted on time, and tracks the views.
You run it from **Clip Control**: a local app with a live **Mission Control** dashboard that also works on your phone.

## Setup (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1
```

This installs Python, ffmpeg, the packages and TwitchDownloaderCLI, runs a health check, and puts a **Clip Control**
shortcut on your desktop. Put your keys in `.env` (a copy of `.env.example`), then double-click the shortcut. It opens
Mission Control in its own window and starts every agent. Or from a console:

```powershell
.\.venv\Scripts\python.exe run_pipeline.py app      # own window (Edge/Chrome app mode)
.\.venv\Scripts\python.exe run_pipeline.py start    # same, in this console
```

Both run under a small supervisor that restarts Clip Control if it ever crashes, and when you press **Restart** on
the dashboard. **Shut down** on the dashboard stops it for good. The console prints the address for your phone
(same Wi-Fi; set `REVIEW_PIN` in `.env`).

## Mission Control

One page, refreshed every 3 seconds, showing:

- **Submit-now strip.** When a campaign post goes live, its link appears with a countdown. Content Rewards only
  accepts a post submitted within 30 minutes of posting.
- **Stats.** Clips waiting for you, approved, posted in 24h, views, estimated campaign earnings, AI spend against the
  daily budget, streamers live, open problems.
- **Production line.** Chunks queued, then downloading, transcribing, finding moments, picking, rendering, agents
  checking, your review, scheduled and live. Busy stages glow.
- **Agent network.** All 120 agents as status chips grouped by crew. Click one to see its job, its backup plan and its
  last action, and to run it now or pause it.
- **Side panels.** Needs attention (deduplicated alerts), live activity, up next, streamers (with LIVE badges),
  connectors, system health (disk, GPU, backups, yt-dlp, clock, lanes, network) and latest clips.
- **Controls.** The countdown to launch, and the Pause all, Restart and Shut down buttons.

The other pages (Review, Agents, Schedule, Stats, Connectors, Sources) are where you act on clips and accounts.

## The network

Full roster with every agent's job and backup plan: [docs/AGENTS.md](docs/AGENTS.md), or `run_pipeline.py agents`.

| Crew | Agents | What it does |
|---|---|---|
| Intake | 17 | One **Scout per streamer**: 30-minute chunks while live, so clips post about 40 minutes after the moment. Also the **Stream Radar** (who's live), Downloader, Transcriber (Whisper large-v3-turbo on GPU), Signal Analyst, **Moment Picker** (Claude), **Backup Picker** (rules, if Claude is down), Editor, Caption Finisher, **Clip Profiler** (measures each clip once), Thumbnail Maker and Trend Watcher |
| Inspection | 40 | One inspector per check: format, length, loudness, peaks, silence, black frames, freezes, sharpness, bitrate, audio track, facecam, platform safe zones, **cutscene detector** (letterbox bars), **duplicate detector** (picture fingerprints), thumbnail, opening frame, caption timing and readability, hook timing and text, title, caption limits (Instagram's 5 hashtags), hashtags, language, credit, permission, leaks, spoilers, violence in the first 7s, **paid disclosure**, **campaign brief rules**, **music guard** (in-game radio and Content ID risk), source authenticity, game check, the **Release Gate** (pre-release footage waits for launch) and five originality guards |
| Judges | 10 | Hook Critic (Claude, text), **Visual Judge** (Claude looks at 4 frames), six reach scorers (signal, archetype, freshness, length, trend, streamer pull), the Reach Forecaster and the Gatekeeper |
| Publishing | 24 | For each of YouTube, TikTok and Instagram: Metadata Writer, Timing Planner, Publisher, Stats Tracker, Token Keeper, Limit Watcher and Momentum Watcher. Plus the **Thumbnail Setter** (custom Shorts thumbnails), Draft Reminder and Breakout Booster |
| Campaign | 5 | **Campaign Clerk** (30-minute submit window), Submission Tracker, Earnings Accountant, Budget Watcher and Brief Keeper |
| Growth | 8 | Archetype, Hour, Length, Caption Style, Streamer and Platform analysts, the Hook A/B Tester and the Daily Digest |
| Operations | 16 | Watchdog, Supervisor, Janitor, **Archivist** (backups every 6h), Backup Verifier, Health Monitor, Network Prober, Clock Checker, **yt-dlp Updater**, Config Watcher, Log Keeper, Alert Desk, **Notifier** (phone), Restart Manager, Disk Forecaster and Performance Monitor |

**Backups, by design.** Each agent has a stated fallback, for example:

- **Claude:** goes through one door (`clipper/llm.py`) with a server-side refusal fallback, a backup model if Opus is
  overloaded, and a daily spend cap.
- **GPU work:** a failed NVENC render redoes on the CPU, and a failed GPU Whisper reloads on the CPU.
- **Stuck work:** stuck renders, downloads and uploads are re-queued.
- **Lanes:** dead lanes are restarted.
- **Database:** snapshotted and the snapshot verified.
- **yt-dlp:** a broken update rolls back to the previous version.
- **Settings:** a broken edit to `config.yaml` or `sources.yaml` keeps the last good settings and tells you why.

## Phone alerts (optional, free)

Install the **ntfy** app, subscribe to a long random topic name, and put it in `.env` as `NTFY_TOPIC=...`. Or create
a Discord webhook and set `DISCORD_WEBHOOK_URL=...`. Urgent alerts (submit-now, disconnected accounts, failures) arrive
instantly; anything that can wait is held during quiet hours (`notify.quiet_hours`). A Daily Digest comes each morning.

## Campaigns

When you join a campaign, set that streamer's `permission: campaign` in `sources.yaml` and paste the brief under
`campaign:`. The format is documented at the top of that file: rate, cap, budget, end date, platforms, required tags
and text, banned words, disclosure and length limits.

The bots act on the brief:

- **Caption Finisher** adds the required text and the `#ad` disclosure.
- **Editor** burns a "PAID PARTNERSHIP" badge into the first 4 seconds (FTC; briefs ask for disclosure at the start).
- **YouTube Publisher** declares paid product placement.
- **Campaign Rules Guard** checks the brief.
- **Campaign Clerk** gets the link in front of you inside the 30-minute submit window.
- **Earnings Accountant** estimates the payout.

## Connecting accounts (the Connectors page has the same steps)

- **YouTube:**
  1. Create a Google Cloud project with YouTube Data API v3 enabled.
  2. Create an OAuth client of type *Desktop app* and save it as `client_secret.json` in the project folder.
  3. In Google Auth Platform, publish the app (Audience). Otherwise the login expires every 7 days.

  Uploads start as `private`. Custom Shorts thumbnails need a phone-verified channel.
- **TikTok:** create a developer app with Login Kit (Desktop) and Content Posting API, with redirect URI
  `http://127.0.0.1:8787/connect/tiktok/callback/` and scopes `user.info.basic,video.upload,video.list`. Clips arrive
  as drafts in your TikTok inbox. The Draft Reminder sends you the caption to paste when you tap Post.
- **Instagram:** you need a Professional account linked to a Facebook Page, a Meta app using Facebook Login, and a
  long-lived token in `.env` as `IG_ACCESS_TOKEN` plus `IG_USER_ID`.

Connect from `http://127.0.0.1:8787` on the PC itself; the logins only return to that address.

## Checked against the platforms' own docs

**2026-11-16 to 11-18:**

- **YouTube:** since Nov 16, `paidProductPlacementDetails.hasPaidProductPlacement` must be set explicitly on upload.
  It's sent on every upload: true for paid campaign clips. Since Nov 3, `thumbnails.set` works for Shorts after
  processing (about 50 quota units). Since Nov 2, uploads from unaudited projects are no longer forced private. Since
  Nov 16, view counts include co-views.
- **TikTok inbox uploads:** at most 5 drafts waiting per 24 hours. Chunk rules and the hex-SHA256 PKCE variant are
  implemented as documented.
- **Instagram:** a local-file upload needs Facebook Login. 100 API posts per 24 hours. 5 hashtags per post since
  December 2025.

## Testing without real accounts

```powershell
.\.venv\Scripts\python.exe -m tests.test_network
```

Renders a synthetic stream into three clips, runs every crew in order, and talks to local stand-ins for YouTube,
TikTok, Instagram, Claude and ntfy that enforce the documented request rules. The run includes an Opus outage, which
the backup model must answer, and a dead lane, which the Watchdog must restart. It takes about 2-3 minutes and
prints ALL PASSED.

## Other commands

```powershell
.\.venv\Scripts\python.exe run_pipeline.py doctor     # environment check
.\.venv\Scripts\python.exe run_pipeline.py agents     # the roster
.\.venv\Scripts\python.exe run_pipeline.py review     # dashboard only, agents off
.\.venv\Scripts\python.exe run_pipeline.py process URL --start 5:35:00 --end 7:35:00   # one VOD range by hand
```

## Safety rules enforced in code

- `unknown` sources are not watched and can never be posted. The Gatekeeper marks their clips *blocked*, and each
  platform's Publisher re-checks permission and approval at upload time.
- Leak keywords skip a VOD; the Leak Guard fails any clip with leak wording. Spoiler flags, cutscene detection and
  slurs show on the review card. Captions mask strong language.
- Auto-approve is off (`safety.auto_approve_threshold: null`). Even when on, it never approves spoiler-flagged,
  backup-picked or unpermitted clips.

## Restoring a backup

Stop Clip Control (Shut down), then copy a file from `data/backups/clipper-YYYYMMDD-HHMM.db` over `data/clipper.db`
and start it again. `config-*.yaml` and `sources-*.yaml` snapshots sit next to it.

## Files

| Path | What it holds |
|---|---|
| `config.yaml` | settings |
| `sources.yaml` | streamers, permission, campaign briefs |
| `.env` | keys |
| `data/` | database, footage, clips, backups, logs |
| `clipper/agents/` | the agents, one file per crew |
| `clipper/llm.py` | the Claude door |
| `clipper/profile.py` | the measurements the inspectors read |
| `clipper/posters/` | platform connectors |
| `review_app/` | Clip Control |
| `prompts/` | the Claude prompts |
| `docs/AGENTS.md` | the roster |

## Known limits

- Kick has no chat replay here, so Kick clips use audio and speech signals only.
- YouTube blocks some downloads from data-center IPs; on a home connection this is normally fine.
- GTA 6 story scenes are not letterboxed (seen on the first launch stream), so the letterbox Cutscene Detector
  rarely fires on them; the Visual Judge (needs the Claude key) is the main story-scene check.
- The music guard detects a steady beat; it can't tell a licensed song from royalty-free music, so it only warns.
- Facebook Reels, X and Snapchat aren't built; each would be another class in `clipper/posters/`.
