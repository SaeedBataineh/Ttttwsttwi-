"""Clip ranking, duplicate detection and content warnings. Pure functions, easy to test."""
from __future__ import annotations

import math
import re
import statistics
from datetime import datetime

from .twitch import parse_time


def baseline_views(clips: list[dict], top: int = 20) -> float:
    """A streamer's 'normal good clip': median views of their top 20 clips this week.

    Using only the top clips ignores the flood of tiny 5-view clips that every
    big stream gets, which would otherwise make every clip look huge.
    """
    views = sorted((c["view_count"] for c in clips if c.get("view_count") is not None), reverse=True)[:top]
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


OFFENSIVE = re.compile(
    r"(slur|n[\s-]?word|hard\s?r|racis|homophob|retard|f[\s-]?slur|nazi|sexual|nsfw|porn|nude|"
    r"swat|doxx?|suicid|kill (him|her|myself|urself)|banned|ban(ned)? (on|from) twitch)", re.I)


def flags_for(clip: dict) -> list[str]:
    flags = []
    game = (clip.get("game_name") or "").lower()
    title = clip.get("title") or ""
    if game in MUSIC_GAMES or MUSIC_WORDS.search(title):
        flags.append("🎵 Possible copyrighted music")
    if OFFENSIVE.search(title):
        flags.append("🚫 Title suggests slurs/offensive or ban-worthy content - likely to get the channel struck")
    if game in GAMBLING_GAMES:
        flags.append("🎰 Gambling content (often restricted)")
    return flags


JUNK_TITLES = {"clip", "lol", "lmao", "lmfao", "w", "l", "bruh", "omg", "wtf", "xd", "pog", "kekw", "hi",
               "ok", "bro", "yo", "what", "huh", "nah", "yes", "no", "lul", "omegalul", "icant", "a", "test"}


def is_bad_title(title: str | None) -> bool:
    """True for titles nobody would click: 'A', '2', 'lol', or the stream's own title
    (Twitch uses it as the default clip title, e.g. '!merch | LIVE 24/7 ...')."""
    t = (title or "").strip()
    letters = sum(ch.isalpha() for ch in t)
    if letters < 3 or t.lower().strip("!?.") in JUNK_TITLES:
        return True
    if re.search(r"(^|\s)![a-z]+", t, re.I) or " | " in t or re.search(r"\b(live|24/7|day \d+)\b", t, re.I) and len(t) > 40:
        return True
    words = t.split()
    return len(words) == 1 and len(t) <= 4


def title_from_transcript(words: list[tuple[float, float, str]], max_chars: int = 70) -> str | None:
    """Pick a quotable line the streamer actually says (3-12 words), for clips with junk titles."""
    if not words:
        return None
    sentences, cur = [], []
    for _, _, w in words:
        cur.append(w)
        if w.endswith((".", "?", "!")) or len(cur) >= 14:
            sentences.append(" ".join(cur))
            cur = []
    if cur:
        sentences.append(" ".join(cur))
    for s in sentences:
        clean = re.sub(r"\s+", " ", s).strip(" .,-")
        n = len(clean.split())
        if 3 <= n <= 12 and sum(ch.isalpha() for ch in clean) >= 8:
            if len(clean) > max_chars:
                clean = clean[: max_chars - 1].rsplit(" ", 1)[0] + "…"
            return f"\u201c{clean}\u201d"
    return None


def best_title(twitch_title: str | None, words: list[tuple[float, float, str]] | None,
               streamer: str, game: str | None) -> str:
    if not is_bad_title(twitch_title):
        return twitch_title.strip()
    quote = title_from_transcript(words or [])
    if quote:
        return quote
    return f"{streamer} in {game}" if game and game.lower() != "just chatting" else f"{streamer} can't believe this"


def make_title(template: str, streamer: str, title: str, game: str | None, limit: int = 90) -> str:
    title = re.sub(r"\s+", " ", (title or "").strip()) or "clip"
    out = template.format(streamer=streamer, title=title, game=game or "")
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"
