from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from ..config import load_config
from ..db import Database
from ..utils.embeds import info_embed, error_embed
from ..utils.solana import is_valid_solana_address


LOOT_HIERARCHY = {
    "Elite": 5,
    "Very High": 4,
    "High": 3,
    "Medium": 2,
    "Low": 1,
}


def shorten_wallet(addr: str) -> str:
    return addr if len(addr) <= 12 else f"{addr[:6]}...{addr[-6:]}"


class OwnedNFTsView(discord.ui.View):
    def __init__(self, nfts: List[Dict[str, Any]], wallet: str, color: discord.Color) -> None:
        super().__init__(timeout=180)
        self.nfts = nfts
        self.wallet = wallet
        self.color = color

    @discord.ui.button(label="Owned NFTs", style=discord.ButtonStyle.primary)
    async def show(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        button.disabled = True
        await interaction.message.edit(view=self)

        # Build list
        nft_list: List[Dict[str, Any]] = []
        for nft in self.nfts:
            if nft.get("collection") != "marketelites":
                continue
            name = nft.get("name", "Unknown")
            mint_address = nft.get("mintAddress", "")
            traits = nft.get("attributes") or nft.get("traits") or []
            loot_value = "Unknown"
            percent_value = 0.0
            for trait in traits:
                t = (trait.get("trait_type") or trait.get("traitType") or "").lower()
                v = trait.get("value")
                if t == "loot":
                    loot_value = str(v)
                elif t == "percent":
                    try:
                        percent_value = float(str(v).replace("%", "").strip())
                    except Exception:
                        percent_value = 0.0
            allocation_amount = (percent_value / 100.0) * 5_983_674
            url = f"https://magiceden.io/item-details/{mint_address}"
            nft_list.append({
                "name": name,
                "loot_value": loot_value,
                "url": url,
                "allocation_amount": allocation_amount,
            })

        nft_list.sort(key=lambda x: x["allocation_amount"], reverse=True)
        top = nft_list[:25]
        if not top:
            await interaction.response.send_message(
                embed=info_embed("Owned NFTs", f"No 'marketelites' NFTs found for {self.wallet}"),
                ephemeral=True,
            )
            return

        lines = [f"[{n['name']} ({n['loot_value']})]({n['url']}) - Allo: ${n['allocation_amount']:,.2f}" for n in top]
        desc = "\n".join(lines)
        if len(desc) > 4096:
            desc = desc[:4093] + "..."
        embed = discord.Embed(title=f"Top 25 Owned NFTs for {self.wallet}", description=desc, color=self.color)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class LeaderboardView(discord.ui.View):
    def __init__(self, entries: List[Tuple[str, Dict[str, Any]]], title: str, per_page: int = 10) -> None:
        super().__init__(timeout=180)
        self.entries = entries
        self.title = title
        self.per_page = per_page
        self.page = 0
        self.total_pages = max(1, (len(entries) - 1) // per_page + 1)
        self.embed = discord.Embed(title=f"{self.title} (Page 1/{self.total_pages})", color=discord.Color.gold())
        self._render()

    def _render(self) -> None:
        start = self.page * self.per_page
        end = start + self.per_page
        lines: List[str] = []
        for idx, (wallet, data) in enumerate(self.entries[start:end], start=start + 1):
            if self.title.startswith("NFT Holders"):
                lines.append(f"**#{idx}** - `{shorten_wallet(wallet)}`: {int(data.get('total_nfts', 0))} NFTs")
            else:
                lines.append(f"**#{idx}** - `{shorten_wallet(wallet)}`: ${float(data.get('total_allocation', 0.0)):,.2f}")
        self.embed.description = "\n".join(lines) if lines else "No data"
        self.embed.title = f"{self.title} (Page {self.page + 1}/{self.total_pages})"

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.page > 0:
            self.page -= 1
            self._render()
            await interaction.message.edit(embed=self.embed, view=self)
        await interaction.response.defer()

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.page < self.total_pages - 1:
            self.page += 1
            self._render()
            await interaction.message.edit(embed=self.embed, view=self)
        await interaction.response.defer()


class PublicCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db
        self.cfg = load_config()

    async def _fetch_nfts(self, wallet: str) -> List[Dict[str, Any]]:
        url = f"{self.cfg.magic_eden_base_url}/wallets/{wallet}/tokens?offset=0&limit=500"
        async with aiohttp.ClientSession() as session:
            while True:
                async with session.get(url, timeout=30) as resp:
                    if resp.status == 200:
                        nfts = await resp.json()
                        return [n for n in nfts if n.get("collection") == "marketelites"]
                    if resp.status in (500, 503):
                        await interaction.followup.send("Retrying Magic Eden...", ephemeral=True)  # type: ignore[name-defined]
                        continue
                    return []

    @app_commands.command(name="rank", description="Show allocation and loot rank for a wallet")
    async def rank(self, interaction: discord.Interaction, wallet: str) -> None:
        await interaction.response.defer(thinking=True)
        if not is_valid_solana_address(wallet):
            await interaction.followup.send("Invalid Solana wallet address.", ephemeral=True)
            return
        # Combine alt wallets
        doc = await self.db.combines.find_one({"_id": wallet})
        wallets = [wallet] + (doc.get("alts", []) if doc else [])

        # Fetch NFTs and compute
        total_percent = 0.0
        total_nfts = 0
        loot_counts: Dict[str, int] = {k: 0 for k in LOOT_HIERARCHY}
        highest_loot: Optional[str] = None
        collected_nfts: List[Dict[str, Any]] = []

        async with aiohttp.ClientSession() as session:
            for w in wallets:
                url = f"{self.cfg.magic_eden_base_url}/wallets/{w}/tokens?offset=0&limit=500"
                async with session.get(url, timeout=30) as resp:
                    if resp.status != 200:
                        continue
                    nfts = await resp.json()
                    for nft in nfts:
                        if nft.get("collection") != "marketelites":
                            continue
                        collected_nfts.append(nft)
                        total_nfts += 1
                        traits = nft.get("attributes") or nft.get("traits") or []
                        for trait in traits:
                            t = (trait.get("trait_type") or trait.get("traitType") or "").lower()
                            v = trait.get("value")
                            if t == "percent":
                                try:
                                    total_percent += float(str(v).replace("%", "").strip())
                                except Exception:
                                    pass
                            elif t == "loot":
                                loot_cap = str(v).capitalize()
                                loot_counts[loot_cap] = loot_counts.get(loot_cap, 0) + 1
                                if highest_loot is None or LOOT_HIERARCHY.get(loot_cap, 0) > LOOT_HIERARCHY.get(highest_loot, 0):
                                    highest_loot = loot_cap

        if total_nfts == 0:
            await interaction.followup.send(embed=error_embed("Rank", "No 'marketelites' NFTs found."), ephemeral=True)
            return

        total_allocation = (total_percent / 100.0) * 5_983_674

        # Persist latest stats for main wallet
        await self.db.zstats.update_one(
            {"_id": wallet},
            {"$set": {
                "total_allocation": float(total_allocation),
                "total_nfts": int(total_nfts),
                "loot_counts": loot_counts,
                "highest_loot": highest_loot,
            }},
            upsert=True,
        )

        # Build ranks across all wallets
        all_docs = await self.db.zstats.find().to_list(length=100000)
        def rank_key_allo(d):
            return (
                -float(d.get("total_allocation", 0.0)),
                -LOOT_HIERARCHY.get(d.get("highest_loot", ""), 0),
                -int((d.get("loot_counts") or {}).get(d.get("highest_loot", ""), 0)),
            )
        sorted_allo = sorted(all_docs, key=rank_key_allo)
        allo_rank = next((i + 1 for i, d in enumerate(sorted_allo) if d.get("_id") == wallet), None)
        sorted_nft = sorted(all_docs, key=lambda d: -int(d.get("total_nfts", 0)))
        nft_rank = next((i + 1 for i, d in enumerate(sorted_nft) if d.get("_id") == wallet), None)

        color = interaction.user.color if isinstance(interaction.user, discord.Member) and interaction.user.color.value else discord.Color.light_grey()
        embed = discord.Embed(title=f"NFT Holder Rank for {wallet}", color=color)
        embed.add_field(name="Total $ Allocation", value=f"${total_allocation:,.2f}", inline=False)
        embed.add_field(name="Total NFTs Held", value=str(total_nfts), inline=False)
        embed.add_field(name="Allocation Rank", value=f"#{allo_rank}", inline=True)
        embed.add_field(name="NFT Holder Rank", value=f"#{nft_rank}", inline=True)
        embed.add_field(name="Highest Loot", value=str(highest_loot or "None"), inline=False)

        # Loot counts sorted by hierarchy
        sorted_loot = sorted((loot_counts or {}).items(), key=lambda x: LOOT_HIERARCHY.get(x[0], 0), reverse=True)
        loot_lines = [f"{k}: {v}" for k, v in sorted_loot if v > 0]
        embed.add_field(name="Loot Trait Counts", value="\n".join(loot_lines) or "None", inline=False)

        view = OwnedNFTsView(collected_nfts, wallet, color)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="leaderboard_nft", description="Leaderboard by NFT count")
    async def leaderboard_nft(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        stats = await self.db.zstats.find().to_list(length=100000)
        entries = [(d["_id"], d) for d in sorted(stats, key=lambda x: -int(x.get("total_nfts", 0)))]
        view = LeaderboardView(entries, "NFT Holders Leaderboard")
        msg = await interaction.followup.send(embed=view.embed, view=view)
        view.message = msg  # type: ignore[attr-defined]
        # Also attach CSV export
        rows = [["discord_user_id","username","wallets","rank","amount"]]
        for idx,(w,d) in enumerate(entries, start=1):
            rows.append(["", "", w, str(idx), str(int(d.get("total_nfts", 0)))])
        import csv as _csv, io as _io
        buf = _io.StringIO(); wr = _csv.writer(buf); wr.writerows(rows)
        data = buf.getvalue().encode("utf-8")
        await interaction.followup.send(file=discord.File(fp=io.BytesIO(data), filename="leaderboard_nft.csv"), ephemeral=True)

    @app_commands.command(name="leaderboard_allo", description="Leaderboard by allocation")
    async def leaderboard_allo(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        stats = await self.db.zstats.find().to_list(length=100000)
        entries = [(d["_id"], d) for d in sorted(stats, key=lambda x: -float(x.get("total_allocation", 0.0)))]
        view = LeaderboardView(entries, "Allocation Leaderboard")
        msg = await interaction.followup.send(embed=view.embed, view=view)
        view.message = msg  # type: ignore[attr-defined]
        # Also attach CSV export
        rows = [["discord_user_id","username","wallets","rank","amount"]]
        for idx,(w,d) in enumerate(entries, start=1):
            rows.append(["", "", w, str(idx), f"{float(d.get('total_allocation', 0.0)):.2f}"])
        import csv as _csv, io as _io
        buf = _io.StringIO(); wr = _csv.writer(buf); wr.writerows(rows)
        data = buf.getvalue().encode("utf-8")
        await interaction.followup.send(file=discord.File(fp=io.BytesIO(data), filename="leaderboard_allo.csv"), ephemeral=True)

    @app_commands.command(name="top", description="Allocation needed to reach rank N (optionally for wallet)")
    async def top(self, interaction: discord.Interaction, rank_number: int, wallet: Optional[str] = None) -> None:
        await interaction.response.defer(thinking=True)
        if wallet and not is_valid_solana_address(wallet):
            await interaction.followup.send("Invalid Solana wallet address.", ephemeral=True)
            return
        stats = await self.db.zstats.find().to_list(length=100000)
        if not stats:
            await interaction.followup.send("No data available.", ephemeral=True)
            return
        sorted_allo = sorted(stats, key=lambda x: -float(x.get("total_allocation", 0.0)))
        if rank_number < 1 or rank_number > len(sorted_allo):
            await interaction.followup.send(f"Rank must be between 1 and {len(sorted_allo)}.", ephemeral=True)
            return
        target = float(sorted_allo[rank_number - 1].get("total_allocation", 0.0))
        if wallet:
            doc = next((d for d in sorted_allo if d.get("_id") == wallet), None)
            if not doc:
                await interaction.followup.send("Wallet not found in data.", ephemeral=True)
                return
            diff = target - float(doc.get("total_allocation", 0.0))
            if diff > 0:
                await interaction.followup.send(f"Wallet `{wallet}` needs an additional ${diff:,.2f} to reach rank #{rank_number}.", ephemeral=True)
            else:
                await interaction.followup.send(f"Wallet `{wallet}` is already above rank #{rank_number} by ${-diff:,.2f}.", ephemeral=True)
        else:
            await interaction.followup.send(f"To be ranked #{rank_number}, you need an allocation of at least ${target:,.2f}.", ephemeral=True)

    @app_commands.command(name="omt", description="Count wallets with allocation >= amount (1% undercut applied)")
    async def omt(self, interaction: discord.Interaction, amount: float) -> None:
        await interaction.response.defer(thinking=True)
        adjusted = amount * 0.99
        stats = await self.db.zstats.find({"total_allocation": {"$gte": adjusted}}).count()  # type: ignore[attr-defined]
        await interaction.followup.send(f"There are {int(stats)} wallets with allocation >= ${amount:,.2f}.", ephemeral=True)

    # Removed /token per request to keep only leaderboards, rank, and omt


async def setup(bot: commands.Bot) -> None:
    db: Database = bot.db  # type: ignore[attr-defined]
    await bot.add_cog(PublicCog(bot, db))


