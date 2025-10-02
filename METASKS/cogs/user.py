from __future__ import annotations

from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from ..db import Database
from ..utils.embeds import info_embed, success_embed, error_embed
from ..utils.solana import is_valid_solana_address


class UserCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

    group = app_commands.Group(name="me", description="Your METASKS controls")

    # Removed wallets_show since wallets are visible via the Dashboard panel

    @group.command(name="wallets_add", description="Add wallets (space or newline separated)")
    async def wallets_add(self, interaction: discord.Interaction, wallets_text: str) -> None:
        wallets = [w.strip() for w in wallets_text.replace("\n", " ").split(" ") if w.strip()]
        if not wallets:
            await interaction.response.send_message("Provide at least one wallet.", ephemeral=True)
            return
        valid: list[str] = []
        invalid: list[str] = []
        seen: set[str] = set()
        for w in wallets:
            if w in seen:
                continue
            seen.add(w)
            if is_valid_solana_address(w):
                valid.append(w)
            else:
                invalid.append(w)
        if not valid:
            await interaction.response.send_message(
                error_embed("Wallets Rejected", "No valid Solana addresses were provided."),
                ephemeral=True,
            )
            return
        if not wallets:
            await interaction.response.send_message("Provide at least one wallet.", ephemeral=True)
            return
        user_id = str(interaction.user.id)
        await self.db.users.update_one(
            {"_id": user_id},
            {"$addToSet": {"wallets": {"$each": valid}}, "$setOnInsert": {"completed_tasks": []}},
            upsert=True,
        )
        msg = f"Added {len(valid)} wallet(s)."
        if invalid:
            msg += f" Rejected {len(invalid)} invalid address(es)."
        await interaction.response.send_message(success_embed("Wallets Added", msg), ephemeral=True)

    # Removed tasks since tasks are accessible through the panel flow

    # Removed dashboard since it's provided via the main panel


async def setup(bot: commands.Bot) -> None:
    db: Database = bot.db  # type: ignore[attr-defined]
    cog = UserCog(bot, db)
    await bot.add_cog(cog)
    # Add slash group commands
    # Avoid double-registration if extension reloads
    if bot.tree.get_command("me") is None:
        bot.tree.add_command(cog.group)


