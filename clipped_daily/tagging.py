"""Tags and hashtags for each clip: who's in it, what game, and what they're talking about."""
from __future__ import annotations

import re
from collections import Counter

# Topic tags picked up from what's said in the clip (and the Twitch title).
TOPICS: dict[str, str] = {
    r"\b(rag(e|ing)|mad|angry|tilted|screaming|throw(s|ing)? (the|my) (controller|keyboard))\b": "rage",
    r"\b(jump ?scare|scary|scared|horror|creepy)\b": "jumpscare",
    r"\b(rizz|date|dating|girlfriend|crush|shoot (my|your) shot)\b": "rizz",
    r"\b(react(ion|ing)?|watching this|look at this)\b": "reaction",
    r"\b(irl|outside|in public|on the street|walking around)\b": "irl stream",
    r"\b(donat(e|ed|ion)|gift(ed)? subs?|subscribers?|tts|super ?chat)\b": "donation",
    r"\b(drama|beef|exposed|called (me|him|her) out|diss)\b": "drama",
    r"\b(fight|fighting|swing|punch)\b": "fight",
    r"\b(challenge|bet|for \$?\d+)\b": "challenge",
    r"\b(clutch|1v\d|one v|ace|win(ning)? the game|victory royale)\b": "clutch",
    r"\b(freestyle|song|beat|bars|rap(ping)?|singing|music)\b": "music",
    r"\b(prank(ed)?|trolled|trolling)\b": "prank",
    r"\b(money|dollars|\$\d+|rich|broke)\b": "money",
    r"\b(food|eating|mukbang|taste|cook(ing)?)\b": "food",
    r"\b(lmao|laugh(ing)?|funny|i'?m crying|dying)\b": "funny moments",
}

GENERIC = ["twitch clips", "streamer clips", "funny stream moments", "shorts"]

STOP = set("""
a about after again all also am an and any are as at back be because been being but by can can't
come could did didn't do does doesn't doing don't down even every for from get getting go going gonna
got had has have having he her here him his how i i'm if in into is isn't it it's just know let like
look man me more my no not now of off oh ok okay on one only or our out over really right said say
see she so some still that that's the their them then there they they're this thing think to too up
us very wait want was wasn't we well were what when where which who why will with would yeah yes yo
you you're your bro dude guys chat lol what's gotta wanna kinda yeah, hey uh um hmm
""".split())


def slug(tag: str) -> str:
    """'Kai Cenat' -> 'kaicenat' (for hashtags)."""
    return re.sub(r"[^a-z0-9]", "", tag.lower())


def topic_tags(text: str) -> list[str]:
    return [tag for pattern, tag in TOPICS.items() if re.search(pattern, text, re.I)]


def mentioned_names(words: list[str], known: dict[str, str] | None = None, limit: int = 3) -> list[str]:
    """People/games mentioned in the clip.

    - Any of your other streamers mentioned (even once) -> their name.
    - Other capitalised words in the middle of sentences that come up at least twice
      (a single capitalised word is often just Whisper starting a new sentence).
    """
    known = known or {}
    found: Counter[str] = Counter()
    hits: list[str] = []
    prev_end = True
    for raw in words:
        w = raw.strip(".,!?\"'()[]:;")
        low = w.lower()
        if low in known and w[:1].isupper():
            if known[low] not in hits:
                hits.append(known[low])
            prev_end = raw.endswith((".", "!", "?"))
            continue
        if w and not prev_end and w[0].isupper() and len(w) >= 3 and low not in STOP and w.isalpha() \
                and w.upper() != w:
            found[w] += 1
        prev_end = raw.endswith((".", "!", "?"))
    return hits + [w.lower() for w, n in found.most_common(limit) if n >= 2][: max(0, limit - len(hits))]


def build_tags(*, streamer_name: str, login: str, streamer_tags: list[str] | None, group: str | None,
               group_tags: dict[str, list[str]] | None, game: str | None, title: str | None,
               transcript: str | None, known_names: dict[str, str] | None = None, limit: int = 15) -> list[str]:
    """Ordered, de-duplicated tags (most important first)."""
    tags: list[str] = [streamer_name.lower(), login.lower()]
    tags += [t.lower() for t in (streamer_tags or [])]
    if group and group_tags:
        tags += [t.lower() for t in group_tags.get(group, [])]
    if game:
        tags.append(game.lower())
    text = f"{title or ''} {transcript or ''}"
    tags += topic_tags(text)
    tags += mentioned_names((transcript or "").split(), known_names)
    tags += [f"{streamer_name.lower()} clips", f"{streamer_name.lower()} funny moments"]
    tags += GENERIC
    out: list[str] = []
    for t in tags:
        t = re.sub(r"\s+", " ", t).strip()
        if t and t not in out and slug(t) not in {slug(x) for x in out}:
            out.append(t)
    # YouTube allows 500 characters of tags in total.
    total, kept = 0, []
    for t in out:
        if total + len(t) + 2 > 480 or len(kept) >= limit:
            break
        kept.append(t)
        total += len(t) + 2
    return kept


def hashtags(tags: list[str], limit: int = 5) -> str:
    """'#kaicenat #fortnite #rage ...' (YouTube shows the first 3 above the title)."""
    out = []
    for t in tags:
        h = slug(t)
        if h and h not in out and h not in ("shorts",):
            out.append(h)
        if len(out) >= limit:
            break
    return " ".join(f"#{h}" for h in ["shorts", *out])
