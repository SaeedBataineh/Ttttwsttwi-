"""Posts a rendered clip to every enabled platform."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .. import tagging
from ..settings import Settings

log = logging.getLogger(__name__)


def description_for(settings: Settings, clip: dict) -> str:
    st = settings.streamer(clip["broadcaster"])
    name = st.name if st else clip["broadcaster"]
    headline = clip.get("post_title") or clip.get("title") or ""
    lines = [
        headline,
        "",
        f"🎥 Streamer: {name} - https://twitch.tv/{clip['broadcaster']}",
        "Go follow and support the stream!",
    ]
    if clip.get("creator_name"):
        lines.append(f"✂️ Clipped on Twitch by {clip['creator_name']}")
    lines += ["", tagging.hashtags(tags_for(settings, clip))]
    return "\n".join(lines)


def tags_for(settings: Settings, clip: dict) -> list[str]:
    stored = clip.get("tags")
    if isinstance(stored, str):
        try:
            stored = json.loads(stored)
        except ValueError:
            stored = None
    if stored:
        return list(stored)
    st = settings.streamer(clip["broadcaster"])
    tags = [clip["broadcaster"], st.name if st else "", "twitch", "stream clips", "shorts"]
    if clip.get("game_name"):
        tags.append(clip["game_name"])
    return [t for t in dict.fromkeys(tags) if t]


def publish(settings: Settings, clip: dict) -> tuple[dict[str, str], dict[str, str]]:
    """Upload to all enabled platforms. Returns (results {platform: url}, errors {platform: msg})."""
    video = Path(clip["work_dir"]) / "final.mp4"
    if not video.exists():
        raise RuntimeError(f"Rendered video missing: {video}")
    title = clip.get("post_title") or clip.get("title") or "clip"
    desc = description_for(settings, clip)
    results: dict[str, str] = {}
    errors: dict[str, str] = {}
    already = clip.get("_results", {})
    p = settings.platforms

    if p.get("youtube", {}).get("enabled") and "youtube" not in already:
        try:
            from . import youtube
            results["youtube"] = youtube.upload(
                video, title, desc, tags_for(settings, clip), settings.data_dir,
                privacy=p["youtube"].get("privacy", "public"),
                category_id=str(p["youtube"].get("category_id", "20")),
            )
        except Exception as e:
            log.exception("YouTube upload failed")
            errors["youtube"] = str(e)[:500]

    if p.get("tiktok", {}).get("enabled") and "tiktok" not in already:
        try:
            from . import tiktok
            results["tiktok"] = tiktok.upload(video, title, p["tiktok"].get("proxy") or None)
        except Exception as e:
            errors["tiktok"] = str(e)[:500]

    if p.get("instagram", {}).get("enabled") and "instagram" not in already:
        errors["instagram"] = "Instagram upload isn't built yet"

    return results, errors
