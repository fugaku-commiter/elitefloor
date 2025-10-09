from __future__ import annotations

import asyncio
import io
import json
import os
import uuid
from typing import Any, List

import aiohttp
import logging
import csv
import discord
from discord import app_commands
from discord.ext import commands
from urllib.parse import urlparse, parse_qs

from ..config import load_config
from ..db import Database
from ..services.snapshot import SnapshotService
from ..utils.embeds import error_embed, info_embed, progress_embed, success_embed
from ..utils.solana import is_valid_solana_address
from ..config import load_config


def is_admin():
    def predicate(interaction: discord.Interaction) -> bool:
        cfg = load_config()
        # Admin only if member has the configured admin role ID
        try:
            member: discord.Member = interaction.user  # type: ignore[assignment]
            admin_roles = set(getattr(cfg, 'admin_role_ids', [cfg.z_id]))
            has_role = any(r.id in admin_roles for r in getattr(member, 'roles', []))
        except Exception:
            has_role = False
        return has_role
    return app_commands.check(predicate)

def has_admin_access(interaction: discord.Interaction) -> bool:
    cfg = load_config()
    try:
        member: discord.Member = interaction.user  # type: ignore[assignment]
        admin_roles = set(getattr(cfg, 'admin_role_ids', [cfg.admin_role_id]))
        has_role = any(r.id in admin_roles for r in getattr(member, 'roles', []))
    except Exception:
        has_role = False
    return has_role


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db
        self.cfg = load_config()
        self.snapshot_service = SnapshotService(db)

    @staticmethod
    def _normalize_csv_url(url: str) -> str:
        if not isinstance(url, str):
            return url
        u = url.strip()
        if not u.lower().startswith("http"):
            return u
        try:
            parsed = urlparse(u)
            host = parsed.netloc.lower()
            path = parsed.path
            if "docs.google.com" in host and "/spreadsheets/" in path:
                # Patterns:
                # 1) https://docs.google.com/spreadsheets/d/<id>/edit#gid=0
                # 2) https://docs.google.com/spreadsheets/d/<id>/view?gid=0
                # 3) https://docs.google.com/spreadsheets/d/<id>/export?format=csv&gid=0
                # 4) Published: .../pub?output=csv
                if "/d/" in path:
                    parts = path.split("/d/")[1]
                    sheet_id = parts.split("/")[0]
                    qs = parse_qs(parsed.query or "")
                    gid = (qs.get("gid") or [None])[0]
                    export = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
                    if gid:
                        export += f"&gid={gid}"
                    return export
                # If it's already a pub with output=csv, keep as-is
                if path.endswith("/pub") and "output=csv" in (parsed.query or ""):
                    return u
        except Exception:
            return u
        return u

    async def _ensure_admin(self, interaction: discord.Interaction) -> bool:
        if has_admin_access(interaction):
            return True
        embed = error_embed("Not Authorized", "You do not have permission to use this command.")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            pass
        return False

    @app_commands.guild_only()
    @app_commands.command(name="panel", description="Post the main METASKS panel")
    async def panel(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_admin(interaction):
            return
        # Panel with wallet manager, tasks by month, and dashboard in a single view
        view = PanelView(self.db)
        embed = discord.Embed(
            title="Elite Floor Panel",
            description="Click 'Dashboard' for your stats, wallets, and tasks.",
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.guild_only()
    @app_commands.command(name="admin", description="Open the admin panel (role-restricted)")
    async def admin(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_admin(interaction):
            return
        await interaction.response.send_message(embed=info_embed("Admin Panel", "Manage holders and snapshots."), view=AdminView(self), ephemeral=True)

    # Removed slash registration for snapshot_start (panel provides this functionality)
    async def snapshot_start(self, interaction: discord.Interaction, wallets_text: str) -> None:
        if not await self._ensure_admin(interaction):
            return
        wallets = [w.strip() for w in wallets_text.split() if w.strip()]

        job_id = str(uuid.uuid4())
        await interaction.response.defer(ephemeral=True, thinking=True)
        sent = await interaction.original_response()
        async def updater(done: int, total: int):
            # Estimate ETA
            job = await self.db.jobs.find_one({"_id": job_id})
            progress = job.get("progress", 0) if job else done
            started_at = job.get("started_at") if job else None
            eta_txt = ""
            if started_at and progress > 0:
                elapsed = max(0.0, (discord.utils.utcnow().timestamp() - started_at))
                avg = elapsed / progress
                remaining = max(0, total - progress)
                eta = avg * remaining
                eta_txt = f"\nETA: ~{eta:.1f}s ({eta/60:.1f}m)"
            await sent.edit(embed=progress_embed("Snapshot Running", done, total, f"Processing wallets...{eta_txt}"))

        async def runner():
            # Periodic updater every 10s regardless of progress callbacks
            async def poller():
                while True:
                    job = await self.db.jobs.find_one({"_id": job_id})
                    if not job or job.get("status") in {"completed", "failed", "cancelled"}:
                        break
                    await updater(job.get("progress", 0), job.get("total", 0))
                    await asyncio.sleep(10)

            poll_task = asyncio.create_task(poller())
            await self.snapshot_service.run_snapshot(wallets, job_id, progress_cb=updater)
            poll_task.cancel()
            job = await self.db.jobs.find_one({"_id": job_id})
            if job and job.get("status") == "completed":
                await sent.edit(embed=success_embed("Snapshot Completed", f"Processed {job.get('progress', 0)} wallets."))
            elif job and job.get("status") == "failed":
                await sent.edit(embed=error_embed("Snapshot Failed", job.get("error", "Unknown error")))

        self.bot.loop.create_task(runner())

    

    # Removed slash registration for holders_add (panel wallet manager covers add)
    async def holders_add(self, interaction: discord.Interaction, wallets_text: str) -> None:
        if not await self._ensure_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        wallets = [w.strip() for w in wallets_text.replace("\n", " ").split(" ") if w.strip()]
        for w in wallets:
            await self.db.holders.update_one({"_id": w}, {"$set": {"_id": w}}, upsert=True)
        await interaction.followup.send(embed=success_embed("holders_add", f"Added {len(wallets)} wallet(s) to holders."), ephemeral=True)

    

    # Removed slash registration for holders_import_file (use admin tools off-panel if needed)
    async def holders_import_file(self, interaction: discord.Interaction, path: str = "unique_holders.json") -> None:
        if not await self._ensure_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(embed=error_embed("holders_import_file", f"Failed to read {path}: {exc}"), ephemeral=True)
            return
        count = 0
        # Case 1: list of strings
        if isinstance(data, list) and data and isinstance(data[0], str):
            for w in data:
                wstr = str(w).strip()
                if not wstr:
                    continue
                await self.db.holders.update_one({"_id": wstr}, {"$set": {"_id": wstr}}, upsert=True)
                count += 1
        # Case 2: list of dicts with OwnerAddress
        elif isinstance(data, list) and data and isinstance(data[0], dict):
            for entry in data:
                w = entry.get("OwnerAddress") or entry.get("owner") or entry.get("wallet")
                if not w:
                    continue
                wstr = str(w).strip()
                if not wstr:
                    continue
                await self.db.holders.update_one({"_id": wstr}, {"$set": {"_id": wstr}}, upsert=True)
                count += 1
        else:
            await interaction.followup.send(embed=error_embed("holders_import_file", "Unsupported JSON format."), ephemeral=True)
            return
        await interaction.followup.send(embed=success_embed("holders_import_file", f"Imported {count} holder(s) into Mongo."), ephemeral=True)

    # Removed slash registration for snapshot_status (admins can monitor via progress embeds)
    async def snapshot_status(self, interaction: discord.Interaction, job_id: str) -> None:
        if not await self._ensure_admin(interaction):
            return
        job = await self.db.jobs.find_one({"_id": job_id})
        if not job:
            await interaction.response.send_message("No such job.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=progress_embed(f"Job {job_id}", job.get("progress", 0), job.get("total", 0), f"Status: {job.get('status')}")
        , ephemeral=True)

    # Removed slash registration for snapshot_cancel (use panel control if exposed)
    async def snapshot_cancel(self, interaction: discord.Interaction, job_id: str) -> None:
        if not await self._ensure_admin(interaction):
            return
        job = await self.db.jobs.find_one({"_id": job_id})
        if not job:
            await interaction.response.send_message("No such job.", ephemeral=True)
            return
        await self.db.jobs.update_one({"_id": job_id}, {"$set": {"status": "cancelled"}}, upsert=True)
        await interaction.response.send_message(success_embed("Cancelled", f"Job {job_id} marked as cancelled."), ephemeral=True)

    # Removed slash registration for gen_refresh
    async def gen_refresh(self, interaction: discord.Interaction, url: str) -> None:
        if not await self._ensure_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        # Fetch CSV
        try:
            rows = []
            if url.lower().startswith("http"):
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=60) as resp:
                        resp.raise_for_status()
                        content = await resp.read()
                text = content.decode("utf-8", errors="ignore")
                reader = csv.DictReader(io.StringIO(text))
                rows = list(reader)
            else:
                # Support file:// and local paths
                path = url
                if url.lower().startswith("file://"):
                    path = url[7:]
                with open(path, "r", encoding="utf-8-sig", newline="") as f:
                    reader = csv.DictReader(f)
                    rows = list(reader)
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(embed=error_embed("gen_refresh", f"Failed to fetch CSV: {exc}"), ephemeral=True)
            return

        # Expect columns: ID, Rank Trait (like zmailfinal.py)
        if not rows or "ID" not in rows[0]:
            await interaction.followup.send(embed=error_embed("gen_refresh", "CSV missing 'ID' column."), ephemeral=True)
            return
        rank_present = "Rank Trait" in rows[0]
        # Upsert rows
        count = 0
        for row in rows:
            try:
                token_id = int(str(row.get("ID")).strip())
            except Exception:
                continue
            update = {"_id": token_id}
            if rank_present:
                update["rank_trait"] = row.get("Rank Trait")
            await self.db.db["gen_ranks"].update_one({"_id": token_id}, {"$set": update}, upsert=True)
            count += 1
        await interaction.followup.send(embed=success_embed("gen_refresh", f"Cached {count} rows."), ephemeral=True)

    # Removed slash registration for combine (manage via backend or panel if needed)
    async def combine(self, interaction: discord.Interaction, main_wallet: str, alt_wallet: str) -> None:
        if not await self._ensure_admin(interaction):
            return
        await self.db.combines.update_one(
            {"_id": main_wallet},
            {"$addToSet": {"alts": alt_wallet}},
            upsert=True,
        )
        await interaction.response.send_message(
            success_embed("Combine Saved", f"Alt `{alt_wallet}` added to main `{main_wallet}`."), ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    db: Database = bot.db  # type: ignore[attr-defined]
    await bot.add_cog(AdminCog(bot, db))


# ===================== PANEL UI (Wallets / Tasks / Dashboard) =====================

class AddWalletsModal(discord.ui.Modal):
    def __init__(self, db: Database):
        super().__init__(title="Add Wallet(s)")
        self.db = db
        self.wallet_input = discord.ui.TextInput(
            label="Wallet Address(es)",
            style=discord.TextStyle.paragraph,
            placeholder="Enter one or more wallet addresses (one per line).",
            required=True,
            max_length=1000,
        )
        self.add_item(self.wallet_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        user_id = str(interaction.user.id)
        wallets_input = [w.strip() for w in self.wallet_input.value.splitlines() if w.strip()]
        valid: list[str] = []
        invalid: list[str] = []
        seen: set[str] = set()
        for w in wallets_input:
            if w in seen:
                continue
            seen.add(w)
            if is_valid_solana_address(w):
                valid.append(w)
            else:
                invalid.append(w)
        if valid:
            await self.db.users.update_one(
                {"_id": user_id},
                {"$addToSet": {"wallets": {"$each": valid}}, "$setOnInsert": {"completed_tasks": []}},
                upsert=True,
            )
        msg = f"Added {len(valid)} wallet(s)."
        if invalid:
            msg += f" Rejected {len(invalid)} invalid address(es)."
        await interaction.followup.send(msg, ephemeral=True)


class ManageWalletsView(discord.ui.View):
    def __init__(self, db: Database):
        super().__init__(timeout=180)
        self.db = db

    @discord.ui.button(label="Show Wallets", style=discord.ButtonStyle.primary, custom_id="show_wallets")
    async def show_wallets_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        user_id = str(interaction.user.id)
        doc = await self.db.users.find_one({"_id": user_id})
        wallets = (doc or {}).get("wallets", [])
        verified_set = set()
        if wallets:
            cursor = self.db.verified_wallets.find({"_id": {"$in": wallets}})
            async for v in cursor:
                verified_set.add(v.get("_id"))
        def fmt(w: str) -> str:
            return f"- {w} (:white_check_mark:)" if w in verified_set else f"- {w}"
        desc = "\n".join(fmt(w) for w in wallets) if wallets else "*No wallets added yet.*"
        embed = discord.Embed(title="Your Wallets", description=desc, color=discord.Color.green())
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Add Wallets", style=discord.ButtonStyle.success, custom_id="add_wallets")
    async def add_wallets_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(AddWalletsModal(self.db))


class TaskModal(discord.ui.Modal):
    def __init__(self, db: Database, month: str, year: int, num1: int, num2: int):
        super().__init__(title=f"{month} {year} Task")
        self.db = db
        self.month = month
        self.year = year
        self.num1 = num1
        self.num2 = num2
        self.answer_input = discord.ui.TextInput(label=f"What is {num1} + {num2}?")
        self.add_item(self.answer_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("Your task request has been received. Please wait for processing...", ephemeral=True)
        user_id = str(interaction.user.id)
        try:
            user_answer = int(self.answer_input.value.strip())
        except Exception:
            await interaction.followup.send("That wasn’t a valid integer, so no changes were made.", ephemeral=True)
            return
        correct = self.num1 + self.num2
        if user_answer == correct:
            # Load user's wallets for confirmation output
            doc = await self.db.users.find_one({"_id": user_id})
            wallets = (doc or {}).get("wallets", [])
            # Add multiple key formats for easier querying: 'Month/YYYY', 'MM/YY', 'YYYY-MM'
            month_to_num = {
                "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
                "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
            }
            mnum = month_to_num.get(self.month, 0)
            mm = f"{mnum:02d}" if mnum else self.month
            yy = str(self.year)[-2:]
            key_text = f"{self.month}/{self.year}"
            key_mm_yy = f"{mm}/{yy}"
            key_iso = f"{self.year}-{mm}"
            await self.db.users.update_one(
                {"_id": user_id},
                {"$addToSet": {"completed_tasks": {"$each": [key_text, key_mm_yy, key_iso]}}, "$setOnInsert": {"wallets": []}},
                upsert=True,
            )
            wallets_list = "\n".join(f"- {w}" for w in wallets) if wallets else "(no wallets on file)"
            embed = discord.Embed(
                title="Task Confirmed",
                description=(
                    f"Completed task for **{key_text}**\n\n"
                    f"Wallets on file:\n{wallets_list}"
                ),
                color=discord.Color.green(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send("Wrong answer. No changes were made.", ephemeral=True)


class MonthButton(discord.ui.Button):
    def __init__(self, db: Database, label: str, month: str, disabled: bool):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, disabled=disabled)
        self.db = db
        self.month = month

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        from datetime import datetime
        year = datetime.now().year
        # Pre-check: user must have wallets saved
        user_id = str(interaction.user.id)
        doc = await self.db.users.find_one({"_id": user_id})
        wallets: list[str] = (doc or {}).get("wallets", [])
        if not wallets:
            await interaction.response.send_message(
                "You don't have any wallets saved. Please add wallets before completing a task.",
                view=ManageWalletsView(self.db),
                ephemeral=True,
            )
            return

        # Ask for confirmation showing wallets and month/year
        wallets_list = "\n".join(f"- {w}" for w in wallets)
        embed = discord.Embed(
            title="Confirm Task Submission",
            description=(
                f"You are about to complete the task for **{self.month} {year}**\n\n"
                f"Wallets:\n{wallets_list}"
            ),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(
            embed=embed,
            view=ConfirmTaskView(self.db, self.month, year),
            ephemeral=True,
        )


class TasksView(discord.ui.View):
    def __init__(self, db: Database, user_completed: list[str]):
        super().__init__(timeout=120)
        from datetime import datetime
        self.db = db
        now = datetime.now()
        current_month_index = now.month
        year = now.year
        months = [
            "January","February","March","April","May","June",
            "July","August","September","October","November","December"
        ]
        for idx, month in enumerate(months, start=1):
            label = month
            mm = f"{idx:02d}"
            yy = str(year)[-2:]
            key_text = f"{month}/{year}"
            key_mm_yy = f"{mm}/{yy}"
            key_iso = f"{year}-{mm}"
            if (key_text in user_completed) or (key_mm_yy in user_completed) or (key_iso in user_completed):
                label += " (Completed)"
            if idx < current_month_index:
                label += " (Expired)"
                disabled = True
            elif idx == current_month_index:
                label += " (Active)"
                disabled = False
            else:
                label += " (Upcoming)"
                disabled = True
            self.add_item(MonthButton(self.db, label=label, month=month, disabled=disabled))


class DashboardView(discord.ui.View):
    def __init__(self, db: Database):
        super().__init__(timeout=180)
        self.db = db

    @discord.ui.button(label="Manage Wallets", style=discord.ButtonStyle.secondary, custom_id="manage_wallets_from_dashboard")
    async def manage_wallets_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send("Manage your wallets below:", view=ManageWalletsView(self.db), ephemeral=True)

    @discord.ui.button(label="Tasks", style=discord.ButtonStyle.green, custom_id="tasks_from_dashboard")
    async def tasks_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        user_id = str(interaction.user.id)
        doc = await self.db.users.find_one({"_id": user_id})
        user_completed = (doc or {}).get("completed_tasks", [])
        await interaction.followup.send("**Here are your tasks:**", view=TasksView(self.db, user_completed), ephemeral=True)

    @discord.ui.button(label="Historical Rewards", style=discord.ButtonStyle.primary, custom_id="hist_rewards")
    async def historical_rewards_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        user_id = str(interaction.user.id)
        doc = await self.db.users.find_one({"_id": user_id})
        wallets: list[str] = (doc or {}).get("wallets", [])
        if not wallets:
            await interaction.followup.send("No wallets on file. Please add wallets first.", view=ManageWalletsView(self.db), ephemeral=True)
            return
        # Determine year range from hist_rewards collection
        years: set[int] = set()
        cursor = self.db.hist_rewards.find({}, {"_id": 1})
        async for row in cursor:
            ep: str = str(row.get("_id", ""))
            # Expect MM/YY
            try:
                yy = int(ep.split("/")[1])
                years.add(2000 + yy if yy < 100 else yy)
            except Exception:
                continue
        if not years:
            from datetime import datetime
            years = {datetime.utcnow().year}
        pages = sorted(years, reverse=True)
        view = HistoricalRewardsView(self.db, wallets, pages)
        embed = view.render_embed()
        lines = await view._compute_lines()
        embed.description = "\n".join(lines)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="Experimental", style=discord.ButtonStyle.danger, custom_id="experimental")
    async def experimental_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        user_id = str(interaction.user.id)
        doc = await self.db.users.find_one({"_id": user_id})
        wallets: list[str] = (doc or {}).get("wallets", [])
        if not wallets:
            await interaction.followup.send("No wallets on file. Please add wallets first.", view=ManageWalletsView(self.db), ephemeral=True)
            return
        verified_set = set()
        # Mark verified by wallet presence in verified_wallets; ownership shown by user_id
        cursor_v = self.db.verified_wallets.find({"_id": {"$in": wallets}})
        async for v in cursor_v:
            verified_set.add(v.get("_id"))
        # Fetch EVM wallet if set
        evm_doc = await self.db.evms.find_one({"_id": user_id})
        evm_addr = evm_doc.get("evm") if evm_doc else None
        lines = []
        for idx, w in enumerate(wallets, start=1):
            mark = " (:white_check_mark:)" if w in verified_set else ""
            lines.append(f"{idx}. {w}{mark}")
        desc = "\n".join(lines)
        if evm_addr:
            desc += f"\n\nEVM: `{evm_addr}`"
        embed = discord.Embed(title="Experimental Verification", description=desc or "No wallets.", color=discord.Color.red())
        await interaction.followup.send(embed=embed, view=ExperimentalVerifyView(self.db, wallets), ephemeral=True)


class ExperimentalVerifyView(discord.ui.View):
    def __init__(self, db: Database, wallets: list[str]):
        super().__init__(timeout=180)
        self.db = db
        self.wallets = wallets

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.primary)
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        modal = ChooseWalletModal(self.db, self.wallets)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Set EVM Wallet", style=discord.ButtonStyle.secondary)
    async def set_evm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        modal = EVMWalletModal(self.db)
        await interaction.response.send_modal(modal)


class ChooseWalletModal(discord.ui.Modal):
    def __init__(self, db: Database, wallets: list[str]):
        super().__init__(title="Choose Wallet to Verify")
        self.db = db
        self.wallets = wallets
        self.num_input = discord.ui.TextInput(
            label="Enter wallet number",
            placeholder="e.g., 1",
            required=True,
            max_length=3,
        )
        self.add_item(self.num_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            idx = int(self.num_input.value.strip()) - 1
        except Exception:
            await interaction.followup.send("Invalid number.", ephemeral=True)
            return
        if idx < 0 or idx >= len(self.wallets):
            await interaction.followup.send("Number out of range.", ephemeral=True)
            return
        wallet = self.wallets[idx]
        # Create a pending verification with small amount (e.g., 5000 lamports = 0.000005 SOL)
        import time
        amount_lamports = 5_000
        await self.db.verifications.update_one(
            {"_id": f"{interaction.user.id}:{wallet}"},
            {"$set": {
                "_id": f"{interaction.user.id}:{wallet}",
                "user_id": str(interaction.user.id),
                "wallet": wallet,
                "amount_lamports": amount_lamports,
                "created_at": time.time(),
                "status": "pending",
            }},
            upsert=True,
        )
        sol = amount_lamports / 1_000_000_000
        msg = (
            f"Send a self-transfer of {sol:.9f} SOL from `{wallet}` to `{wallet}`.\n"
            "After sending, click 'Done' to verify."
        )
        await interaction.followup.send(msg, view=VerificationDoneView(self.db, wallet, amount_lamports), ephemeral=True)


def _is_valid_evm(addr: str) -> bool:
    try:
        a = addr.strip()
        if len(a) != 42 or not a.startswith("0x"):
            return False
        int(a[2:], 16)
        return True
    except Exception:
        return False


class EVMWalletModal(discord.ui.Modal):
    def __init__(self, db: Database):
        super().__init__(title="Set EVM Wallet")
        self.db = db
        self.addr_input = discord.ui.TextInput(
            label="EVM Address (0x...)",
            placeholder="0x...",
            required=True,
            max_length=42,
        )
        self.add_item(self.addr_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        addr = self.addr_input.value.strip()
        if not _is_valid_evm(addr):
            await interaction.followup.send("Invalid EVM address.", ephemeral=True)
            return
        import time
        await self.db.evms.update_one(
            {"_id": str(interaction.user.id)},
            {"$set": {"_id": str(interaction.user.id), "evm": addr, "updated_at": time.time()}},
            upsert=True,
        )
        await interaction.followup.send("EVM wallet saved.", ephemeral=True)


async def verify_self_transfer(wallet: str, amount_lamports: int) -> bool:
    cfg = load_config()
    import aiohttp as _aio
    payload = {
        "jsonrpc": "2.0",
        "id": "verify",
        "method": "getSignaturesForAddress",
        "params": [wallet, {"limit": 20}],
    }
    try:
        async with _aio.ClientSession() as session:
            async with session.post(cfg.solana_rpc_url, json=payload, timeout=20) as resp:
                if resp.status != 200:
                    logging.warning("verify_self_transfer: signatures status=%s", resp.status)
                    return False
                data = await resp.json()
                logging.info("verify_self_transfer: signatures result count=%s", len(data.get("result") or []))
                sigs = [e.get("signature") for e in (data.get("result") or []) if e.get("signature")]
                if not sigs:
                    logging.info("verify_self_transfer: no signatures for %s", wallet)
                    return False
        # Fallback to checking individual transactions via getTransaction (jsonParsed), avoids HTTP 400 for batched calls
        check_sigs = sigs[:10]
        async with _aio.ClientSession() as session:
            for sig in check_sigs:
                payload_tx = {
                    "jsonrpc": "2.0",
                    "id": "tx",
                    "method": "getTransaction",
                    "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
                }
                async with session.post(cfg.solana_rpc_url, json=payload_tx, timeout=30) as resp_tx:
                    if resp_tx.status != 200:
                        logging.warning("verify_self_transfer: getTransaction status=%s sig=%s", resp_tx.status, sig)
                        continue
                    tx = await resp_tx.json()
                    entry = tx.get("result")
                    if not entry:
                        continue
                    # Gather parsed instructions (top-level + inner)
                    msg = (entry.get("transaction") or {}).get("message") or {}
                    insts = list(msg.get("instructions") or [])
                    for inner in (entry.get("meta") or {}).get("innerInstructions") or []:
                        insts.extend(inner.get("instructions") or [])
                    for inst in insts:
                        program = inst.get("program")
                        parsed = inst.get("parsed") or {}
                        if program == "system" and parsed.get("type") == "transfer":
                            info = parsed.get("info") or {}
                            src = info.get("source")
                            dst = info.get("destination")
                            lam = int(info.get("lamports") or 0)
                            logging.debug("verify_self_transfer: sig=%s src=%s dst=%s lam=%s", sig, src, dst, lam)
                            if src == wallet and dst == wallet and lam == amount_lamports:
                                return True
    except Exception as exc:
        logging.exception("verify_self_transfer error: %s", exc)
        return False
    return False


class VerificationDoneView(discord.ui.View):
    def __init__(self, db: Database, wallet: str, amount_lamports: int):
        super().__init__(timeout=300)
        self.db = db
        self.wallet = wallet
        self.amount_lamports = amount_lamports

    @discord.ui.button(label="Done", style=discord.ButtonStyle.success)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok = await verify_self_transfer(self.wallet, self.amount_lamports)
        if not ok:
            await interaction.followup.send("Verification not found yet. Please try again in a moment.", ephemeral=True)
            return
        # Ensure wallet not already verified by any user
        exists = await self.db.verified_wallets.find_one({"_id": self.wallet})
        if exists:
            await interaction.followup.send("This wallet is already verified.", ephemeral=True)
            return
        import time
        await self.db.verified_wallets.update_one(
            {"_id": self.wallet},
            {"$set": {"_id": self.wallet, "user_id": str(interaction.user.id), "verified_at": time.time()}},
            upsert=True,
        )
        await self.db.verifications.update_one({"_id": f"{interaction.user.id}:{self.wallet}"}, {"$set": {"status": "verified"}}, upsert=True)
        await interaction.followup.send("Wallet verified!", ephemeral=True)


class PanelView(discord.ui.View):
    def __init__(self, db: Database):
        super().__init__(timeout=None)
        self.db = db

    @discord.ui.button(label="Dashboard", style=discord.ButtonStyle.primary, custom_id="dashboard_main")
    async def dashboard_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        user_id = str(interaction.user.id)
        doc = await self.db.users.find_one({"_id": user_id})
        user_wallets = (doc or {}).get("wallets", [])
        # Sum allocation from zstats
        total_allocation = 0.0
        if user_wallets:
            cursor = self.db.zstats.find({"_id": {"$in": user_wallets}})
            async for w in cursor:
                total_allocation += float(w.get("total_allocation", 0.0))
        allocation_dollars = f"${total_allocation:,.2f}"
        allocation_percent = (total_allocation / 6_000_000) * 100 if total_allocation > 0 else 0
        embed = discord.Embed(title="Your Dashboard", description="Here’s an overview of your stats:", color=discord.Color.green())
        embed.add_field(name="Total Allocation", value=f"{allocation_dollars} / {allocation_percent:.2f}%", inline=False)
        if user_wallets:
            verified_set = set()
            cursor_v = self.db.verified_wallets.find({"_id": {"$in": user_wallets}})
            async for v in cursor_v:
                verified_set.add(v.get("_id"))
            def fmt(w: str) -> str:
                return f"- {w} (:white_check_mark:)" if w in verified_set else f"- {w}"
            wallets_list = "\n".join(fmt(w) for w in user_wallets)
            embed.add_field(name="Your Wallets", value=wallets_list, inline=False)
        else:
            embed.add_field(name="Your Wallets", value="No wallets added yet.", inline=False)
        await interaction.followup.send(embed=embed, view=DashboardView(self.db), ephemeral=True)


# ===================== ADMIN PANEL UI =====================

class AdminView(discord.ui.View):
    def __init__(self, cog: 'AdminCog'):
        super().__init__(timeout=300)
        self.cog = cog

    @discord.ui.button(label="Fetch Holders", style=discord.ButtonStyle.secondary)
    async def fetch_holders(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not has_admin_access(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        # Ensure HTTP session exists
        await self.cog.snapshot_service.start()
        wallets = await self.cog.snapshot_service.fetch_collection_holders()
        for w in wallets:
            await self.cog.db.holders.update_one({"_id": w}, {"$set": {"_id": w}}, upsert=True)
        await interaction.followup.send(embed=success_embed("Fetch Holders", f"Saved {len(wallets)} wallet(s)."), ephemeral=True)

    @discord.ui.button(label="Start Snapshot", style=discord.ButtonStyle.danger)
    async def start_snapshot(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not has_admin_access(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        modal = ConfirmSnapshotModal(self.cog)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Pull Data (CSV)", style=discord.ButtonStyle.primary)
    async def pull_data(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not has_admin_access(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        modal = PullDataModal(self.cog)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Add Historical Rewards", style=discord.ButtonStyle.secondary)
    async def add_hist_rewards(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not has_admin_access(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        modal = AddHistoricalRewardsModal(self.cog)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Export Leaderboards (CSV)", style=discord.ButtonStyle.secondary)
    async def export_leaderboards(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not has_admin_access(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        # Build wallet->user mapping
        wallet_to_users: dict[str, list[str]] = {}
        async for u in self.cog.db.users.find({}, {"_id": 1, "wallets": 1}):
            uid = str(u.get("_id", ""))
            for w in (u.get("wallets") or []):
                wallet_to_users.setdefault(str(w), []).append(uid)

        # Allocation leaderboard
        allo_stats = await self.cog.db.zstats.find().to_list(length=100000)
        allo_sorted = sorted(allo_stats, key=lambda x: -float(x.get("total_allocation", 0.0)))
        rows_allo: list[list[str]] = [["discord_user_id","wallet","rank","amount_usd"]]
        for idx, d in enumerate(allo_sorted, start=1):
            w = str(d.get("_id"))
            amt = f"{float(d.get('total_allocation', 0.0)):.2f}"
            uids = ",".join(wallet_to_users.get(w, []))
            rows_allo.append([uids, w, str(idx), amt])

        # NFT leaderboard
        nft_sorted = sorted(allo_stats, key=lambda x: -int(x.get("total_nfts", 0)))
        rows_nft: list[list[str]] = [["discord_user_id","wallet","rank","nfts"]]
        for idx, d in enumerate(nft_sorted, start=1):
            w = str(d.get("_id"))
            cnt = str(int(d.get("total_nfts", 0)))
            uids = ",".join(wallet_to_users.get(w, []))
            rows_nft.append([uids, w, str(idx), cnt])

        import csv as _csv, io as _io
        buf_allo = _io.StringIO(); _csv.writer(buf_allo).writerows(rows_allo)
        buf_nft = _io.StringIO(); _csv.writer(buf_nft).writerows(rows_nft)
        file_allo = discord.File(io.BytesIO(buf_allo.getvalue().encode("utf-8")), filename="leaderboard_allo.csv")
        file_nft = discord.File(io.BytesIO(buf_nft.getvalue().encode("utf-8")), filename="leaderboard_nft.csv")
        await interaction.followup.send(content="Leaderboards exported.", files=[file_allo, file_nft], ephemeral=True)


class ConfirmSnapshotModal(discord.ui.Modal):
    def __init__(self, cog: 'AdminCog'):
        super().__init__(title="Confirm Snapshot")
        self.cog = cog
        self.confirm = discord.ui.TextInput(label="Type CONFIRM to start snapshot", required=True)
        self.add_item(self.confirm)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.confirm.value.strip().upper() != "CONFIRM":
            await interaction.response.send_message("Cancelled.", ephemeral=True)
            return
        # Post a public progress message in the channel
        progress_embed = progress_embed_func("Snapshot Running", 0, 0, "Starting...")
        msg = await interaction.channel.send(embed=progress_embed)  # type: ignore[arg-type]
        await interaction.response.send_message("Snapshot started.", ephemeral=True)

        job_id = str(uuid.uuid4())

        async def updater(done: int, total: int):
            job = await self.cog.db.jobs.find_one({"_id": job_id})
            progress = job.get("progress", 0) if job else done
            total_v = job.get("total", total)
            started_at = job.get("started_at") if job else None
            eta_txt = ""
            if started_at and progress > 0 and total_v:
                import time as _t
                elapsed = max(0.0, (_t.time() - started_at))
                avg = elapsed / progress
                remaining = max(0, total_v - progress)
                eta = avg * remaining
                eta_txt = f"\nETA: ~{eta:.1f}s ({eta/60:.1f}m)"
            await msg.edit(embed=progress_embed_func("Snapshot Running", progress, total_v or total, f"Processing wallets...{eta_txt}"))

        async def runner():
            async def poller():
                while True:
                    job = await self.cog.db.jobs.find_one({"_id": job_id})
                    if not job or job.get("status") in {"completed", "failed", "cancelled"}:
                        break
                    await updater(job.get("progress", 0), job.get("total", 0))
                    await asyncio.sleep(10)
            poll_task = asyncio.create_task(poller())
            await self.cog.snapshot_service.run_snapshot_from_db(job_id, progress_cb=updater)
            poll_task.cancel()
            job = await self.cog.db.jobs.find_one({"_id": job_id})
            if job and job.get("status") == "completed":
                await msg.edit(embed=success_embed("Snapshot Completed", f"Processed {job.get('progress', 0)} wallets."))
            elif job and job.get("status") == "failed":
                await msg.edit(embed=error_embed("Snapshot Failed", job.get("error", "Unknown error")))

        asyncio.create_task(runner())


def progress_embed_func(title: str, progress: int, total: int, desc: str):
    return progress_embed(title, progress, total, desc)


class ConfirmTaskView(discord.ui.View):
    def __init__(self, db: Database, month: str, year: int):
        super().__init__(timeout=120)
        self.db = db
        self.month = month
        self.year = year

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        import random
        num1 = random.randint(1, 10)
        num2 = random.randint(1, 10)
        await interaction.response.send_modal(TaskModal(self.db, self.month, self.year, num1, num2))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message("Cancelled.", ephemeral=True)


class PullDataModal(discord.ui.Modal):
    def __init__(self, cog: 'AdminCog'):
        super().__init__(title="Pull Data for CSV")
        self.cog = cog
        self.timeframe = discord.ui.TextInput(
            label="Time frame (MM/YY)",
            placeholder="e.g., 09/25",
            required=True,
            max_length=5,
        )
        self.add_item(self.timeframe)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        tf = self.timeframe.value.strip()
        await interaction.response.defer(ephemeral=True, thinking=True)

        # Build CSV (task doers for that month): one row per user wallet
        # Columns: discord_user_id, username, wallet, wallet_allocation_usd, user_total_allocation_usd, task_completed
        headers = [
            "discord_user_id",
            "username",
            "wallet",
            "wallet_allocation_usd",
            "user_total_allocation_usd",
            "task_completed",
        ]
        rows: list[list[str]] = []

        async def resolve_username(guild: discord.Guild | None, uid: int) -> str:
            if not guild:
                return ""
            # Try cache first
            m = guild.get_member(uid)
            if not m:
                try:
                    m = await guild.fetch_member(uid)
                except Exception:
                    m = None
            if not m:
                return ""
            # Prefer global/display name; discriminator may be 0 in new usernames
            base = m.global_name or m.display_name or m.name
            if hasattr(m, "discriminator") and m.discriminator and m.discriminator != "0":
                return f"{base}#{m.discriminator}"
            return base

        cursor = self.cog.db.users.find({"completed_tasks": tf})
        async for user_doc in cursor:
            uid = user_doc.get("_id", "")
            wallets: list[str] = user_doc.get("wallets", [])
            completed: list[str] = user_doc.get("completed_tasks", [])
            completed_flag = tf in completed

            # resolve display name if possible (avoid duplicating ID)
            username = await resolve_username(interaction.guild, int(uid)) if uid and interaction.guild else ""

            # Pull per-wallet allocations and compute user total (include wallets with zstats even if not saved -> handled only for saved list here)
            wallet_allo_map: dict[str, float] = {}
            user_total = 0.0
            if wallets:
                zc = self.cog.db.zstats.find({"_id": {"$in": wallets}})
                async for z in zc:
                    w = str(z.get("_id"))
                    allo = float(z.get("total_allocation", 0.0))
                    wallet_allo_map[w] = allo
                # ensure all wallets present with 0 if no zstats yet
                for w in wallets:
                    if w not in wallet_allo_map:
                        wallet_allo_map[w] = 0.0
                user_total = sum(wallet_allo_map.values())

            if not wallets:
                # still emit a row with empty wallet to represent the user
                rows.append([
                    str(uid),
                    username,
                    "",
                    f"{0.0:.2f}",
                    f"{0.0:.2f}",
                    "yes" if completed_flag else "no",
                ])
            else:
                for w in wallets:
                    rows.append([
                        str(uid),
                        username,
                        w,
                        f"{wallet_allo_map.get(w, 0.0):.2f}",
                        f"{user_total:.2f}",
                        "yes" if completed_flag else "no",
                    ])

        # Render CSV to bytes
        import csv as _csv
        import io as _io
        buf = _io.StringIO()
        writer = _csv.writer(buf)
        writer.writerow(headers)
        writer.writerows(rows)
        data = buf.getvalue().encode("utf-8")

        file = discord.File(io.BytesIO(data), filename=f"tasks_{tf.replace('/', '-')}.csv")
        # Show last snapshot time (from jobs collection)
        last_job = await self.cog.db.jobs.find_one({"type": "snapshot", "status": "completed"}, sort=[("finished_at", -1)])
        last_txt = "unknown"
        if last_job and last_job.get("finished_at"):
            import datetime
            last_txt = datetime.datetime.utcfromtimestamp(float(last_job["finished_at"])) .strftime("%Y-%m-%d %H:%M:%S UTC")
        await interaction.followup.send(content=f"Last updated snapshot at {last_txt}", file=file, ephemeral=True)

        # Also export zstats allocations for all wallets (separate CSV)
        rows2: list[list[str]] = [["wallet","total_allocation_usd"]]
        async for z in self.cog.db.zstats.find({}, {"_id": 1, "total_allocation": 1}):
            rows2.append([str(z.get("_id")), f"{float(z.get('total_allocation', 0.0)):.2f}"])
        buf2 = _io.StringIO(); writer2 = _csv.writer(buf2); writer2.writerows(rows2)
        data2 = buf2.getvalue().encode("utf-8")
        file2 = discord.File(io.BytesIO(data2), filename="zstats_allocations.csv")
        await interaction.followup.send(file=file2, ephemeral=True)


class AddHistoricalRewardsModal(discord.ui.Modal):
    def __init__(self, cog: 'AdminCog'):
        super().__init__(title="Add Historical Rewards")
        self.cog = cog
        self.epoch = discord.ui.TextInput(
            label="Epoch (MM/YY)",
            placeholder="e.g., 09/25",
            required=True,
            max_length=5,
        )
        self.csv_url = discord.ui.TextInput(
            label="CSV URL or file://path",
            placeholder="Must include columns: wallet, usd",
            required=True,
            style=discord.TextStyle.paragraph,
            max_length=500,
        )
        self.add_item(self.epoch)
        self.add_item(self.csv_url)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not has_admin_access(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        ep = self.epoch.value.strip()
        url = AdminCog._normalize_csv_url(self.csv_url.value.strip())
        await interaction.response.defer(ephemeral=True, thinking=True)
        # Fetch CSV
        rows: list[dict[str, str]] = []
        try:
            if url.lower().startswith("http"):
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=60) as resp:
                        resp.raise_for_status()
                        content = await resp.read()
                text = content.decode("utf-8", errors="ignore")
                reader = csv.DictReader(io.StringIO(text))
                rows = list(reader)
            else:
                path = url
                if url.lower().startswith("file://"):
                    path = url[7:]
                with open(path, "r", encoding="utf-8-sig", newline="") as f:
                    reader = csv.DictReader(f)
                    rows = list(reader)
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(embed=error_embed("his_add", f"Failed to fetch CSV: {exc}"), ephemeral=True)
            return
        # Validate
        if not rows or not {"wallet", "usd"}.issubset({k.strip() for k in rows[0].keys()}):
            await interaction.followup.send(embed=error_embed("his_add", "CSV must have 'wallet' and 'usd' columns."), ephemeral=True)
            return
        # Build rewards map
        rewards: dict[str, float] = {}
        for row in rows:
            w = str(row.get("wallet", "")).strip()
            if not w:
                continue
            try:
                amt = float(str(row.get("usd", 0)).replace(",", "").strip())
            except Exception:
                amt = 0.0
            rewards[w] = rewards.get(w, 0.0) + amt
        # Upsert epoch (override)
        await self.cog.db.hist_rewards.update_one({"_id": ep}, {"$set": {"_id": ep, "rewards": rewards}}, upsert=True)
        await interaction.followup.send(embed=success_embed("his_add", f"Saved {len(rewards)} wallet reward(s) for {ep}.") , ephemeral=True)


class HistoricalRewardsView(discord.ui.View):
    def __init__(self, db: Database, wallets: list[str], years: list[int]):
        super().__init__(timeout=180)
        self.db = db
        self.wallets = wallets
        self.years = years
        self.page = 0  # index into years list

    def _month_labels(self) -> list[tuple[str, str]]:
        months = [
            ("01", "January"),("02", "February"),("03", "March"),("04", "April"),
            ("05", "May"),("06", "June"),("07", "July"),("08", "August"),
            ("09", "September"),("10", "October"),("11", "November"),("12", "December"),
        ]
        year = self.years[self.page]
        yy = str(year)[-2:]
        return [(f"{mm}/{yy}", name) for mm, name in months]

    async def _compute_lines(self) -> list[str]:
        lines: list[str] = []
        cumulative_total = 0.0
        for epoch, label in self._month_labels():
            doc = await self.db.hist_rewards.find_one({"_id": epoch})
            rewards_map: dict[str, float] = (doc or {}).get("rewards", {})
            total = 0.0
            for w in self.wallets:
                try:
                    total += float(rewards_map.get(w, 0.0))
                except Exception:
                    continue
            value = f"${total:,.2f}" if total > 0 else "N/A"
            lines.append(f"{label} {self.years[self.page]}: {value}")
            cumulative_total += total
        # Add cumulative year-to-date total at the bottom
        lines.append(f"\nCumulative YTD:  ${cumulative_total:,.2f}")
        return lines

    def render_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"Historical Rewards ({self.years[self.page]})",
            color=discord.Color.purple(),
        )
        embed.description = "Loading..."
        return embed

    @discord.ui.button(label="Previous Year", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.page > 0:
            self.page -= 1
        lines = await self._compute_lines()
        embed = self.render_embed()
        embed.description = "\n".join(lines)
        await interaction.message.edit(embed=embed, view=self)
        await interaction.response.defer()

    @discord.ui.button(label="Next Year", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.page < len(self.years) - 1:
            self.page += 1
        lines = await self._compute_lines()
        embed = self.render_embed()
        embed.description = "\n".join(lines)
        await interaction.message.edit(embed=embed, view=self)
        await interaction.response.defer()

