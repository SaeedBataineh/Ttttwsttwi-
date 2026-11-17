"""Discord bot: review clips with buttons, manage the queue, and run the schedule."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks

from . import gaming
from .db import DB, now_iso
from .pipeline import Pipeline, age_text, flags_of, results_of, tags_of
from .publish import publish
from .settings import Settings, Streamer

log = logging.getLogger(__name__)

GREEN, RED, BLURPLE, GREY = 0x2ECC71, 0xE74C3C, 0x5865F2, 0x95A5A6


class EditTitleModal(discord.ui.Modal, title="Edit title & tags"):
    def __init__(self, bot: "ClippedBot", clip_id: str, current: str, tags: list[str]):
        super().__init__(timeout=600)
        self.bot = bot
        self.clip_id = clip_id
        self.new_title = discord.ui.TextInput(label="Title", default=current[:100], max_length=95)
        self.new_tags = discord.ui.TextInput(label="Tags (comma separated)", default=", ".join(tags)[:450],
                                             style=discord.TextStyle.paragraph, max_length=480, required=False)
        self.add_item(self.new_title)
        self.add_item(self.new_tags)

    async def on_submit(self, interaction: discord.Interaction):
        tags = [x.strip().lower() for x in str(self.new_tags.value or "").split(",") if x.strip()]
        self.bot.db.update_clip(self.clip_id, post_title=str(self.new_title.value).strip(), tags=tags)
        row = self.bot.db.clip(self.clip_id)
        try:
            await interaction.response.edit_message(view=self.bot.review_card(row))
        except discord.HTTPException:
            if not interaction.response.is_done():
                await interaction.response.send_message("Title saved.", ephemeral=True)


class ClippedBot(discord.Client):
    def __init__(self, settings: Settings, db: DB, pipeline: Pipeline):
        super().__init__(intents=discord.Intents.default())
        self.s, self.db, self.pipe = settings, db, pipeline
        self.tree = app_commands.CommandTree(self)
        self.guild_obj = discord.Object(id=int(settings.env("DISCORD_GUILD_ID")))
        self.review_id = int(settings.env("DISCORD_REVIEW_CHANNEL_ID"))
        self.queue_id = int(settings.env("DISCORD_QUEUE_CHANNEL_ID", False) or self.review_id)
        self.posted_id = int(settings.env("DISCORD_POSTED_CHANNEL_ID", False) or self.review_id)
        admins = settings.env("DISCORD_ADMIN_IDS", False)
        self.admins = {int(x) for x in admins.split(",") if x.strip()} if admins else set()
        self.cycle_lock = asyncio.Lock()
        self.post_lock = asyncio.Lock()

    # ================================================================ lifecycle
    async def setup_hook(self):
        if self.db.kv_get("review_layout") != "card":
            self.db.execute("UPDATE clips SET review_message_id=NULL WHERE status='review'")
            self.db.kv_set("review_layout", "card")
        n = self.pipe.retry_failed()
        if n:
            log.info("Retrying %d previously failed clip(s)", n)
        self._register_commands()
        await self.tree.sync(guild=self.guild_obj)
        self.fetch_loop.change_interval(minutes=float(self.s.get("fetch_interval_minutes", 45)))
        self.fetch_loop.start()
        self.post_loop.start()

    async def on_ready(self):
        log.info("Logged in as %s", self.user)
        await self.sync_deleted_reviews()
        await self.post_reviews()

    async def sync_deleted_reviews(self) -> None:
        """Clips whose review card was deleted while the bot was off count as rejected."""
        rows = self.db.query("SELECT id, review_message_id FROM clips WHERE status='review' "
                             "AND review_message_id IS NOT NULL")
        if not rows:
            return
        ch = await self.channel(self.review_id)
        gone = 0
        for r in rows:
            try:
                await ch.fetch_message(int(r["review_message_id"]))
            except discord.NotFound:
                self.db.update_clip(r["id"], status="rejected", error="review card deleted")
                gone += 1
            except discord.HTTPException:
                pass
        if gone:
            log.info("%d review card(s) were deleted in Discord - treated as rejected", gone)

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        """Deleting a review card in Discord = reject that clip."""
        if payload.channel_id != self.review_id:
            return
        row = self.db.one("SELECT id, status FROM clips WHERE review_message_id=?", (str(payload.message_id),))
        if row and row["status"] == "review":
            self.db.update_clip(row["id"], status="rejected", error="review card deleted")
            log.info("Review card for %s deleted - marked as rejected", row["id"])

    @tasks.loop(minutes=45)
    async def fetch_loop(self):
        await self.run_cycle()

    @fetch_loop.before_loop
    async def _before_fetch(self):
        await self.wait_until_ready()

    @tasks.loop(seconds=30)
    async def post_loop(self):
        now = datetime.now(self.s.tz)
        slot = now.strftime("%H:%M")
        if slot not in self.s.get("post_times", []):
            return
        key = f"{now.date()} {slot}"
        if self.db.kv_get("last_slot") == key:
            return
        self.db.kv_set("last_slot", key)
        await self.post_next()

    @post_loop.before_loop
    async def _before_post(self):
        await self.wait_until_ready()

    async def run_cycle(self) -> int:
        if self.cycle_lock.locked():
            return 0
        async with self.cycle_lock:
            try:
                ready = await asyncio.to_thread(self.pipe.run_cycle)
            except Exception:
                log.exception("Cycle failed")
                return 0
        await self.post_reviews()
        waiting = len(self.db.by_status("review"))
        mins = self.s.get("fetch_interval_minutes", 45)
        log.info("Check done: %d new clip(s) sent to #review, %d waiting for you. Next check in %s min "
                 "(or use /fetchnow in Discord). Bot is running - leave this window open.", len(ready), waiting, mins)
        return len(ready)

    # ================================================================ helpers
    def is_admin(self, user: discord.abc.User) -> bool:
        return not self.admins or user.id in self.admins

    def streamer_name(self, login: str) -> str:
        st = self.s.streamer(login)
        return st.name if st else login

    def next_slots(self, n: int) -> list[datetime]:
        tz = self.s.tz
        now = datetime.now(tz)
        times = sorted(self.s.get("post_times", []))
        out = []
        day = 0
        while len(out) < n and times and day < 30:
            d = (now + timedelta(days=day)).date()
            for t in times:
                h, m = map(int, t.split(":"))
                dt = datetime(d.year, d.month, d.day, h, m, tzinfo=tz)
                if dt > now:
                    out.append(dt)
                    if len(out) >= n:
                        break
            day += 1
        return out

    @staticmethod
    def media_name(row) -> str:
        return f"{row['broadcaster']}.mp4"

    def review_card(self, row) -> discord.ui.LayoutView:
        """One Discord card: title, video, details, warnings and buttons together."""
        ui = discord.ui
        status = row["status"]
        color = {"review": BLURPLE, "approved": GREEN, "posted": GREEN, "rejected": RED}.get(status, GREY)
        name = self.streamer_name(row["broadcaster"])
        title = (row["post_title"] or row["title"] or "clip").replace("[", "(").replace("]", ")")

        card = ui.Container(accent_colour=color)
        card.add_item(ui.TextDisplay(f"### [{title}]({row['url']})"))
        # The video lives on the Discord message itself, so edits keep showing it.
        card.add_item(ui.MediaGallery(discord.MediaGalleryItem(f"attachment://{self.media_name(row)}")))
        details = (f"**{name}** · 👁 {row['view_count']:,} views · 🔥 {row['score']:.2f} · "
                   f"⏱ {age_text(row['created_at'])} · {row['duration'] or 0:.0f}s · {row['game_name'] or '-'}")
        lines = [details]
        if row["title"] and row["title"] != row["post_title"]:
            lines.append(f"Twitch title: *{row['title'][:200]}*")
        tags = tags_of(row)
        if tags:
            lines.append(f"🏷️ {', '.join(tags[:10])}")
        flags = flags_of(row)
        if flags:
            lines.append("**⚠️ Check before approving**\n" + "\n".join(flags))
        card.add_item(ui.TextDisplay("\n".join(lines)))

        cid = row["id"]
        buttons: list[discord.ui.Button] = []
        if status == "review":
            buttons = [
                ui.Button(label="Approve", emoji="✅", style=discord.ButtonStyle.success, custom_id=f"cd:approve:{cid}"),
                ui.Button(label="Reject", emoji="❌", style=discord.ButtonStyle.danger, custom_id=f"cd:reject:{cid}"),
                ui.Button(label="Edit title & tags", emoji="✏️", style=discord.ButtonStyle.secondary, custom_id=f"cd:edit:{cid}"),
            ]
        elif status in ("approved", "rejected"):
            buttons = [ui.Button(label="Undo", emoji="↩️", style=discord.ButtonStyle.secondary, custom_id=f"cd:undo:{cid}")]
        if buttons:
            card.add_item(ui.Separator())
            card.add_item(ui.ActionRow(*buttons))

        footer = {
            "review": "Waiting for review",
            "approved": "✅ Approved - in the posting queue",
            "rejected": "❌ Rejected",
            "posted": "📤 Posted",
            "skipped": "⏭️ Skipped",
            "failed": f"⚠️ Failed: {(row['error'] or '')[:150]}",
        }.get(status, status)
        card.add_item(ui.TextDisplay(f"-# {footer} · id {cid}"))

        view = ui.LayoutView(timeout=None)
        view.add_item(card)
        return view

    async def channel(self, cid: int) -> discord.abc.Messageable:
        return self.get_channel(cid) or await self.fetch_channel(cid)

    async def post_reviews(self) -> None:
        rows = self.db.query("SELECT * FROM clips WHERE status='review' AND review_message_id IS NULL "
                             "ORDER BY score DESC")
        if not rows:
            return
        ch = await self.channel(self.review_id)
        for row in rows:
            preview = Path(row["work_dir"] or "") / "preview.mp4"
            try:
                if not preview.exists():
                    log.warning("Preview missing for %s, skipping", row["id"])
                    continue
                msg = await ch.send(view=self.review_card(row),
                                    file=discord.File(str(preview), filename=self.media_name(row)))
                self.db.update_clip(row["id"], review_message_id=str(msg.id))
            except Exception:
                log.exception("Could not post review for %s", row["id"])

    # ================================================================ buttons
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid = (interaction.data or {}).get("custom_id", "")
        if not cid.startswith("cd:"):
            return
        _, action, clip_id = cid.split(":", 2)
        if not self.is_admin(interaction.user):
            await interaction.response.send_message("You can't review clips.", ephemeral=True)
            return
        row = self.db.clip(clip_id)
        if not row:
            await interaction.response.send_message("Clip not found.", ephemeral=True)
            return

        if action == "edit":
            await interaction.response.send_modal(
                EditTitleModal(self, clip_id, row["post_title"] or row["title"], tags_of(row)))
            return
        if action == "approve" and row["status"] == "review":
            self.db.update_clip(clip_id, status="approved", approved_at=now_iso())
        elif action == "reject" and row["status"] == "review":
            self.db.update_clip(clip_id, status="rejected")
        elif action == "undo" and row["status"] in ("approved", "rejected"):
            self.db.update_clip(clip_id, status="review", approved_at=None)
        row = self.db.clip(clip_id)
        try:
            await interaction.response.edit_message(view=self.review_card(row))
        except discord.HTTPException:
            # Older-style message that can't be converted - the click still counted.
            if not interaction.response.is_done():
                await interaction.response.send_message(f"Done: **{row['status']}**.", ephemeral=True)

        if action == "approve" and row["status"] == "approved":
            pos = len(self.db.by_status("approved"))
            slots = self.next_slots(pos)
            when = f"<t:{int(slots[-1].timestamp())}:R>" if slots else "next slot"
            try:
                await (await self.channel(self.queue_id)).send(
                    f"✅ **{row['post_title']}** ({self.streamer_name(row['broadcaster'])}) - #{pos} in queue, "
                    f"posts around {when}")
            except Exception:
                log.exception("Could not write to queue channel")

    # ================================================================ posting
    async def post_next(self) -> str:
        async with self.post_lock:
            today = self.pipe.start_of_today_utc()
            cap = self.s.get("max_posts_per_streamer_per_day", 2)
            posted_today: dict[str, int] = {}
            for r in self.db.query("SELECT broadcaster FROM clips WHERE status='posted' AND posted_at >= ?", (today,)):
                posted_today[r["broadcaster"]] = posted_today.get(r["broadcaster"], 0) + 1
            row = next((r for r in self.db.by_status("approved", order="approved_at ASC")
                        if posted_today.get(r["broadcaster"], 0) < cap), None)
            if not row:
                return "Nothing to post (queue empty, or every queued streamer hit today's limit)."
            clip = dict(row)
            clip["_results"] = results_of(row)
            try:
                results, errors = await asyncio.to_thread(publish, self.s, clip)
            except Exception as e:
                results, errors = {}, {"all": str(e)}
            all_results = {**clip["_results"], **results}
            if not all_results and not errors:
                errors = {"all": "No platforms are enabled in config.yaml"}
            status = "posted" if all_results else "failed"
            self.db.update_clip(row["id"], status=status, results=all_results,
                                posted_at=now_iso() if all_results else None,
                                error="; ".join(f"{k}: {v}" for k, v in errors.items()) or None)

            name = self.streamer_name(row["broadcaster"])
            lines = [f"📤 **{row['post_title']}** ({name})" if all_results else f"⚠️ Failed to post **{row['post_title']}** ({name})"]
            lines += [f"• {k}: {v}" for k, v in all_results.items()]
            lines += [f"• {k} error: {v[:300]}" for k, v in errors.items()]
            msg = "\n".join(lines)
            try:
                await (await self.channel(self.posted_id)).send(msg)
                await self._refresh_review_message(row["id"])
            except Exception:
                log.exception("Could not report post result")
            return msg

    async def _refresh_review_message(self, clip_id: str) -> None:
        row = self.db.clip(clip_id)
        if not row or not row["review_message_id"]:
            return
        try:
            ch = await self.channel(self.review_id)
            msg = await ch.fetch_message(int(row["review_message_id"]))
            await msg.edit(view=self.review_card(row))
        except Exception:
            pass

    # ================================================================ commands
    def _register_commands(self):
        g = self.guild_obj
        bot = self

        async def guard(inter: discord.Interaction) -> bool:
            if bot.is_admin(inter.user):
                return True
            await inter.response.send_message("Not allowed.", ephemeral=True)
            return False

        @self.tree.command(name="queue", description="Show approved clips waiting to post", guild=g)
        async def queue(inter: discord.Interaction):
            rows = self.db.by_status("approved", order="approved_at ASC")
            waiting = len(self.db.by_status("review"))
            hint = f"\n👀 {waiting} clip(s) waiting for you in <#{self.review_id}>." if waiting else ""
            if not rows:
                await inter.response.send_message(
                    "📭 Queue is empty - nothing approved yet. Press ✅ Approve on a clip to add it." + hint,
                    ephemeral=True)
                return
            slots = self.next_slots(len(rows))
            lines = [f"📬 **{len(rows)} approved** - posting at {', '.join(self.s.get('post_times', []))}:{hint}"]
            for i, r in enumerate(rows):
                when = f"<t:{int(slots[i].timestamp())}:f>" if i < len(slots) else "later"
                lines.append(f"**{i + 1}.** {r['post_title']} ({self.streamer_name(r['broadcaster'])}) - {when}")
            await inter.response.send_message("\n".join(lines)[:1990], ephemeral=True)

        @self.tree.command(name="skip", description="Remove a clip from the queue by its /queue number", guild=g)
        async def skip(inter: discord.Interaction, position: int):
            if not await guard(inter):
                return
            rows = self.db.by_status("approved", order="approved_at ASC")
            if not 1 <= position <= len(rows):
                await inter.response.send_message("No clip at that position.", ephemeral=True)
                return
            r = rows[position - 1]
            self.db.update_clip(r["id"], status="skipped")
            await self._refresh_review_message(r["id"])
            await inter.response.send_message(f"⏭️ Skipped **{r['post_title']}**", ephemeral=True)

        @self.tree.command(name="postnow", description="Post the next approved clip right now", guild=g)
        async def postnow(inter: discord.Interaction):
            if not await guard(inter):
                return
            await inter.response.defer(ephemeral=True, thinking=True)
            await inter.followup.send((await self.post_next())[:1990], ephemeral=True)

        @self.tree.command(name="fetchnow", description="Check Twitch for new clips right now", guild=g)
        async def fetchnow(inter: discord.Interaction):
            if not await guard(inter):
                return
            if self.cycle_lock.locked():
                await inter.response.send_message("A check is already running.", ephemeral=True)
                return
            await inter.response.send_message("🔎 Checking Twitch… new clips will appear in review.", ephemeral=True)
            asyncio.create_task(self.run_cycle())

        @self.tree.command(name="gaming", description="Pause rendering while you play", guild=g)
        @app_commands.choices(mode=[app_commands.Choice(name=n, value=n) for n in ("on", "off", "auto")])
        async def gaming_cmd(inter: discord.Interaction, mode: app_commands.Choice[str]):
            if not await guard(inter):
                return
            gaming.set_mode(self.db, mode.value)
            active, why = gaming.is_gaming(self.s, self.db)
            await inter.response.send_message(
                f"🎮 Gaming mode: **{mode.value}** ({why}). Rendering is {'paused' if active else 'active'}.",
                ephemeral=True)

        @self.tree.command(name="status", description="Bot status", guild=g)
        async def status(inter: discord.Interaction):
            counts = {r["status"]: r["n"] for r in self.db.query("SELECT status, COUNT(*) n FROM clips GROUP BY status")}
            active, why = gaming.is_gaming(self.s, self.db)
            nxt = self.next_slots(1)
            lines = [
                f"👀 In review: **{counts.get('review', 0)}**  ✅ Queued: **{counts.get('approved', 0)}**  "
                f"📤 Posted: **{counts.get('posted', 0)}**  ⚠️ Failed: **{counts.get('failed', 0)}**",
                f"🎮 Gaming: {gaming.get_mode(self.db)} - {why} ({'rendering paused' if active else 'rendering on'})",
                f"⏰ Next post: <t:{int(nxt[0].timestamp())}:R>" if nxt else "⏰ No post times set",
                f"📺 Streamers: {', '.join(s.name for s in self.s.active_streamers())}",
            ]
            await inter.response.send_message("\n".join(lines), ephemeral=True)

        streamers = app_commands.Group(name="streamers", description="Manage streamers", guild_ids=[g.id])

        @streamers.command(name="list", description="List streamers")
        async def s_list(inter: discord.Interaction):
            lines = [f"• **{s.name}** (`{s.login}`){' - paused' if not s.allowed else ''}"
                     f"{' - facecam layout' if s.facecam else ''}" for s in self.s.streamers]
            await inter.response.send_message("\n".join(lines) or "No streamers.", ephemeral=True)

        @streamers.command(name="add", description="Add a Twitch streamer")
        async def s_add(inter: discord.Interaction, login: str, display_name: str | None = None):
            if not await guard(inter):
                return
            login = login.strip().lower().removeprefix("https://twitch.tv/").removeprefix("twitch.tv/")
            if self.s.streamer(login):
                await inter.response.send_message("Already on the list.", ephemeral=True)
                return
            await inter.response.defer(ephemeral=True)
            users = await asyncio.to_thread(self.pipe.twitch.users, [login])
            if login not in users:
                await inter.followup.send(f"Twitch user `{login}` not found.", ephemeral=True)
                return
            self.s.streamers.append(Streamer(login=login, name=display_name or users[login]["display_name"]))
            self.s.save_streamers()
            await inter.followup.send(f"➕ Added **{display_name or users[login]['display_name']}**.", ephemeral=True)

        @streamers.command(name="remove", description="Remove a streamer")
        async def s_remove(inter: discord.Interaction, login: str):
            if not await guard(inter):
                return
            st = self.s.streamer(login)
            if not st:
                await inter.response.send_message("Not on the list.", ephemeral=True)
                return
            self.s.streamers.remove(st)
            self.s.save_streamers()
            await inter.response.send_message(f"➖ Removed **{st.name}**.", ephemeral=True)

        self.tree.add_command(streamers)


def run_bot(settings: Settings, db: DB) -> None:
    bot = ClippedBot(settings, db, Pipeline(settings, db))
    bot.run(settings.env("DISCORD_TOKEN"), log_handler=None)
