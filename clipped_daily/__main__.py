"""Command line:

  python -m clipped_daily doctor          check setup (ffmpeg, GPU, keys, streamers)
  python -m clipped_daily fetch           pull clips and print the ranking (no rendering)
  python -m clipped_daily render <url>    render one Twitch clip URL to test the look
  python -m clipped_daily snapshot <login>  frame with a grid to find a streamer's facecam
  python -m clipped_daily auth-youtube    log in to the YouTube channel (once)
  python -m clipped_daily run             start the Discord bot + schedule
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import os
import warnings

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_VERBOSITY", "error")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
warnings.filterwarnings("ignore", module="huggingface_hub")

from . import settings as settings_mod
from .db import DB


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("discord.gateway", "discord.http", "googleapiclient", "faster_whisper", "urllib3",
                  "httpx", "httpx2", "httpcore", "huggingface_hub", "filelock", "discord.client"):
        logging.getLogger(noisy).setLevel(logging.ERROR if noisy.startswith(("hugging", "discord.client")) else logging.WARNING)


def cmd_doctor(s, db) -> int:
    ok = True

    def line(good: bool, msg: str, fix: str = ""):
        nonlocal ok
        ok &= good
        print(f"  {'OK  ' if good else 'FAIL'} {msg}" + (f"\n       -> {fix}" if fix and not good else ""))

    print("Clipped Daily setup check\n")
    env_path = s.root / ".env"
    if not env_path.exists():
        line(False, ".env file exists", "copy .env.example .env  then fill it in")
    else:
        n = settings_mod.load_env(s.root)
        line(n > 0, f".env readable ({n} values loaded from {env_path})",
             "The file looks empty to Python. Open it with notepad .env, check lines look like KEY=value, "
             "then File > Save As > Encoding: UTF-8")
    from .media import render
    ff = render.find_tool("ffmpeg")
    line(bool(ff), f"ffmpeg found ({ff})" if ff else "ffmpeg found",
         "Install ffmpeg: winget install Gyan.FFmpeg (then reopen the terminal), or unzip it into this folder")
    line(bool(render.find_tool("ffprobe")), "ffprobe found", "ffprobe.exe comes in the same ffmpeg zip - keep it next to ffmpeg.exe")
    if ff:
        line(render.has_nvenc(), "NVENC GPU encoding available", "Update NVIDIA drivers. Rendering will still work on CPU.")
    try:
        from .media import captions
        import ctranslate2
        n = ctranslate2.get_cuda_device_count()
        line(n > 0, f"CUDA GPU visible to Whisper ({n})", "Update NVIDIA drivers. Captions will run on CPU (slower).")
        if os.name == "nt":
            have = captions.CUDA_DLLS
            line("cublas64_12.dll" in have and any(x.startswith("cudnn") for x in have),
                 f"CUDA libraries loaded ({len(have)} DLLs: {', '.join(have[:4])}{'…' if len(have) > 4 else ''})",
                 'pip install --force-reinstall nvidia-cublas-cu12 "nvidia-cudnn-cu12>=9,<10"')
        if n > 0:
            try:
                line(True, captions.gpu_selftest())
            except Exception as e:
                line(False, f"Whisper GPU test ({str(e)[:150]})",
                     "pip install nvidia-cublas-cu12 \"nvidia-cudnn-cu12>=9,<10\"  (captions fall back to CPU meanwhile)")
    except Exception as e:
        line(False, f"faster-whisper import ({e})", "pip install -r requirements.txt")

    for key in ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"):
        line(bool(s.env(key, False)), f"{key} set", "Fill it in .env")
    if s.env("TWITCH_CLIENT_ID", False) and s.env("TWITCH_CLIENT_SECRET", False):
        try:
            from .twitch import Twitch
            tw = Twitch(s.env("TWITCH_CLIENT_ID"), s.env("TWITCH_CLIENT_SECRET"))
            found = tw.users([st.login for st in s.streamers])
            for st in s.streamers:
                line(st.login in found, f"Twitch user {st.login}",
                     "Wrong username in streamers.yaml" if st.login not in found else "")
        except Exception as e:
            line(False, f"Twitch API login ({e})", "Check TWITCH_CLIENT_ID / SECRET")

    for key in ("DISCORD_TOKEN", "DISCORD_GUILD_ID", "DISCORD_REVIEW_CHANNEL_ID"):
        line(bool(s.env(key, False)), f"{key} set", "Fill it in .env")
    for key in ("DISCORD_QUEUE_CHANNEL_ID", "DISCORD_POSTED_CHANNEL_ID"):
        if not s.env(key, False):
            print(f"  note {key} empty - using the review channel instead")

    if s.platforms.get("youtube", {}).get("enabled"):
        from .publish import youtube
        line(youtube.token_path(s.data_dir).exists(), "YouTube connected",
             "Run: python -m clipped_daily auth-youtube")
    proxy = s.platforms.get("tiktok", {}).get("proxy")
    if proxy:
        try:
            from .publish import tiktok
            line(True, tiktok.check_reachable(proxy) + f" via {proxy}")
        except Exception as e:
            line(False, f"TikTok via proxy {proxy} ({e})", "Is Cloudflare WARP running in proxy mode?")

    print("\nAll good!" if ok else "\nFix the FAIL lines above, then run doctor again.")
    return 0 if ok else 1


def cmd_fetch(s, db, top: int) -> int:
    from .pipeline import Pipeline, age_text
    p = Pipeline(s, db)
    n = p.fetch()
    print(f"\nFetched {n} clips. Top {top} unreviewed by score:\n")
    rows = db.query("SELECT * FROM clips WHERE status='seen' AND view_count >= ? ORDER BY score DESC LIMIT ?",
                    (s.get("min_views", 300), top))
    for i, r in enumerate(rows, 1):
        print(f"{i:>2}. [{r['score']:6.2f}] {r['broadcaster']:<14} {r['view_count']:>7,} views  "
              f"{age_text(r['created_at']):>10}  {(r['title'] or '')[:50]}\n      {r['url']}")
    return 0


def cmd_render(s, url: str, login: str | None, no_captions: bool) -> int:
    from .media.download import download_clip
    from .pipeline import render_files
    st = s.streamer(login) if login else None
    if no_captions:
        s.config.setdefault("render", {})["captions"] = False
    slug = url.rstrip("/").split("/")[-1].split("?")[0]
    d = s.work_dir / f"test-{slug}"
    print("Downloading…")
    src = download_clip(url, d)
    print("Rendering…")
    final = render_files(s, src, d, st.facecam if st else None, "Test render: how does this look?")
    print(f"\nDone: {final}\nPreview for Discord: {d / 'preview.mp4'}")
    return 0


def cmd_snapshot(s, db, login: str) -> int:
    from .media.download import download_clip
    from .media.render import snapshot_grid
    from .pipeline import Pipeline
    p = Pipeline(s, db)
    p.fetch()
    row = db.one("SELECT * FROM clips WHERE broadcaster=? ORDER BY view_count DESC LIMIT 1", (login.lower(),))
    if not row:
        print(f"No recent clips found for {login}.")
        return 1
    d = s.work_dir / f"snapshot-{login}"
    src = download_clip(row["url"], d)
    out = snapshot_grid(src, d / "grid.jpg")
    print(f"Saved {out}\nEach red square is 10% of the frame. Read the webcam box and put it in streamers.yaml, e.g.\n"
          f"  facecam: {{x: 0.0, y: 0.0, w: 0.25, h: 0.3}}   # left, top, width, height")
    return 0


def cmd_auth_youtube(s, client_secret: str | None) -> int:
    from .publish import youtube
    path = Path(client_secret) if client_secret else next(iter(sorted(s.root.glob("client_secret*.json"))), None)
    if not path or not path.exists():
        print("Put the OAuth client JSON from Google Cloud in this folder (client_secret_....json) "
              "or pass --client-secret PATH")
        return 1
    name = youtube.authorize(path, s.data_dir)
    print(f"Connected YouTube channel: {name}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="clipped_daily")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    f = sub.add_parser("fetch")
    f.add_argument("--top", type=int, default=15)
    r = sub.add_parser("render")
    r.add_argument("url")
    r.add_argument("--streamer", help="login, to use their facecam layout")
    r.add_argument("--no-captions", action="store_true")
    sn = sub.add_parser("snapshot")
    sn.add_argument("login")
    a = sub.add_parser("auth-youtube")
    a.add_argument("--client-secret")
    sub.add_parser("run")
    args = ap.parse_args(argv)

    _setup_logging(args.verbose)
    s = settings_mod.load()
    db = DB(s.db_path)

    if args.cmd == "doctor":
        return cmd_doctor(s, db)
    if args.cmd == "fetch":
        return cmd_fetch(s, db, args.top)
    if args.cmd == "render":
        return cmd_render(s, args.url, args.streamer, args.no_captions)
    if args.cmd == "snapshot":
        return cmd_snapshot(s, db, args.login)
    if args.cmd == "auth-youtube":
        return cmd_auth_youtube(s, args.client_secret)
    if args.cmd == "run":
        from .bot import run_bot
        run_bot(s, db)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
