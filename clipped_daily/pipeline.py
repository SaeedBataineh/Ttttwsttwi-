"""Fetch -> score -> pick -> download -> caption -> render. Runs in a worker thread."""
from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import gaming, scoring
from .db import DB, now_iso
from .media import captions, render
from .media.download import download_clip
from .settings import Settings
from .twitch import Twitch, parse_time

log = logging.getLogger(__name__)

ACTIVE = ("rendering", "review", "approved", "posted")
BASELINE_TTL_H = 12


class Pipeline:
    def __init__(self, settings: Settings, db: DB, twitch: Twitch | None = None):
        self.s = settings
        self.db = db
        self.twitch = twitch or Twitch(settings.env("TWITCH_CLIENT_ID"), settings.env("TWITCH_CLIENT_SECRET"))
        self._games: dict[str, str] = db.kv_get("games", {})

    # ------------------------------------------------------------------ helpers
    def start_of_today_utc(self) -> str:
        local = datetime.now(self.s.tz).replace(hour=0, minute=0, second=0, microsecond=0)
        return local.astimezone(timezone.utc).isoformat(timespec="seconds")

    def user_ids(self) -> dict[str, str]:
        cache: dict[str, str] = self.db.kv_get("user_ids", {})
        missing = [s.login for s in self.s.active_streamers() if s.login not in cache]
        if missing:
            found = self.twitch.users(missing)
            for login in missing:
                if login in found:
                    cache[login] = found[login]["id"]
                else:
                    log.warning("Twitch user '%s' not found - check streamers.yaml", login)
            self.db.kv_set("user_ids", cache)
        return cache

    def baseline(self, login: str, broadcaster_id: str, now: datetime) -> float:
        key = f"baseline2:{login}"
        cached = self.db.kv_get(key)
        if cached and now.timestamp() - cached["ts"] < BASELINE_TTL_H * 3600:
            return cached["value"]
        week = self.twitch.clips(broadcaster_id, now - timedelta(days=7), now, limit=300)
        value = scoring.baseline_views(week)
        self.db.kv_set(key, {"value": value, "ts": now.timestamp()})
        return value

    def game_names(self, ids: list[str]) -> dict[str, str]:
        missing = [i for i in ids if i and i not in self._games]
        if missing:
            self._games.update(self.twitch.games(missing))
            self.db.kv_set("games", self._games)
        return self._games

    # ------------------------------------------------------------------ fetch
    def fetch(self) -> int:
        """Pull top recent clips for every streamer and store/refresh them. Returns clips seen."""
        now = datetime.now(timezone.utc)
        since = now - timedelta(hours=self.s.get("lookback_hours", 24))
        ids = self.user_ids()
        seen = 0
        for st in self.s.active_streamers():
            bid = ids.get(st.login)
            if not bid:
                continue
            try:
                base = self.baseline(st.login, bid, now)
                clips = self.twitch.clips(bid, since, now, limit=self.s.get("clips_per_streamer", 100))
            except Exception as e:
                log.error("Fetching clips for %s failed: %s", st.login, e)
                continue
            games = self.game_names([c.get("game_id") for c in clips])
            for c in clips:
                self.db.upsert_clip({
                    "id": c["id"],
                    "url": c["url"],
                    "broadcaster_id": bid,
                    "broadcaster": st.login,
                    "creator_name": c.get("creator_name"),
                    "video_id": c.get("video_id") or None,
                    "vod_offset": c.get("vod_offset"),
                    "game_id": c.get("game_id"),
                    "game_name": games.get(c.get("game_id") or ""),
                    "title": c.get("title"),
                    "view_count": c.get("view_count", 0),
                    "duration": c.get("duration"),
                    "created_at": c["created_at"],
                    "score": scoring.score(c.get("view_count", 0), c["created_at"], base, now),
                })
                seen += 1
            log.info("%s: %d clips (baseline %.0f views)", st.login, len(clips), base)
        return seen

    # ------------------------------------------------------------------ select
    def select(self, limit: int) -> list[dict]:
        """Pick the best unreviewed clips, respecting caps and skipping duplicates."""
        s = self.s
        pending = len(self.db.by_status("rendering", "review"))
        room = max(0, min(limit, s.get("max_pending_review", 15) - pending))
        if room == 0:
            return []
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=s.get("lookback_hours", 24))).isoformat()
        today = self.start_of_today_utc()
        per_day = s.get("max_review_per_streamer_per_day", 4)
        group_of = {st.login: st.group for st in s.streamers}
        allowed = {st.login for st in s.active_streamers()}

        active = [dict(r) for r in self.db.by_status(*ACTIVE)]
        counts: dict[str, int] = {}
        for r in self.db.query("SELECT broadcaster FROM clips WHERE rendered_at >= ?", (today,)):
            counts[r["broadcaster"]] = counts.get(r["broadcaster"], 0) + 1

        picked: list[dict] = []
        for row in self.db.by_status("seen"):
            c = dict(row)
            if c["broadcaster"] not in allowed:
                continue
            if c["created_at"] < cutoff[:19]:
                continue
            if c["view_count"] < s.get("min_views", 300):
                continue
            if not (s.get("min_duration", 8) <= (c["duration"] or 0) <= s.get("max_duration", 60)):
                continue
            if counts.get(c["broadcaster"], 0) >= per_day:
                continue
            dup = scoring.is_duplicate(c, active + picked, group_of)
            if dup:
                self.db.update_clip(c["id"], status="duplicate", error=f"duplicate of {dup}")
                continue
            picked.append(c)
            counts[c["broadcaster"]] = counts.get(c["broadcaster"], 0) + 1
            if len(picked) >= room:
                break
        return picked

    # ------------------------------------------------------------------ render
    def clip_dir(self, clip_id: str) -> Path:
        return self.s.work_dir / clip_id

    def process(self, c: dict) -> None:
        """Download + caption + render one clip, leaving it in 'review'."""
        st = self.s.streamer(c["broadcaster"])
        d = self.clip_dir(c["id"])
        self.db.update_clip(c["id"], status="rendering", work_dir=str(d), rendered_at=now_iso())
        try:
            src = download_clip(c["url"], d)
            render_files(self.s, src, d, st.facecam if st else None, c["title"])
            title = scoring.make_title(self.s.get("title_template", "{streamer}: {title}"),
                                       st.name if st else c["broadcaster"], c["title"], c.get("game_name"))
            self.db.update_clip(c["id"], status="review", post_title=title, flags=scoring.flags_for(c), error=None)
            log.info("Rendered %s (%s)", c["id"], c["broadcaster"])
        except Exception as e:
            log.exception("Render failed for %s", c["id"])
            self.db.update_clip(c["id"], status="failed", error=str(e)[:1000])

    # ------------------------------------------------------------------ cycle
    def run_cycle(self, render_enabled: bool = True) -> list[str]:
        """One full check. Returns ids of clips that are newly ready for review."""
        # Recover clips stuck mid-render after a crash/restart.
        for r in self.db.by_status("rendering"):
            self.db.update_clip(r["id"], status="seen")
        self.fetch()
        self.cleanup()
        if not render_enabled:
            return []
        gaming_now, why = gaming.is_gaming(self.s, self.db)
        if gaming_now:
            log.info("Skipping renders: %s", why)
            return []
        ready = []
        try:
            for c in self.select(self.s.get("renders_per_cycle", 5)):
                self.process(c)
                if self.db.clip(c["id"])["status"] == "review":
                    ready.append(c["id"])
        finally:
            captions.unload()
        return ready

    def retry_failed(self) -> int:
        """Give failed clips another chance (called when the bot starts)."""
        rows = self.db.by_status("failed")
        for r in rows:
            self.db.update_clip(r["id"], status="seen", error=None)
        return len(rows)

    def cleanup(self) -> None:
        days = self.s.get("cleanup_days", 3)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        for r in self.db.query(
            "SELECT id, work_dir FROM clips WHERE status IN ('posted','rejected','duplicate','failed','skipped') "
            "AND work_dir IS NOT NULL AND updated_at < ?", (cutoff,)):
            shutil.rmtree(r["work_dir"], ignore_errors=True)
            self.db.update_clip(r["id"], work_dir=None)


def render_files(s: Settings, src: Path, d: Path, facecam: dict | None, twitch_title: str | None) -> Path:
    """Captions + vertical render + Discord preview for a downloaded clip. Returns final.mp4."""
    rcfg = s.render
    duration = render.probe_duration(src)
    layout = "facecam" if facecam else "blur"
    fh = int(rcfg.get("facecam_height", 640))
    pos = render.layout_positions(layout, fh)
    chunks = []
    if rcfg.get("captions", True):
        try:
            words = captions.transcribe(src, rcfg.get("whisper_model", "small"), rcfg.get("whisper_device", "auto"))
            chunks = captions.chunk_words(words)
        except Exception:
            log.exception("Captions failed - rendering without them")
    hook = twitch_title if (rcfg.get("hook_title", True) and pos["hook_y"] and twitch_title) else None
    ass = captions.build_ass(d / "subs.ass", chunks, caption_y=pos["caption_y"], duration=duration,
                             hook=hook, hook_y=pos["hook_y"] or 0, watermark=rcfg.get("watermark"))
    final = render.render_vertical(src, d / "final.mp4", layout=layout, facecam=facecam, ass_path=ass,
                                   encoder=rcfg.get("encoder", "auto"), facecam_height=fh)
    render.render_preview(final, d / "preview.mp4", rcfg.get("encoder", "auto"))
    return final


def results_of(row) -> dict:
    try:
        return json.loads(row["results"] or "{}")
    except Exception:
        return {}


def flags_of(row) -> list[str]:
    try:
        return json.loads(row["flags"] or "[]")
    except Exception:
        return []


def age_text(created_at: str) -> str:
    mins = int((datetime.now(timezone.utc) - parse_time(created_at)).total_seconds() // 60)
    return f"{mins}m ago" if mins < 60 else f"{mins // 60}h {mins % 60}m ago"


__all__ = ["Pipeline", "results_of", "flags_of", "age_text", "now_iso"]
