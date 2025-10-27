import discord
from discord.ext import commands
from datetime import datetime
import pytz
import json
import os
from dotenv import load_dotenv
from discord import app_commands

# -----------------------------
# CONFIGURATION / VARIABLES
# -----------------------------
# Replace with your actual IDs
INPUT_GUILD_ID = 741119908741644348
OUTPUT_GUILD_ID = 1169054273254985830

# The array of user IDs allowed to run "!refresh" and "!grant"
ALLOWED_USER_IDS = [
    627002551144939520,  # example ID
    714374856468201504  # add more if needed
]

INPUT_CHANNELS = [
    1168393935752790067,  # e.g. "kingmaker"
    1263286111397085346,  # e.g. "daily-outlook"
    1133229826313039922,
    800907394791899136
]
OUTPUT_CHANNELS = [
    1333854322575802479,
    1333854252178608150,
    1333825631053545545,
    1331705449711538316
]
PING_ROLES = [
    1333852767986515988,
    1333852811804541010,
    1333852309528117299,
    1333852271787642970
]

TITLE_ARRAY = [
    "King Maker Alert",
    "Viking Alert",
    "Daily Outlook",
    "Steady FuturesAlert",
    "Steady Options Alert"
]

# Each index in these arrays is a pair: INPUT_ROLE_IDS[i] -> OUTPUT_ROLE_IDS[i]
INPUT_ROLE_IDS = [
    1274568396997922871,
    1274568333299027988,
    1274568120392093777,
    823398193291198504,
    796839145444081725
]

SINGLE_OUTPUT_ROLE_ID = 1334391975511855187

# Load .env from METASKS folder (next to this file) and project root
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"), override=False)
load_dotenv(override=False)
DISCORD_BOT_TOKEN = os.getenv("steadytoken") or os.getenv("STEADY_TOKEN") or ""

# The path to the JSON file we use to store manually granted user IDs
ZSTEADY_JSON_PATH = "zsteady.json"

# -----------------------------
# JSON LOAD / SAVE FUNCTIONS
# -----------------------------
def load_zsteady():
    """Load the list of manually granted user IDs from zsteady.json."""
    if not os.path.exists(ZSTEADY_JSON_PATH):
        return []
    try:
        with open(ZSTEADY_JSON_PATH, "r") as f:
            data = json.load(f)
            # We expect data to be a list of user IDs
            if isinstance(data, list):
                return data
            else:
                print("zsteady.json is not a list; returning empty list.")
                return []
    except Exception as e:
        print(f"Error loading zsteady.json: {e}")
        return []

def save_zsteady(manual_ids):
    """Save the list of manually granted user IDs to zsteady.json."""
    try:
        with open(ZSTEADY_JSON_PATH, "w") as f:
            json.dump(manual_ids, f)
    except Exception as e:
        print(f"Error saving zsteady.json: {e}")

# -----------------------------
# BOT SETUP
# -----------------------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# We'll maintain this in-memory list of manually granted IDs (loaded from zsteady.json)
manual_ids = load_zsteady()

@bot.event
async def on_ready():
    print(f"Bot is online as {bot.user} (ID: {bot.user.id})")

    # Sync slash commands to the output guild so they're available immediately
    try:
        await bot.tree.sync(guild=discord.Object(id=OUTPUT_GUILD_ID))
        print("Synced slash commands to output guild.")
    except Exception as e:
        print(f"Slash command sync failed: {e}")

    # Full role sync on startup
    await full_role_sync()
    print("Full role sync complete on startup.")

async def full_role_sync():
    """
    This performs a full role sync (similar to what on_ready did before).
    It checks every member of the input guild and ensures they
    have or do not have the SINGLE_OUTPUT_ROLE_ID in the output guild accordingly.
    """
    input_guild = bot.get_guild(INPUT_GUILD_ID)
    output_guild = bot.get_guild(OUTPUT_GUILD_ID)

    if not input_guild:
        print(f"Could not find INPUT guild (ID: {INPUT_GUILD_ID}). Role sync skipped.")
        return
    if not output_guild:
        print(f"Could not find OUTPUT guild (ID: {OUTPUT_GUILD_ID}). Role sync skipped.")
        return

    output_role = output_guild.get_role(SINGLE_OUTPUT_ROLE_ID)
    if not output_role:
        print(f"Could not find output role (ID: {SINGLE_OUTPUT_ROLE_ID}).")
        return

    print("Starting full role sync...")

    async for member in input_guild.fetch_members(limit=None):
        out_member = output_guild.get_member(member.id)
        if not out_member:
            continue  # The user might not exist in the output guild

        # Check if they have ANY of the input roles or if they're manually in zsteady.json
        has_any_input_role = any(
            (input_guild.get_role(rid) in member.roles) for rid in INPUT_ROLE_IDS
        ) or (member.id in manual_ids)

        # Check if they currently have the single output role
        has_output_role = (output_role in out_member.roles)

        # If they SHOULD have it but don't, add it
        if has_any_input_role and not has_output_role:
            try:
                await out_member.add_roles(output_role, reason="Full role sync")
            except discord.Forbidden:
                print(f"Missing permissions to add role {output_role.id} to {out_member}")
            except Exception as e:
                print(f"Error adding role {output_role.id} to {out_member}: {e}")

        # If they should NOT have it but do, remove it
        if not has_any_input_role and has_output_role:
            try:
                await out_member.remove_roles(output_role, reason="Full role sync")
            except discord.Forbidden:
                print(f"Missing permissions to remove role {output_role.id} from {out_member}")
            except Exception as e:
                print(f"Error removing role {output_role.id} from {out_member}: {e}")

    print("Full role sync done.")

@bot.command(name="refresh")
async def refresh(ctx):
    """
    Allows an authorized user to manually trigger a full role sync.
    Usage: !refresh
    """
    # Check if the user is allowed
    if ctx.author.id not in ALLOWED_USER_IDS:
        await ctx.send("You do not have permission to use this command.")
        return

    await full_role_sync()
    await ctx.send("Full role sync has been completed.")

@bot.command(name="grant")
async def grant(ctx, member: discord.Member = None):
    """
    Allows an authorized user to manually grant the single output role to someone.
    Also adds that user ID to zsteady.json if not already there.
    
    Usage: !grant @SomeUser
    """
    # Check if the user is allowed
    if ctx.author.id not in ALLOWED_USER_IDS:
        await ctx.send("You do not have permission to use this command.")
        return

    if not member:
        await ctx.send("You must mention a user. Example: `!grant @SomeUser`")
        return

    # Add the user ID to manual_ids if not present
    if member.id not in manual_ids:
        manual_ids.append(member.id)
        save_zsteady(manual_ids)    
        

    # Now ensure they get the role in the output guild
    output_guild = bot.get_guild(OUTPUT_GUILD_ID)
    if not output_guild:
        await ctx.send("Could not find output guild; cannot grant role.")
        return

    output_role = output_guild.get_role(SINGLE_OUTPUT_ROLE_ID)
    if not output_role:
        await ctx.send("Could not find output role; cannot grant role.")
        return

    out_member = output_guild.get_member(member.id)
    if not out_member:
        await ctx.send("That user is not in the output guild, so I cannot grant them the role.")
        return

    if output_role in out_member.roles:
        await ctx.send("They already have the output role.")
    else:
        try:
            await out_member.add_roles(output_role, reason="Manual grant via !grant")
            await ctx.send(f"Role granted to <@{member.id}> in the Market Elites Server.")
        except discord.Forbidden:
            await ctx.send("Missing permissions to add roles.")
        except Exception as e:
            await ctx.send(f"Error adding the role: {e}")

# -----------------------------
# SLASH COMMANDS: /add and /remove
# -----------------------------

@bot.tree.command(name="add", description="Grant access role in output server and record in manual list")
@app_commands.guilds(discord.Object(id=OUTPUT_GUILD_ID))
@app_commands.describe(user="User to grant access to")
async def add_command(interaction: discord.Interaction, user: discord.User):
    # Permission check
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    output_guild = bot.get_guild(OUTPUT_GUILD_ID)
    if not output_guild:
        await interaction.response.send_message("Could not find output guild; cannot grant role.", ephemeral=True)
        return

    # Persist manual grant
    if user.id not in manual_ids:
        manual_ids.append(user.id)
        save_zsteady(manual_ids)

    output_role = output_guild.get_role(SINGLE_OUTPUT_ROLE_ID)
    if not output_role:
        await interaction.response.send_message("Could not find output role; cannot grant role.", ephemeral=True)
        return

    out_member = output_guild.get_member(user.id)
    if not out_member:
        await interaction.response.send_message(
            "User is not in the output guild. Added to manual list; they will get the role upon joining.",
            ephemeral=True
        )
        return

    if output_role in out_member.roles:
        await interaction.response.send_message("They already have the output role.", ephemeral=True)
        return

    try:
        await out_member.add_roles(output_role, reason="Manual grant via /add")
        await interaction.response.send_message(
            f"Role granted to <@{user.id}> in the Market Elites Server.",
            ephemeral=True
        )
    except discord.Forbidden:
        await interaction.response.send_message("Missing permissions to add roles.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Error adding the role: {e}", ephemeral=True)


@bot.tree.command(name="remove", description="Revoke manual access; remove role if user not eligible via input roles")
@app_commands.guilds(discord.Object(id=OUTPUT_GUILD_ID))
@app_commands.describe(user="User to remove access from")
async def remove_command(interaction: discord.Interaction, user: discord.User):
    # Permission check
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    # Remove from manual list if present
    removed_from_manual = False
    if user.id in manual_ids:
        manual_ids.remove(user.id)
        save_zsteady(manual_ids)
        removed_from_manual = True

    output_guild = bot.get_guild(OUTPUT_GUILD_ID)
    if not output_guild:
        await interaction.response.send_message("Could not find output guild; cannot remove role.", ephemeral=True)
        return

    output_role = output_guild.get_role(SINGLE_OUTPUT_ROLE_ID)
    if not output_role:
        await interaction.response.send_message("Could not find output role; cannot remove role.", ephemeral=True)
        return

    out_member = output_guild.get_member(user.id)

    # Determine if the user is still eligible via input roles
    input_guild = bot.get_guild(INPUT_GUILD_ID)
    has_any_input_role = False
    if input_guild:
        input_member = input_guild.get_member(user.id)
        if input_member:
            has_any_input_role = any(
                (input_guild.get_role(rid) in input_member.roles) for rid in INPUT_ROLE_IDS
            )

    # If not eligible via input roles, remove the role in output guild
    if out_member and (not has_any_input_role) and (output_role in out_member.roles):
        try:
            await out_member.remove_roles(output_role, reason="Manual revoke via /remove and no input roles")
        except discord.Forbidden:
            await interaction.response.send_message("Missing permissions to remove roles.", ephemeral=True)
            return
        except Exception as e:
            await interaction.response.send_message(f"Error removing the role: {e}", ephemeral=True)
            return

    # Prepare a clear response
    if removed_from_manual:
        if has_any_input_role:
            await interaction.response.send_message(
                "Removed from manual list. User still has input roles, so access remains.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "Removed from manual list and access role removed (no input roles).",
                ephemeral=True
            )
    else:
        if has_any_input_role:
            await interaction.response.send_message(
                "User was not in manual list. They still have access via input roles.",
                ephemeral=True
            )
        else:
            if out_member and (output_role not in out_member.roles):
                await interaction.response.send_message(
                    "User was not in manual list and does not have the access role.",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "User was not in manual list. Access role removed if present.",
                    ephemeral=True
                )

@bot.event
async def on_message(message: discord.Message):
    """
    Reflect messages from INPUT_CHANNELS to OUTPUT_CHANNELS.
    If the message has >=1 embed, replicate those embeds exactly (ignore text).
    If no embed, create our own custom embed with a title, author, etc.
    """
    # Avoid infinite loops
    if message.author == bot.user:
        return

    # Relay messages from input guild to output guild channels
    if message.guild and message.guild.id == INPUT_GUILD_ID:
        if message.channel.id in INPUT_CHANNELS:
            idx = INPUT_CHANNELS.index(message.channel.id)

            output_channel_id = OUTPUT_CHANNELS[idx]
            ping_role_id = PING_ROLES[idx]
            embed_title = TITLE_ARRAY[idx]

            output_guild = bot.get_guild(OUTPUT_GUILD_ID)
            if not output_guild:
                print(f"Could not find output guild with ID {OUTPUT_GUILD_ID}")
                return

            output_channel = output_guild.get_channel(output_channel_id)
            if not output_channel:
                print(f"Could not find output channel with ID {output_channel_id}")
                return

            ping_role = output_guild.get_role(ping_role_id)
            role_mention = ping_role.mention if ping_role else ""

            # Gather attachments
            files_to_send = []
            for att in message.attachments:
                files_to_send.append(await att.to_file())

            # If the message has embeds, replicate them
            if message.embeds:
                outgoing_embeds = [embed.copy() for embed in message.embeds]
                await output_channel.send(
                    content=role_mention,
                    embeds=outgoing_embeds,
                    files=files_to_send if files_to_send else None
                )
            else:
                # Create a new embed
                embed = discord.Embed(
                    title=embed_title,
                    description=message.content if message.content else " ",
                    color=0x2ecc71
                )
                embed.set_author(
                    name=message.author.display_name,
                    icon_url=message.author.display_avatar.url
                )
                eastern_tz = pytz.timezone("US/Eastern")
                est_now = datetime.now(eastern_tz)
                embed.timestamp = est_now
                footer_text = est_now.strftime("%Y-%m-%d %I:%M %p EST")
                embed.set_footer(text=footer_text)

                # Optionally embed the first attached image
                image_extensions = (".png", ".jpg", ".jpeg", ".gif", ".webp")
                first_image_url = None
                for att in message.attachments:
                    if att.filename.lower().endswith(image_extensions):
                        first_image_url = att.url
                        break
                if first_image_url:
                    embed.set_image(url=first_image_url)

                await output_channel.send(
                    content=role_mention,
                    embed=embed,
                    files=files_to_send if files_to_send else None
                )

    await bot.process_commands(message)

@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    """
    When roles change in the Input Guild, if the user gains ANY input role or is in manual_ids,
    ensure they have the SINGLE_OUTPUT_ROLE_ID in the Output Guild.
    If they lose all input roles and are not in manual_ids, remove it.
    """
    # We only care about changes in the Input Guild
    if before.guild.id != INPUT_GUILD_ID:
        return

    output_guild = bot.get_guild(OUTPUT_GUILD_ID)
    if not output_guild:
        return

    output_member = output_guild.get_member(after.id)
    if not output_member:
        return

    output_role = output_guild.get_role(SINGLE_OUTPUT_ROLE_ID)
    if not output_role:
        return

    before_role_ids = [role.id for role in before.roles]
    after_role_ids = [role.id for role in after.roles]

    had_any_before = any(rid in before_role_ids for rid in INPUT_ROLE_IDS) or (before.id in manual_ids)
    has_any_now = any(rid in after_role_ids for rid in INPUT_ROLE_IDS) or (after.id in manual_ids)

    # No change in overall status
    if had_any_before == has_any_now:
        return

    # If they gained an input role or got added to manual_ids
    if not had_any_before and has_any_now:
        if output_role not in output_member.roles:
            try:
                await output_member.add_roles(output_role, reason="Mirroring role gain or manual grant")
            except discord.Forbidden:
                print(f"Missing permissions to add role {output_role.id} to {output_member}.")
            except Exception as e:
                print(f"Error adding role {output_role.id} to {output_member}: {e}")

    # If they lost their last input role and are not in manual_ids
    if had_any_before and not has_any_now:
        if output_role in output_member.roles:
            try:
                await output_member.remove_roles(output_role, reason="Mirroring role loss; no input roles and not in zsteady")
            except discord.Forbidden:
                print(f"Missing permissions to remove role {output_role.id} from {output_member}.")
            except Exception as e:
                print(f"Error removing role {output_role.id} from {output_member}: {e}")


# -----------------------------
# RUN THE BOT
# -----------------------------
if not DISCORD_BOT_TOKEN:
    raise RuntimeError("steadytoken is required in .env or environment for zsteady")
bot.run(DISCORD_BOT_TOKEN)
