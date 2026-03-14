import asyncio
import os
from collections import OrderedDict
from urllib.parse import urlencode, urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from cogs.logger import log_slash_command

DEFAULT_BASE_URL = "https://naoleiwiki.pages.dev"
DEFAULT_LIMIT = 5
MAX_LIMIT = 50
REQUEST_TIMEOUT_SECONDS = 15

SECTION_LABELS = {
    "faq": "常见问题",
    "faq/recent": "常见问题-近期常见",
    "faq/discord": "常见问题-Discord",
    "faq/st-usage": "常见问题-酒馆使用",
    "st-basics": "酒馆基础",
    "st-basics/install": "酒馆基础-安装",
    "troubleshooting": "报错对照表",
    "tools": "工具",
    "works": "作品",
    "credits": "致谢",
}

SUBSECTION_LABELS = {
    "recent": "近期常见",
    "discord": "Discord",
    "st-usage": "酒馆使用",
    "install": "安装",
    "general": "通用",
    "claude": "Claude",
    "deepseek": "DeepSeek",
    "gemini-api": "Gemini API",
    "gemini-build": "Gemini Build",
    "gemini-cli": "Gemini CLI",
}


async def safe_defer(interaction: discord.Interaction):
    """
    一个绝对安全的"占坑"函数。
    它会检查交互是否已被响应，如果没有，就立即以"仅自己可见"的方式延迟响应，
    这能完美解决超时和重复响应问题。
    """
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)


def normalize_base_url(base_url: str) -> str:
    base_url = (base_url or "").strip()
    if not base_url:
        return DEFAULT_BASE_URL
    return base_url.rstrip("/")


def resolve_limit(raw_value: str) -> int:
    try:
        limit = int(raw_value)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    return max(1, min(limit, MAX_LIMIT))


def build_search_url(base_url: str, query: str, limit: int, include_works: bool) -> str:
    params = {"q": query, "limit": str(limit)}
    if include_works:
        params["includeWorks"] = "1"
    return f"{normalize_base_url(base_url)}/api/search?{urlencode(params)}"


def get_section_key(url: str) -> str:
    if not url:
        return "其他"
    try:
        parsed = urlparse(url)
    except ValueError:
        return "其他"
    path = (parsed.path or "").strip("/")
    if not path:
        return "首页"
    parts = path.split("/")
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return parts[0]


def get_section_label(section_key: str) -> str:
    if section_key in SECTION_LABELS:
        return SECTION_LABELS[section_key]
    if "/" in section_key:
        parent, child = section_key.split("/", 1)
        parent_label = SECTION_LABELS.get(parent, parent)
        child_label = SUBSECTION_LABELS.get(child, child)
        return f"{parent_label}-{child_label}"
    return SECTION_LABELS.get(section_key, section_key or "其他")


def group_results_by_section(results: list[dict]) -> OrderedDict[str, list[dict]]:
    grouped: OrderedDict[str, list[dict]] = OrderedDict()
    for item in results:
        section_key = get_section_key(item.get("url") or "")
        if section_key not in grouped:
            grouped[section_key] = []
        grouped[section_key].append(item)
    return grouped


def order_section_keys(grouped: OrderedDict[str, list[dict]]) -> list[str]:
    keys = list(grouped.keys())
    ordered: list[str] = []

    if "faq/recent" in grouped:
        ordered.append("faq/recent")

    for key in keys:
        if key == "faq" or key.startswith("faq/"):
            if key not in ordered:
                ordered.append(key)

    for key in keys:
        if key not in ordered:
            ordered.append(key)

    return ordered


def is_recent_faq_item(item: dict) -> bool:
    title = (item.get("title") or "").strip()
    if "近期常见" in title:
        return True
    url = (item.get("url") or "").lower()
    return "/faq/recent" in url


def normalize_snippet(snippet: str) -> str:
    text = (snippet or "").strip()
    if not text:
        return ""
    # 处理返回内容中转义的 \n
    text = text.replace("\\r", "\r").replace("\\n", "\n")
    # 规整空白
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)


def format_search_results(results: list[dict]) -> str:
    lines: list[str] = []
    grouped = group_results_by_section(results)
    index = 1
    for section_key in order_section_keys(grouped):
        items = grouped.get(section_key, [])
        section_label = get_section_label(section_key)
        lines.append(f"【{section_label}】")
        for item in items:
            title = (item.get("title") or "无标题").strip()
            url = (item.get("url") or "").strip()
            snippet = normalize_snippet(item.get("snippet") or "")
            if url:
                headline = f"{index}. [{title}]({url})"
            else:
                headline = f"{index}. {title}"
            if snippet:
                lines.append(f"{headline}\n{snippet}")
            else:
                lines.append(headline)
            index += 1
        lines.append("")
    return "\n".join(line for line in lines if line is not None).strip()


class WikiSearch(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="问题搜索", description="在脑类知识库中搜索相关内容")
    @app_commands.describe(
        关键词="要搜索的关键词",
        包含作品="是否包含 /works 下的内容（默认不包含）",
    )
    async def wiki_search(
        self,
        interaction: discord.Interaction,
        关键词: str,
        包含作品: bool = False,
    ):
        await safe_defer(interaction)

        query = (关键词 or "").strip()
        if not query:
            await interaction.edit_original_response(content="❌ 关键词不能为空。")
            log_slash_command(interaction, False)
            return

        base_url = normalize_base_url(os.getenv("NAOLEI_WIKI_BASE_URL", DEFAULT_BASE_URL))
        limit = resolve_limit(os.getenv("NAOLEI_WIKI_SEARCH_LIMIT", str(DEFAULT_LIMIT)))
        search_url = build_search_url(base_url, query, limit, 包含作品)

        try:
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(search_url) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        await interaction.edit_original_response(
                            content=(
                                "❌ 搜索服务返回错误。\n"
                                f"HTTP {response.status}"
                            )
                        )
                        print(f"[WikiSearch] HTTP {response.status}: {error_text}")
                        log_slash_command(interaction, False)
                        return
                    data = await response.json()
        except asyncio.TimeoutError:
            await interaction.edit_original_response(content="❌ 搜索服务超时，请稍后重试。")
            log_slash_command(interaction, False)
            return
        except aiohttp.ClientError as exc:
            await interaction.edit_original_response(content="❌ 搜索服务连接失败，请稍后重试。")
            print(f"[WikiSearch] Client error: {exc}")
            log_slash_command(interaction, False)
            return
        except Exception as exc:
            await interaction.edit_original_response(content="❌ 搜索服务响应异常，请稍后重试。")
            print(f"[WikiSearch] Unexpected error: {exc}")
            log_slash_command(interaction, False)
            return

        results = data.get("results", []) if isinstance(data, dict) else []

        if results:
            results = sorted(results, key=lambda item: (0 if is_recent_faq_item(item) else 1))

        if not results:
            await interaction.edit_original_response(content="🔎 未找到相关内容。")
            log_slash_command(interaction, True)
            return

        limited_results = results[:limit]
        description = format_search_results(limited_results)
        embed = discord.Embed(
            title="问题搜索结果",
            description=description,
            color=discord.Color.blue(),
        )
        embed.set_footer(text=f"来源: {base_url}")

        await interaction.edit_original_response(embed=embed)
        log_slash_command(interaction, True)


async def setup(bot: commands.Bot):
    await bot.add_cog(WikiSearch(bot))
