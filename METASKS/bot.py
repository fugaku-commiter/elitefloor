from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import discord
from discord.ext import commands

from .config import load_config
from .db import Database
from .utils.embeds import info_embed, progress_embed, success_embed, error_embed


logging.basicConfig(level=logging.INFO)


class METASKSBot(commands.Bot):
    def __init__(self) -> None:
        self.cfg = load_config()
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix=self.cfg.command_prefix, intents=intents)
        # attach db after super init
        self.db = Database(self.cfg.mongo_uri, self.cfg.mongo_db)
        self._auto_task_started = False
        # Channels for automation
        self._auto_status_channel_id = int(os.getenv("AUTO_STATUS_CHANNEL_ID", "1247779174878412810"))
        self._panel_channel_id = int(os.getenv("AUTO_PANEL_CHANNEL_ID", "1318748930103709846"))

    async def setup_hook(self) -> None:
        await self.db.ensure_indexes()
        # load cogs
        await self.load_extension("METASKS.cogs.admin")
        await self.load_extension("METASKS.cogs.user")
        await self.load_extension("METASKS.cogs.public")
        # Register persistent views so panel buttons work after restarts
        try:
            from .cogs.admin import PanelView  # local import to avoid circulars at module import time
            self.add_view(PanelView(self.db))
        except Exception:
            pass

    async def on_ready(self) -> None:
        logging.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")
        try:
            synced = await self.tree.sync()
            logging.info("Synced %d slash commands", len(synced))
        except Exception as exc:
            logging.exception("Failed to sync commands: %s", exc)
        # Start background auto snapshot task once
        if not self._auto_task_started:
            self.loop.create_task(self._auto_snapshot_loop())
            self._auto_task_started = True

    async def _resolve_text_channel(self, channel_id: int) -> discord.TextChannel | None:
        ch = self.get_channel(channel_id)
        if isinstance(ch, discord.TextChannel):
            return ch
        try:
            ch_fetched = await self.fetch_channel(channel_id)
            return ch_fetched if isinstance(ch_fetched, discord.TextChannel) else None
        except Exception:
            return None

    async def _clear_channel_messages(self, channel: discord.TextChannel) -> None:
        try:
            await channel.purge(limit=1000)
        except Exception:
            # Fallback: delete last 200 individually
            try:
                async for msg in channel.history(limit=200):
                    try:
                        await msg.delete()
                    except Exception:
                        continue
            except Exception:
                pass

    async def _auto_snapshot_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                status_channel = await self._resolve_text_channel(self._auto_status_channel_id)
                if status_channel is not None:
                    try:
                        await status_channel.send(embed=info_embed("Autosnapshot", "Initiating auto snapshot"))
                    except Exception:
                        pass

                # Get AdminCog (provides snapshot service and helper)
                admin_cog = self.get_cog("AdminCog")
                if admin_cog is None:
                    # if not yet loaded, wait and retry next cycle
                    logging.warning("AdminCog not available; skipping autosnapshot cycle")
                else:
                    # Announce and fetch holders
                    if status_channel is not None:
                        try:
                            await status_channel.send(embed=info_embed("Autosnapshot", "Fetching holders..."))
                        except Exception:
                            pass
                    try:
                        await admin_cog.snapshot_service.start()
                        holders = await admin_cog.snapshot_service.fetch_collection_holders()
                        for w in holders:
                            await self.db.holders.update_one({"_id": w}, {"$set": {"_id": w}}, upsert=True)
                        if status_channel is not None:
                            try:
                                await status_channel.send(embed=success_embed("Autosnapshot", f"Fetched {len(holders)} holders."))
                            except Exception:
                                pass
                    except Exception as exc:  # noqa: BLE001
                        logging.exception("Auto fetch holders failed: %s", exc)

                    # Start snapshot with progress
                    progress_msg = None
                    if status_channel is not None:
                        try:
                            progress_msg = await status_channel.send(embed=info_embed("Autosnapshot", "Starting snapshot..."))
                        except Exception:
                            progress_msg = None
                    import uuid, time
                    job_id = str(uuid.uuid4())

                    async def updater(done: int, total: int):
                        try:
                            if progress_msg is not None:
                                await progress_msg.edit(embed=progress_embed("Autosnapshot Progress", done, total, "Running snapshot..."))
                        except Exception:
                            pass

                    try:
                        await admin_cog.snapshot_service.run_snapshot_from_db(job_id, progress_cb=updater)
                        if status_channel is not None:
                            try:
                                await status_channel.send(embed=success_embed("Autosnapshot", "Snapshot completed."))
                            except Exception:
                                pass
                        # Update bunker roles and notify (runs inline here; it rate-limits messages)
                        try:
                            result = await admin_cog.update_bunker_roles_and_notify()
                            if status_channel is not None and isinstance(result, dict):
                                try:
                                    th = result.get("threshold")
                                    add_c = result.get("added")
                                    rem_c = result.get("removed")
                                    elig = result.get("eligible")
                                    ok = result.get("ok")
                                    desc = f"ok={ok}\nthreshold={th}\neligible={elig}\nadded={add_c}\nremoved={rem_c}"
                                    await status_channel.send(embed=info_embed("Bunker Update Summary", desc))
                                except Exception:
                                    pass
                        except Exception as exc:  # noqa: BLE001
                            logging.exception("Bunker role update failed: %s", exc)
                    except Exception as exc:  # noqa: BLE001
                        logging.exception("Auto snapshot failed: %s", exc)
                        if status_channel is not None:
                            try:
                                await status_channel.send(embed=error_embed("Autosnapshot", f"Snapshot failed: {exc}"))
                            except Exception:
                                pass

                # Auto-send panel: clear target channel then post panel
                panel_channel = await self._resolve_text_channel(self._panel_channel_id)
                if panel_channel is not None:
                    # Clear messages best-effort; do not abort on failure
                    await self._clear_channel_messages(panel_channel)
                    # Send panel; report errors to status channel
                    try:
                        from .cogs.admin import PanelView  # local import
                        view = PanelView(self.db)  # persistent view already registered
                        embed = discord.Embed(
                            title="Elite Floor Panel",
                            description="Click 'Dashboard' for your stats, wallets, and tasks.",
                            color=discord.Color.green(),
                        )
                        await panel_channel.send(embed=embed, view=view)
                        if status_channel is not None:
                            try:
                                await status_channel.send(embed=success_embed("Autosnapshot", "Panel posted."))
                            except Exception:
                                pass
                    except Exception as exc:  # noqa: BLE001
                        logging.exception("Auto panel post failed: %s", exc)
                        if status_channel is not None:
                            try:
                                await status_channel.send(embed=error_embed("Autosnapshot", f"Panel post failed: {exc}"))
                            except Exception:
                                pass
            except Exception as exc:  # noqa: BLE001
                logging.exception("Auto snapshot loop error: %s", exc)
            # Sleep 24 hours
            await asyncio.sleep(24 * 60 * 60)


def main() -> None:
    bot = METASKSBot()
    bot.run(bot.cfg.discord_token)


if __name__ == "__main__":
    main()


