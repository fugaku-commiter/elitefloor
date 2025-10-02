import discord


def progress_embed(title: str, progress: int, total: int, description: str | None = None) -> discord.Embed:
    percent = 0 if total <= 0 else int((progress / max(total, 1)) * 100)
    bar_len = 20
    filled = int(bar_len * percent / 100)
    bar = "█" * filled + "─" * (bar_len - filled)
    embed = discord.Embed(title=title, description=description or "", color=discord.Color.blurple())
    embed.add_field(name="Progress", value=f"{progress}/{total} ({percent}%)\n`{bar}`", inline=False)
    return embed


def info_embed(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=discord.Color.blue())


def success_embed(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=discord.Color.green())


def error_embed(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=discord.Color.red())


