from __future__ import annotations

from pathlib import Path


def download_clip(url: str, out_dir: Path) -> Path:
    """Download a Twitch clip as MP4 with yt-dlp. Returns the file path."""
    from yt_dlp import YoutubeDL

    out_dir.mkdir(parents=True, exist_ok=True)
    opts = {
        "outtmpl": str(out_dir / "source.%(ext)s"),
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "noprogress": True,
        "noplaylist": True,
        "overwrites": True,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info))
    if not path.exists():
        matches = sorted(out_dir.glob("source.*"))
        if not matches:
            raise RuntimeError(f"yt-dlp finished but no file found for {url}")
        path = matches[0]
    return path
