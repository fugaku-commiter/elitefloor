from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import discord
from discord.ext import commands

from .config import load_config
from .db import Database


logging.basicConfig(level=logging.INFO)


class METASKSBot(commands.Bot):
    def __init__(self) -> None:
        self.cfg = load_config()
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix=self.cfg.command_prefix, intents=intents)
        # attach db after super init
        self.db = Database(self.cfg.mongo_uri, self.cfg.mongo_db)

    async def setup_hook(self) -> None:
        await self.db.ensure_indexes()
        # load cogs
        await self.load_extension("METASKS.cogs.admin")
        await self.load_extension("METASKS.cogs.user")
        await self.load_extension("METASKS.cogs.public")

    async def on_ready(self) -> None:
        logging.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")
        try:
            synced = await self.tree.sync()
            logging.info("Synced %d app commands", len(synced))
        except Exception as exc:  # noqa: BLE001
            logging.exception("Failed to sync app commands: %s", exc)


def main() -> None:
    bot = METASKSBot()
    bot.run(bot.cfg.discord_token)


if __name__ == "__main__":
    main()


