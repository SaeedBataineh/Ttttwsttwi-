"""Clip ranking, duplicate detection and content warnings. Pure functions, easy to test."""
from __future__ import annotations

import math
import re
import statistics
from datetime import datetime

from .twitch import parse_time


def baseline_views(clips: list[dict]) -> float:
    """A streamer's 'normal' clip views: median of their top clips over the past week."""
    views = [c["view_count"] for c in clips if c.get("view_count") is not None]
    return float(statistics.median(views)) if views else 0.0


def score(view_count: int, created_at: str, baseline: float, now: datetime) -> float:
    """Big-for-this-streamer x rising-fast.

    relative: how this clip compares to the streamer's usual clip, so smaller
              streamers can compete with Kai/xQc.
    velocity: views per hour since the clip was made, so fresh, rising clips win.
    """
    age_h = max((now - parse_time(created_at)).total_seconds() / 3600, 0.25)
    relative = view_count / max(baseline, 50.0)
    velocity = view_count / age_h
    return round(relative * math.log10(10 + velocity), 4)


def is_duplicate(clip: dict, others: list[dict], group_of: dict[str, str | None],
                 same_streamer_window: int = 90, group_window: int = 120) -> str | None:
    """Return the id of a clip this one duplicates, or None.

    - Same streamer: same VOD and offsets within 60s, or created within 90s.
    - Same group (streamers who stream together): created within 120s.
    """
    t = parse_time(clip["created_at"])
    group = group_of.get(clip["broadcaster"])
    for o in others:
        if o["id"] == clip["id"]:
            continue
        dt = abs((parse_time(o["created_at"]) - t).total_seconds())
        if o["broadcaster"] == clip["broadcaster"]:
            if (clip.get("video_id") and clip.get("video_id") == o.get("video_id")
                    and clip.get("vod_offset") is not None and o.get("vod_offset") is not None
                    and abs(clip["vod_offset"] - o["vod_offset"]) < 60):
                return o["id"]
            if dt < same_streamer_window:
                return o["id"]
        elif group and group_of.get(o["broadcaster"]) == group and dt < group_window:
            return o["id"]
    return None


MUSIC_GAMES = {"music", "music & performing arts", "djs"}
GAMBLING_GAMES = {"slots & casino", "slots", "virtual casino", "poker"}
MUSIC_WORDS = re.compile(r"\b(freestyle|song|beat|music|rap|singing|sings|cypher)\b", re.I)


def flags_for(clip: dict) -> list[str]:
    flags = []
    game = (clip.get("game_name") or "").lower()
    title = clip.get("title") or ""
    if game in MUSIC_GAMES or MUSIC_WORDS.search(title):
        flags.append("🎵 Possible copyrighted music")
    if game in GAMBLING_GAMES:
        flags.append("🎰 Gambling content (often restricted)")
    return flags


def make_title(template: str, streamer: str, title: str, game: str | None, limit: int = 90) -> str:
    title = re.sub(r"\s+", " ", (title or "").strip()) or "clip"
    out = template.format(streamer=streamer, title=title, game=game or "")
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"
