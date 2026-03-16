import asyncio
import re
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from cogs.logger import log_slash_command

BJ_TZ = timezone(timedelta(hours=8))


def parse_timezone_offset(raw: str) -> timezone:
    if raw == "Z":
        return timezone.utc
    sign = 1 if raw.startswith("+") else -1
    rest = raw[1:]
    if ":" in rest:
        hours_str, minutes_str = rest.split(":", 1)
    elif len(rest) > 2:
        hours_str, minutes_str = rest[:2], rest[2:]
    else:
        hours_str, minutes_str = rest, "0"
    hours = int(hours_str)
    minutes = int(minutes_str) if minutes_str else 0
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def parse_reminder_time(raw: str) -> datetime:
    text = (raw or "").strip()
    if not text:
        raise ValueError("时间不能为空")

    relative_match = re.fullmatch(r"(\d+)\s*([smhdSMHD])", text)
    if relative_match:
        amount = int(relative_match.group(1))
        unit = relative_match.group(2).lower()
        if amount <= 0:
            raise ValueError("相对时间必须大于 0")
        delta_map = {
            "s": timedelta(seconds=amount),
            "m": timedelta(minutes=amount),
            "h": timedelta(hours=amount),
            "d": timedelta(days=amount),
        }
        return datetime.now(timezone.utc) + delta_map[unit]

    combo_match = re.fullmatch(
        r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?",
        text,
        re.IGNORECASE,
    )
    if combo_match and any(combo_match.groups()):
        days = int(combo_match.group(1) or 0)
        hours = int(combo_match.group(2) or 0)
        minutes = int(combo_match.group(3) or 0)
        seconds = int(combo_match.group(4) or 0)
        total_seconds = days * 86400 + hours * 3600 + minutes * 60 + seconds
        if total_seconds <= 0:
            raise ValueError("相对时间必须大于 0")
        return datetime.now(timezone.utc) + timedelta(seconds=total_seconds)

    time_only_match = re.fullmatch(
        r"(\d{1,2}):(\d{2})(?:\s*(Z|[+-]\d{1,2}(?::?\d{2})?))?",
        text,
    )
    if time_only_match:
        hour = int(time_only_match.group(1))
        minute = int(time_only_match.group(2))
        tz_part = time_only_match.group(3)
        if hour > 23 or minute > 59:
            raise ValueError("时间范围无效")
        tzinfo = BJ_TZ if tz_part is None else parse_timezone_offset(tz_part)
        now_local = datetime.now(tzinfo)
        target_local = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target_local <= now_local:
            target_local += timedelta(days=1)
        return target_local.astimezone(timezone.utc)

    absolute_match = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})(?:\s*(Z|[+-]\d{1,2}(?::?\d{2})?))?",
        text,
    )
    if absolute_match:
        date_part = absolute_match.group(1)
        time_part = absolute_match.group(2)
        tz_part = absolute_match.group(3)
        dt = datetime.strptime(f"{date_part} {time_part}", "%Y-%m-%d %H:%M")
        tzinfo = BJ_TZ if tz_part is None else parse_timezone_offset(tz_part)
        return dt.replace(tzinfo=tzinfo).astimezone(timezone.utc)

    raise ValueError(
        "支持格式: YYYY-MM-DD HH:MM(+08:00) / 30m / 2h / 1d / 1d2h30m / HH:MM(+08:00)"
    )


class Reminder(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._tasks: set[asyncio.Task] = set()

    def _track_task(self, task: asyncio.Task) -> None:
        self._tasks.add(task)
        task.add_done_callback(lambda t: self._tasks.discard(t))

    def cog_unload(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

    async def _send_reminder(self, user: discord.User, target_dt: datetime, note: str) -> None:
        delay = (target_dt - datetime.now(timezone.utc)).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)

        note_text = (note or "").strip()
        content = "⏰ 提醒时间到了！"
        if note_text:
            content += f"\n📝 提示事项：{note_text}"

        try:
            await user.send(content)
        except discord.Forbidden:
            print(f"[提醒] 无法向用户 {user.id} 发送私信（可能关闭私信）")
        except Exception as exc:
            print(f"[提醒] 发送私信失败: {exc}")

    @app_commands.command(name="提醒", description="设置到点私信提醒")
    @app_commands.describe(
        时间="支持 YYYY-MM-DD HH:MM(+08:00) / 30m/2h/1d / 1d2h30m / HH:MM(+08:00)",
        提示事项="可选：提醒你要做的事项",
    )
    async def remind(
        self,
        interaction: discord.Interaction,
        时间: str,
        提示事项: str = "",
    ) -> None:
        try:
            target_dt = parse_reminder_time(时间)
        except ValueError as exc:
            await interaction.response.send_message(
                f"❌ 时间格式无效：{exc}\n示例：2026-03-16 21:30 或 30m 或 21:30 或 2026-03-16 21:30+08:00",
                ephemeral=True,
            )
            log_slash_command(interaction, False)
            return

        now_utc = datetime.now(timezone.utc)
        if target_dt <= now_utc:
            await interaction.response.send_message("❌ 提醒时间已过，请选择未来时间。", ephemeral=True)
            log_slash_command(interaction, False)
            return

        task = asyncio.create_task(self._send_reminder(interaction.user, target_dt, 提示事项))
        self._track_task(task)

        target_bj = target_dt.astimezone(BJ_TZ)
        note_text = (提示事项 or "").strip()
        note_line = f"\n提示事项：{note_text}" if note_text else ""
        await interaction.response.send_message(
            f"✅ 已设置提醒：{target_bj.strftime('%Y-%m-%d %H:%M')}（北京时间）{note_line}",
            ephemeral=True,
        )
        log_slash_command(interaction, True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Reminder(bot))
