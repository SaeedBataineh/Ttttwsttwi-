# Clipped Daily

Pulls the top Twitch clips from your streamers every ~45 minutes, turns the best ones
into vertical 1080x1920 videos with captions, sends them to Discord for you to approve,
then posts approved clips to YouTube Shorts on a schedule (TikTok / Instagram next).

```
Twitch API -> score & pick -> download (yt-dlp) -> captions (Whisper, GPU)
           -> vertical render (ffmpeg, NVENC) -> Discord #review [Approve/Reject/Edit]
           -> #queue -> posted at 12:00 / 16:00 / 20:00 -> #posted with links
```

## 1. Install (Windows)

**Easiest:** in a normal PowerShell in this folder run `.\setup.ps1`. It installs
everything below (Python packages, ffmpeg, GPU caption libraries), creates `.env`
and runs the full check. Safe to rerun.

Manual steps:

1. **Python 3.11+** from python.org (tick "Add python.exe to PATH").
2. **ffmpeg**: open PowerShell and run `winget install Gyan.FFmpeg`, then close and reopen PowerShell.
3. **Up-to-date NVIDIA driver** (for NVENC encoding).
4. In this folder:
   ```powershell
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   copy .env.example .env
   ```

### GPU captions (optional, recommended)
Whisper runs on the RTX 3070 if CUDA libraries are available; otherwise it falls back
to CPU automatically (slower, still works). To enable the GPU on Windows, download
the cuBLAS + cuDNN for CUDA 12 DLLs (the `faster-whisper` README links Purfview's
`whisper-standalone-win` archive), extract them to a folder and add that folder to PATH.
`python -m clipped_daily doctor` tells you whether it worked.

## 2. Keys (fill in `.env`)

### Twitch (2 min)
dev.twitch.tv/console/apps -> **Register Your Application**
- OAuth Redirect URL: `http://localhost`
- Category: Application Integration
- Copy **Client ID** and create a **Client Secret** into `.env`.

### Discord bot (5 min)
1. discord.com/developers/applications -> **New Application** -> "Clipped Daily".
2. **Bot** tab -> Reset Token -> copy into `DISCORD_TOKEN`. No privileged intents needed.
3. **OAuth2 -> URL Generator**: scopes `bot` + `applications.commands`; permissions
   *Send Messages, Embed Links, Attach Files, Read Message History*. Open the URL and add it to your server.
4. In your server make `#review`, `#queue`, `#posted` (private is best).
5. Discord Settings -> Advanced -> **Developer Mode** on. Right-click the server / channels
   -> **Copy ID** into `.env`. Put your own user ID in `DISCORD_ADMIN_IDS`.

### YouTube (10 min)
1. console.cloud.google.com -> new project "Clipped Daily".
2. **APIs & Services -> Library** -> enable **YouTube Data API v3**.
3. **OAuth consent screen**: External. Add your Google account as a test user, then
   click **Publish app** (otherwise the login expires every 7 days; the "unverified app"
   warning is fine for personal use).
4. **Credentials -> Create credentials -> OAuth client ID -> Desktop app**. Download the
   JSON into this folder (keep the name `client_secret_....json`; it's git-ignored).
5. `python -m clipped_daily auth-youtube` and log in with the **Clipped Daily** channel.

> **Important:** YouTube locks videos uploaded through a new, unaudited API project to
> **private**. Submit the free *YouTube API Services audit* form from the Google Cloud
> console to get public uploads. Until then, set `privacy: private` and publish
> manually, or test the whole flow privately first.

## 3. Test step by step

```powershell
python -m clipped_daily doctor                  # everything green?
python -m clipped_daily fetch                   # see the ranked clips (no rendering)
python -m clipped_daily render https://clips.twitch.tv/<clip-slug>   # check the look -> work\test-...\final.mp4
python -m clipped_daily snapshot kaicenat       # grid image to find the facecam box
python -m clipped_daily run                     # start the bot
```

## Discord

| | |
|---|---|
| `#review` | each candidate: preview video, views, score, ⚠️ warnings, **Approve / Reject / Edit title** |
| `#queue` | approved clips and roughly when they'll post |
| `#posted` | links after upload |
| `/queue` `/skip n` `/postnow` | manage the queue |
| `/fetchnow` | check Twitch immediately |
| `/gaming on/off/auto` | pause rendering while you play (auto detects games from `config.yaml`) |
| `/streamers list/add/remove` | edit the streamer list |
| `/status` | counts, gaming mode, next post |

## Tuning (`config.yaml`)

- `post_times`, `max_posts_per_streamer_per_day`, `title_template`
- `min_views`, `max_review_per_streamer_per_day`, `max_pending_review`
- `render.whisper_model` (`small` is a good balance; `medium` = better accuracy, more VRAM)
- Facecam layout per streamer in `streamers.yaml` (`facecam: {x, y, w, h}`), otherwise blurred background.

## How clips are picked

`score = (views / streamer's usual clip views) x log10(10 + views per hour)`

So a clip that's huge *for that streamer* and still climbing wins, which keeps
smaller streamers competitive with Kai / xQc. Clips of the same moment are
de-duplicated, including across streamers in the same `group` (e.g. AMP).

## TikTok in Jordan

TikTok upload isn't built yet. When it is, only TikTok traffic will go through
`platforms.tiktok.proxy` (e.g. free Cloudflare WARP in proxy mode:
`socks5h://127.0.0.1:40000`), so you don't need a system-wide VPN. Running the bot
on a VPS outside Jordan avoids the need entirely.

## Respect the streamers

Credit is added to every description automatically. Only clip streamers who allow
it, and remove anyone who asks (`/streamers remove`).
