import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import datetime
import sqlite3
import json
import asyncio
import base64
from typing import Dict, List, Optional, Any

# ================= 配置映射 =================
# 新手开帖 论坛频道 ID
try:
    TARGET_FORUM_ID = int(os.getenv("TARGET_CHANNEL_OR_THREAD", 0))
except:
    TARGET_FORUM_ID = 0

# 新手答疑 汇报频道 ID
try:
    REPORT_CHANNEL_ID = int(os.getenv("ALERT_CHANNEL_OR_THREAD", 0))
except:
    REPORT_CHANNEL_ID = 0

# 已解决标签 ID
try:
    RESOLVED_TAG_ID = int(os.getenv("RESOLVED_TAG_ID", 0))
except:
    RESOLVED_TAG_ID = 0

# 优先使用图片描述模型，如果没有则回退到通用模型
AI_MODEL_NAME = os.getenv("IMAGE_DESCRIBE_MODEL") or os.getenv("OPENAI_MODEL")

DB_DIR = "tagger"
DB_PATH = os.path.join(DB_DIR, "unanswered.db")

class UnansweredFilter(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._ensure_db_ready()
        # 启动定时任务 (每日北京时间 12:00 = UTC 04:00)
        self.daily_check_task.start()

    def cog_unload(self):
        self.daily_check_task.cancel()

    # ================= 数据库管理 =================
    def _ensure_db_ready(self):
        if not os.path.exists(DB_DIR):
            os.makedirs(DB_DIR, exist_ok=True)

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        # 帖子状态缓存：记录上次分析时的状态，避免重复分析
        c.execute('''CREATE TABLE IF NOT EXISTS thread_cache (
            thread_id INTEGER PRIMARY KEY,
            last_message_id INTEGER,
            reply_count INTEGER,
            status TEXT,
            reason TEXT,
            last_analyzed_at TIMESTAMP
        )''')
        # 图片描述缓存：避免重复上传图片给AI
        c.execute('''CREATE TABLE IF NOT EXISTS image_cache (
            attachment_id INTEGER PRIMARY KEY,
            description TEXT
        )''')
        conn.commit()
        conn.close()

    def _get_cached_thread(self, thread_id: int):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT last_message_id, reply_count, status, reason FROM thread_cache WHERE thread_id=?", (thread_id,))
        row = c.fetchone()
        conn.close()
        return row

    def _update_thread_cache(self, thread_id: int, last_msg_id: int, reply_count: int, status: str, reason: str):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO thread_cache VALUES (?,?,?,?,?,?)", 
                  (thread_id, last_msg_id, reply_count, status, reason, datetime.datetime.now()))
        conn.commit()
        conn.close()

    def _delete_thread_cache(self, thread_id: int):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM thread_cache WHERE thread_id=?", (thread_id,))
        conn.commit()
        conn.close()

    def _get_cached_image(self, attachment_id: int) -> Optional[str]:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT description FROM image_cache WHERE attachment_id=?", (attachment_id,))
        row = c.fetchone()
        conn.close()
        return row[0] if row else None

    def _cache_image_desc(self, attachment_id: int, description: str):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO image_cache VALUES (?,?)", (attachment_id, description))
        conn.commit()
        conn.close()

    # ================= 核心逻辑：数据抓取与预处理 =================

    async def _fetch_and_prepare_batch(self):
        """拉取帖子，计算真实回复数，构建发送给AI的数据包"""
        if not TARGET_FORUM_ID:
            print("❌ [Unanswered] 未配置 TARGET_CHANNEL_OR_THREAD")
            return None, [], []

        forum_channel = self.bot.get_channel(TARGET_FORUM_ID)
        if not forum_channel:
            # 尝试 fetch
            try:
                forum_channel = await self.bot.fetch_channel(TARGET_FORUM_ID)
            except:
                print(f"❌ [Unanswered] 无法获取论坛频道 {TARGET_FORUM_ID}")
                return None, [], []

        # 获取标签对象
        resolved_tag = next((t for t in forum_channel.available_tags if t.id == RESOLVED_TAG_ID), None)
        if not resolved_tag:
            print(f"❌ [Unanswered] 找不到标签: {RESOLVED_TAG_NAME}")
            return None, [], []

        # 扫描范围：活跃帖子 + 30天内的归档
        target_date = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)

        threads_to_analyze = []  # 需要发给AI判定的
        unchanged_results = []   # 没变动，直接复用缓存的

        # 获取所有待扫描线程列表
        all_threads = list(forum_channel.threads)
        try:
            async for t in forum_channel.archived_threads(limit=50):
                if t.created_at >= target_date:
                    all_threads.append(t)
        except Exception as e:
            print(f"⚠️ [Unanswered] 获取归档帖子失败: {e}")

        print(f"🔍 [Unanswered] 开始扫描 {len(all_threads)} 个帖子...")

        for thread in all_threads:
            # 1. 基础过滤
            if resolved_tag in thread.applied_tags:
                continue
            if thread.created_at < target_date:
                continue

            # 2. 获取历史记录与计算真实回复数
            # 抓取最近 10 条，足够判断是否有人回复
            try:
                recent_msgs = [m async for m in thread.history(limit=10, oldest_first=False)]
            except Exception as e:
                print(f"⚠️ 无法读取帖子 {thread.id} 历史: {e}")
                continue

            if not recent_msgs:
                continue

            # 【关键修改】计算非楼主回复数
            # 过滤掉 author.id == thread.owner_id 的消息
            helper_replies = [m for m in recent_msgs if m.author.id != thread.owner_id]
            true_reply_count = len(helper_replies)

            # 获取最后活跃信息
            last_msg = recent_msgs[0] # history默认最新在前
            last_msg_id = last_msg.id
            time_since_active = datetime.datetime.now(datetime.timezone.utc) - last_msg.created_at
            days_silent = time_since_active.days

            # 3. 缓存比对
            cached = self._get_cached_thread(thread.id)
            # 缓存命中条件：最后消息ID一致 AND 真实回复数一致 AND (静默期未满7天 或 已经是unsolved)
            # 如果静默期刚满7天，需要强制重新判定（因为可能变成“技术性静默已解决”）
            if cached and cached[0] == last_msg_id and cached[1] == true_reply_count:
                if days_silent < 7:
                    unchanged_results.append({
                        "thread_obj": thread,
                        "status": cached[2],
                        "reason": cached[3],
                        "reply_count": true_reply_count
                    })
                    continue

            # 4. 准备 AI 判定数据
            # 抓取首楼（用于获取问题描述和图片）
            starter_msg = None
            # 如果帖子短，recent_msgs[-1] 就是首楼；如果长，单独抓
            if len(recent_msgs) < 10:
                starter_msg = recent_msgs[-1]
            else:
                try:
                    async for m in thread.history(limit=1, oldest_first=True):
                        starter_msg = m
                        break
                except:
                    pass

            if not starter_msg:
                continue

            # 图片处理
            image_data = [] 
            if starter_msg.attachments:
                for att in starter_msg.attachments:
                    if att.content_type and att.content_type.startswith('image/'):
                        cached_desc = self._get_cached_image(att.id)
                        if cached_desc:
                            # 缓存命中：只发送文本描述，省 Token
                            image_data.append(f"[图片附件(ID:{att.id}) 已分析: {cached_desc}]")
                        else:
                            # 缓存未命中：下载转 Base64
                            try:
                                img_bytes = await att.read()
                                b64 = base64.b64encode(img_bytes).decode('utf-8')
                                image_data.append({
                                    "type": "image_url",
                                    "image_url": {"url": f"data:{att.content_type};base64,{b64}"},
                                    "attachment_id": att.id # 标记ID以便后续
                                })
                            except Exception as e:
                                print(f"❌ 图片读取失败: {e}")

            # 构建对话历史文本
            history_text = []
            # 取最近 10 条，转为正序
            for m in reversed(recent_msgs[:10]):
                role = "楼主" if m.author.id == thread.owner_id else f"用户{m.author.name}"
                history_text.append(f"[{m.created_at.strftime('%Y-%m-%d')}] {role}: {m.content}")

            threads_to_analyze.append({
                "thread_obj": thread,
                "data": {
                    "id": thread.id,
                    "title": thread.name,
                    "created_at": str(thread.created_at),
                    "days_silent": days_silent,
                    "true_reply_count": true_reply_count, # 核心指标
                    "starter_content": starter_msg.content,
                    "starter_images": image_data,
                    "recent_history": history_text
                }
            })

        return resolved_tag, threads_to_analyze, unchanged_results

    async def _call_gemini_batch(self, threads_data):
        """发送审计请求给 Gemini"""
        if not threads_data:
            return {}

        # System Prompt
        system_prompt = """
        你需要批量分析以下帖子数据，并判断其状态。

        【判定标准】
        1. 已解决:
           - 楼主回复了“谢谢”、“已解决”、“ok”等明确确认。
           - 有人给出了可行方案，且帖子静默超过7天 (days_silent >= 7)。
           - 有人追问细节但楼主超过7天未回。

        2. 未解决:
           - 对话仍在进行，无明确结论。
           - 零回复 (true_reply_count == 0)：这是最高优先级，表示只有楼主在自言自语或完全没人理。
           - 方案被楼主明确否定。

        【任务】
        1. 如果帖子有新的图片数据，请生成简短描述（如“报错截图：NullPointerException”），放入 new_image_descriptions。
        2. 返回纯 JSON。

        JSON结构:
        {
            "results": [
                {
                    "id": 12345,
                    "status": "solved" | "unsolved",
                    "reason": "简短判定理由",
                    "new_image_descriptions": [ {"attachment_id": 999, "desc": "..."} ]
                }
            ]
        }
        """

        # User Content (混合 Text 和 Image)
        user_content = []
        user_content.append({"type": "text", "text": "请分析以下帖子数据包：\n"})

        for item in threads_data:
            t_data = item['data']
            # 分离 Base64 图片对象 和 文本描述
            images_payload = []

            # 序列化 JSON 数据
            thread_info = {k:v for k,v in t_data.items() if k != 'starter_images'}
            # 将已缓存的图片描述加回 JSON
            cached_imgs = [x for x in t_data['starter_images'] if isinstance(x, str)]
            thread_info['cached_images'] = cached_imgs

            user_content.append({"type": "text", "text": f"\n--- Thread {t_data['id']} ---\n{json.dumps(thread_info, ensure_ascii=False)}\n"})

            # 处理未缓存图片
            uncached_imgs = [x for x in t_data['starter_images'] if isinstance(x, dict)]
            if uncached_imgs:
                user_content.append({"type": "text", "text": "该帖子包含以下新图片："})
                for img in uncached_imgs:
                    # 移除 attachment_id 字段，API 不接受
                    api_img = {k:v for k,v in img.items() if k != 'attachment_id'}
                    user_content.append(api_img)
                    user_content.append({"type": "text", "text": f"(Image Attachment ID: {img['attachment_id']})"})

        # 调用 API
        try:
            print(f"📤 [Unanswered] 发送 Gemini 请求，包含 {len(threads_data)} 个帖子...")
            if not self.bot.openai_client:
                print("❌ OpenAI 客户端未初始化")
                return {}

            response = await self.bot.openai_client.chat.completions.create(
                model=AI_MODEL_NAME, # 使用 .env 中的 IMAGE_DESCRIBE_MODEL
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ],
                response_format={"type": "json_object"},
                max_tokens=4096
            )

            content = response.choices[0].message.content
            return json.loads(content)
        except Exception as e:
            print(f"❌ [Unanswered] AI 请求失败: {e}")
            return {}

    # ================= 定时任务与指令 =================

    @tasks.loop(time=datetime.time(hour=4, minute=0)) # UTC 04:00 = Beijing 12:00
    async def daily_check_task(self):
        await self.bot.wait_until_ready()
        print("⏰ [Unanswered] 执行每日扫描...")
        await self.execute_check()

    @app_commands.command(name="待办清单", description="[管理员] 强制执行一次未解决帖子扫描")
    async def manual_check(self, interaction: discord.Interaction):
        # 权限检查：使用 bot.py 中加载的 admins/trusted
        user_id = interaction.user.id
        is_admin = (user_id in getattr(self.bot, 'admins', []))
        is_trusted = (user_id in getattr(self.bot, 'trusted_users', []))

        if not (is_admin or is_trusted):
            await interaction.response.send_message("❌ 权限不足", ephemeral=True)
            return

        await interaction.response.defer()
        stats = await self.execute_check()
        await interaction.followup.send(f"✅ 扫描完成。\n自动归档: {stats['solved']} 个\n未解决汇报: {stats['unsolved']} 个")

    async def execute_check(self):
        resolved_tag, threads_to_analyze, unchanged_results = await self._fetch_and_prepare_batch()

        if not resolved_tag:
            return {"solved": 0, "unsolved": 0}

        # 1. AI 判定
        ai_results_map = {}
        if threads_to_analyze:
            ai_response = await self._call_gemini_batch(threads_to_analyze)
            results_list = ai_response.get("results", [])
            
            for res in results_list:
                t_id = res.get("id")
                # 更新图片缓存
                if "new_image_descriptions" in res:
                    for img in res["new_image_descriptions"]:
                        self._cache_image_desc(img["attachment_id"], img["desc"])
                ai_results_map[t_id] = res

        # 2. 结果汇总
        final_solved = []
        final_unsolved = []

        # 处理新分析的数据
        for item in threads_to_analyze:
            t = item['thread_obj']
            res = ai_results_map.get(t.id)

            status = "unsolved"
            reason = "AI未响应/判定失败"
            reply_cnt = item['data']['true_reply_count']

            if res:
                status = res.get("status", "unsolved")
                reason = res.get("reason", "无理由")

            # 更新 Thread 缓存
            last_msg_id = t.last_message_id or 0
            self._update_thread_cache(t.id, last_msg_id, reply_cnt, status, reason)

            if status == "solved":
                final_solved.append((t, reason))
            else:
                final_unsolved.append((t, reply_cnt))

        # 处理缓存数据 (未变动的)
        for item in unchanged_results:
            if item['status'] == "solved":
                final_solved.append((item['thread_obj'], item['reason']))
            else:
                final_unsolved.append((item['thread_obj'], item['reply_count']))

        # 3. 执行操作：贴标签
        for t, reason in final_solved:
            if resolved_tag not in t.applied_tags:
                try:
                    new_tags = t.applied_tags[:4] # 限制标签数
                    new_tags.append(resolved_tag)
                    await t.edit(applied_tags=new_tags, reason="AI自动贴已解决标签")

                    embed = discord.Embed(
                        description=f"✅ **检测到本帖已满足解决条件**\n理由：{reason}\n(如有异议，请回复本帖，系统将自动撤销标签)",
                        color=discord.Color.green()
                    )
                    await t.send(embed=embed)
                except Exception as e:
                    print(f"❌ 贴标签失败 {t.name}: {e}")

        # 4. 执行操作：发送汇报
        if final_unsolved and REPORT_CHANNEL_ID:
            report_channel = self.bot.get_channel(REPORT_CHANNEL_ID)
            if report_channel:
                # 排序：0回复 (true_reply_count == 0) 的排前面
                zero_replies = [x for x in final_unsolved if x[1] == 0]
                others = [x for x in final_unsolved if x[1] > 0]

                # 构建 Embed
                embed = discord.Embed(
                    title=f"📅 {datetime.date.today()} 未解决问题汇总",
                    description="以下问题仍未解决，请大家看看是否能提供帮助！",
                    color=discord.Color.orange()
                )

                if zero_replies:
                    # 限制显示数量，防止Embed超长
                    lines = []
                    for t, cnt in zero_replies[:10]:
                        lines.append(f"🚨 **[{t.name}]({t.jump_url})** <t:{int(t.created_at.timestamp())}:R>")

                    if len(zero_replies) > 10:
                        lines.append(f"...还有 {len(zero_replies)-10} 个零回复帖子")

                    embed.add_field(name=f"🆘 零回复救援区 ({len(zero_replies)})", value="\n".join(lines), inline=False)

                if others:
                    lines = []
                    for t, cnt in others[:10]:
                        lines.append(f"• [{t.name}]({t.jump_url}) ({cnt}条他人回复)")

                    if len(others) > 10:
                        lines.append(f"...还有 {len(others)-10} 个讨论中帖子")

                    embed.add_field(name=f"💬 讨论进行中 ({len(others)})", value="\n".join(lines), inline=False)

                try:
                    await report_channel.send(embed=embed)
                except Exception as e:
                    print(f"❌ 发送汇报失败: {e}")

        return {"solved": len(final_solved), "unsolved": len(final_unsolved)}

    # ================= 反悔重开机制 =================
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """监听已解决帖子的新回复，自动重开"""
        # 忽略机器人
        if message.author.bot:
            return

        # 检查是否在目标论坛的帖子内
        if not isinstance(message.channel, discord.Thread):
            return

        thread = message.channel
        if thread.parent_id != TARGET_FORUM_ID:
            return

        # 检查是否有“已解决”标签
        if not thread.parent: return

        # 检查发帖时间是否超过 14 天
        now = datetime.datetime.now(datetime.timezone.utc)
        thread_age = now - thread.created_at
        if thread_age.days >= 14:
            # 超过 14 天的老帖子，即使有新回复也不再自动重开
            return

        resolved_tag = next((t for t in thread.parent.available_tags if t.id == RESOLVED_TAG_ID), None)

        if resolved_tag and resolved_tag in thread.applied_tags:
            try:
                # 移除标签
                new_tags = [t for t in thread.applied_tags if t.id != resolved_tag.id]
                await thread.edit(applied_tags=new_tags, reason=f"用户 {message.author.name} 新增回复，自动重开")

                await thread.send(f"🔓 **检测到新回复，已自动移除「✅已解决」标签。**\n本帖将进入明日的自动扫描队列。")

                # 强制删除缓存，确保下次扫描时重新判定
                self._delete_thread_cache(thread.id)
                print(f"🔓 [Unanswered] 帖子 {thread.id} 已重开")

            except Exception as e:
                print(f"❌ 反悔重开失败: {e}")

async def setup(bot):
    await bot.add_cog(UnansweredFilter(bot))