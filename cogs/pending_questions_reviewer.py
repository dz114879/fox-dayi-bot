import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import datetime
import sqlite3
import ast
import json
import asyncio
import io
from typing import Dict, List, Optional, Any, Sequence

# ================= 配置映射 =================
# 新手开帖 论坛频道 ID
try:
    TARGET_FORUM_ID = int(os.getenv("TARGET_FORUM_ID", 0))
except:
    TARGET_FORUM_ID = 0

# 新手答疑 汇报频道 ID
try:
    REPORT_CHANNEL_ID = int(os.getenv("REPORT_CHANNEL_ID", 0))
except:
    REPORT_CHANNEL_ID = 0

# 已解决标签 ID
try:
    RESOLVED_TAG_ID = int(os.getenv("RESOLVED_TAG_ID", 0))
except:
    RESOLVED_TAG_ID = 0

# 优先使用通用模型，如果没有则回退到图片描述模型
AI_MODEL_NAME = os.getenv("OPENAI_MODEL") or os.getenv("IMAGE_DESCRIBE_MODEL")
RESOLVED_TAG_NAME = os.getenv("RESOLVED_TAG_NAME", "已解决")
DB_DIR = "reviewer"
DB_PATH = os.path.join(DB_DIR, "unanswered.db")

class UnansweredFilter(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # AI 请求/响应跟踪信息（用于手动扫描后的管理员私信报告）
        self._last_ai_raw_response_text: Optional[str] = None
        self._last_ai_response_note: str = "尚未调用 AI"
        self._ai_request_logs: List[Dict[str, Any]] = []
        self._ai_expected_total_batches: int = 0
        self._ai_expected_total_threads: int = 0

        # 批处理配置
        self._ai_batch_size: int = 4
        self._ai_batch_interval_seconds: int = 10

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

    def _stringify_ai_content(self, content: Any) -> str:
        """把 OpenAI/Gemini 返回的 content 尽量转成可读文本。"""
        if content is None:
            return ""

        if isinstance(content, str):
            return content

        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict):
                    parts.append(str(part.get("text", "")))
                else:
                    part_text = getattr(part, "text", None)
                    if part_text is not None:
                        parts.append(str(part_text))

            joined = "\n".join([p for p in parts if p]).strip()
            return joined if joined else str(content)

        return str(content)

    def _parse_ai_json_response(self, content: Any) -> Optional[Dict[str, Any]]:
        """尽可能稳健地解析 AI 返回的 JSON 文本。"""
        raw_text = self._stringify_ai_content(content).strip()

        if not raw_text:
            return None

        candidates = [raw_text]

        # 兼容 ```json ... ``` 包裹
        if raw_text.startswith("```"):
            lines = raw_text.splitlines()
            if len(lines) >= 2:
                inner_lines = lines[1:]
                if inner_lines and inner_lines[-1].strip().startswith("```"):
                    inner_lines = inner_lines[:-1]
                stripped = "\n".join(inner_lines).strip()
                if stripped:
                    candidates.append(stripped)

        # 尝试从文本中抽取最外层 JSON 对象
        for text in list(candidates):
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                extracted = text[start:end + 1].strip()
                if extracted and extracted not in candidates:
                    candidates.append(extracted)

        for text in candidates:
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
                if isinstance(parsed, list):
                    return {"results": parsed}
            except Exception:
                pass

            # 兼容 AI 返回 Python 风格字典（单引号）
            try:
                parsed_py = ast.literal_eval(text)
                if isinstance(parsed_py, dict):
                    return parsed_py
                if isinstance(parsed_py, list):
                    return {"results": parsed_py}
            except Exception:
                pass

        preview = raw_text[:500].replace("\n", "\\n")
        print(f"⚠️ [Unanswered] AI 返回内容无法解析为 JSON，预览: {preview}")
        return None

    # ================= 核心逻辑：数据抓取与预处理 =================

    def _summarize_attachments(self, attachments: Sequence[discord.Attachment]) -> str:
        """把附件转换为纯文本占位符，如 [图片附件x3] [文件附件x2]。"""
        if not attachments:
            return ""

        image_count = 0
        file_count = 0
        image_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".heic"}

        for att in attachments:
            is_image = False
            if att.content_type and att.content_type.startswith("image/"):
                is_image = True
            else:
                lower_name = (att.filename or "").lower()
                for ext in image_exts:
                    if lower_name.endswith(ext):
                        is_image = True
                        break

            if is_image:
                image_count += 1
            else:
                file_count += 1

        parts = []
        if image_count > 0:
            parts.append(f"[图片附件x{image_count}]")
        if file_count > 0:
            parts.append(f"[文件附件x{file_count}]")
        return " ".join(parts)

    def _chunk_threads(self, threads_data: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        """按配置的批大小切分待分析帖子。"""
        if not threads_data:
            return []

        size = max(1, self._ai_batch_size)
        return [threads_data[i:i + size] for i in range(0, len(threads_data), size)]

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

        # 检查是否为论坛频道并获取标签对象
        if not isinstance(forum_channel, discord.ForumChannel):
            print(f"❌ [Unanswered] 频道 {TARGET_FORUM_ID} 不是论坛频道，无法使用标签功能")
            return None, [], []

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

            # 构建对话历史文本
            history_text = []
            # 取最近 10 条，转为正序
            for m in reversed(recent_msgs[:10]):
                role = "楼主" if m.author.id == thread.owner_id else f"用户{m.author.name}"
                attachment_hint = self._summarize_attachments(m.attachments)
                content_text = (m.content or "").strip()
                combined_text = " ".join([x for x in [content_text, attachment_hint] if x]).strip()
                if not combined_text:
                    combined_text = "(无文本内容)"
                history_text.append(f"[{m.created_at.strftime('%Y-%m-%d')}] {role}: {combined_text}")

            starter_content_text = (starter_msg.content or "").strip()
            starter_attachment_hint = self._summarize_attachments(starter_msg.attachments)

            threads_to_analyze.append({
                "thread_obj": thread,
                "data": {
                    "id": thread.id,
                    "title": thread.name,
                    "created_at": str(thread.created_at),
                    "days_silent": days_silent,
                    "true_reply_count": true_reply_count, # 核心指标
                    "starter_content": starter_content_text,
                    "starter_attachments": starter_attachment_hint,
                    "recent_history": history_text
                }
            })

        return resolved_tag, threads_to_analyze, unchanged_results

    async def _call_gemini_batch(self, threads_data: List[Dict[str, Any]], batch_index: int, total_batches: int) -> Dict[str, Any]:
        """发送单个批次的审计请求给 Gemini，返回结构化执行结果。"""
        started_at = datetime.datetime.now(datetime.timezone.utc)
        thread_ids = [int(item.get("data", {}).get("id", 0)) for item in threads_data]

        result: Dict[str, Any] = {
            "batch_index": batch_index,
            "total_batches": total_batches,
            "thread_ids": [tid for tid in thread_ids if tid],
            "thread_count": len(threads_data),
            "started_at": started_at.isoformat(),
            "ended_at": None,
            "duration_ms": 0,
            "ok": False,
            "json_ok": False,
            "results_count": 0,
            "error": "",
            "raw_text": "",
            "parsed": {}
        }

        if not threads_data:
            result["error"] = "空批次，无需请求"
            ended_at = datetime.datetime.now(datetime.timezone.utc)
            result["ended_at"] = ended_at.isoformat()
            result["duration_ms"] = int((ended_at - started_at).total_seconds() * 1000)
            return result

        # System Prompt
        system_prompt = """
        你需要批量分析以下帖子数据，并判断其状态，以 JSON 格式返回判断结果。

        【判定标准】
        1. 已解决:
           - 楼主回复了“谢谢”“已解决”“ok”等明确确认。
           - 有人给出了可行方案，且帖子静默超过7天 (days_silent >= 7)。
           - 有人追问细节但楼主超过7天未回。

        2. 未解决:
           - 对话仍在进行、问题描述不足或无明确结论。
           - 零回复 (true_reply_count == 0)：这是最高优先级，表示只有楼主在自言自语或完全没人理。
           - 方案被楼主明确否定。

        【任务】
        帖子内容中的附件会使用占位符表示：
        - [图片附件xN]
        - [文件附件xN]
        请只依据文字和上下文判断，不要臆测附件里的具体内容。

        返回内容必须为纯JSON，正文必须仅包含JSON。

        JSON结构:
        {
            "results": [
                {
                    "id": 12345,
                    "status": "solved" | "unsolved",
                    "reason": "简短判定理由"
                }
            ]
        }
        """

        # User Content（纯文本）
        payload_lines = ["请分析以下帖子数据："]
        for item in threads_data:
            t_data = item["data"]
            payload_lines.append(f"\n--- Thread {t_data['id']} ---")
            payload_lines.append(json.dumps(t_data, ensure_ascii=False))
        user_content = "\n".join(payload_lines)

        try:
            print(f"📤 [Unanswered] 发送 Gemini 请求（批次 {batch_index}/{total_batches}），包含 {len(threads_data)} 个帖子...")

            if not self.bot.openai_client:
                raise RuntimeError("OpenAI 客户端未初始化")

            response = await asyncio.to_thread(
                self.bot.openai_client.chat.completions.create,
                model=AI_MODEL_NAME,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ],
                response_format={"type": "json_object"},
                temperature=0.5,
                max_tokens=4096
            )

            if not response or not getattr(response, "choices", None):
                result["ok"] = False
                result["error"] = "AI 返回空响应对象"
            else:
                content = response.choices[0].message.content
                raw_text = self._stringify_ai_content(content)
                result["raw_text"] = raw_text

                parsed = self._parse_ai_json_response(content)
                if parsed is None:
                    result["ok"] = True
                    result["json_ok"] = False
                    result["error"] = "AI 请求成功，但响应无法解析为 JSON"
                    result["parsed"] = {}
                else:
                    results_list = parsed.get("results", []) if isinstance(parsed, dict) else []
                    result["ok"] = True
                    result["json_ok"] = True
                    result["results_count"] = len(results_list)
                    result["parsed"] = parsed
                    result["error"] = ""

            if result["raw_text"].strip():
                self._last_ai_raw_response_text = result["raw_text"]
            else:
                # 不覆盖已有的最近一次非空快照
                if not self._last_ai_raw_response_text:
                    self._last_ai_raw_response_text = ""

        except Exception as e:
            result["ok"] = False
            result["json_ok"] = False
            result["error"] = f"AI 请求失败: {e}"
            print(f"❌ [Unanswered] 批次 {batch_index}/{total_batches} 请求失败: {e}")

        finally:
            ended_at = datetime.datetime.now(datetime.timezone.utc)
            result["ended_at"] = ended_at.isoformat()
            result["duration_ms"] = int((ended_at - started_at).total_seconds() * 1000)

        return result

    def _build_ai_fallback_reason(self, thread_id: int, batch_index_map: Dict[int, int], batch_logs_map: Dict[int, Dict[str, Any]]) -> str:
        """当线程没有拿到有效 AI 结果时，生成可落库的失败原因。"""
        batch_idx = batch_index_map.get(thread_id)
        if not batch_idx:
            return "AI未命中批次/判定失败"

        log = batch_logs_map.get(batch_idx)
        if not log:
            return f"AI批次{batch_idx}日志缺失"

        if not log.get("ok"):
            err = (log.get("error") or "未知错误").strip()
            return f"AI请求失败(批次{batch_idx}): {err}"

        if not log.get("json_ok"):
            err = (log.get("error") or "响应无法解析JSON").strip()
            return f"AI响应异常(批次{batch_idx}): {err}"

        return f"AI结果缺失(批次{batch_idx})"

    # ================= 定时任务与指令 =================

    @tasks.loop(time=datetime.time(hour=4, minute=0)) # UTC 04:00 = Beijing 12:00
    async def daily_check_task(self):
        await self.bot.wait_until_ready()
        print("⏰ [Unanswered] 执行每日扫描...")
        await self.execute_check()

    async def _safe_defer(self, interaction: discord.Interaction):
        """一个绝对安全的“占坑”函数。"""
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

    def _build_last_ai_response_txt(self) -> str:
        """生成用于私信附件的 AI 批处理执行报告（含每批原始响应）。"""
        now_str = datetime.datetime.now(datetime.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")

        total_logs = len(self._ai_request_logs)
        api_success = sum(1 for x in self._ai_request_logs if x.get("ok"))
        json_success = sum(1 for x in self._ai_request_logs if x.get("json_ok"))
        api_failed = total_logs - api_success
        json_failed = api_success - json_success

        lines = [
            f"生成时间: {now_str}",
            f"模型: {AI_MODEL_NAME or '未配置'}",
            f"状态: {self._last_ai_response_note}",
            f"待分析帖子数: {self._ai_expected_total_threads}",
            f"计划请求数: {self._ai_expected_total_batches}（每批最多{self._ai_batch_size}帖，间隔{self._ai_batch_interval_seconds}秒）",
            f"实际执行批次: {total_logs}",
            f"接口成功: {api_success} | 接口失败: {api_failed}",
            f"JSON可解析: {json_success} | JSON异常: {json_failed}",
            ""
        ]

        if not self._ai_request_logs:
            lines.append("(本次没有 AI 批次请求记录)\n")
            return "\n".join(lines)

        lines.append("===== AI_BATCH_SUMMARY_BEGIN =====")
        for log in self._ai_request_logs:
            batch_idx = log.get("batch_index", "?")
            total_batches = log.get("total_batches", "?")
            thread_count = log.get("thread_count", 0)
            thread_ids = log.get("thread_ids", [])
            thread_ids_text = ", ".join(str(x) for x in thread_ids) if thread_ids else "-"
            ok_text = "SUCCESS" if log.get("ok") else "FAILED"
            json_text = "YES" if log.get("json_ok") else "NO"
            duration_ms = log.get("duration_ms", 0)
            error_text = (log.get("error") or "").strip()

            lines.append(f"[Batch {batch_idx}/{total_batches}] status={ok_text}, json_ok={json_text}, results={log.get('results_count', 0)}, duration_ms={duration_ms}")
            lines.append(f"thread_count={thread_count}, thread_ids=[{thread_ids_text}]")
            if error_text:
                lines.append(f"error={error_text}")
            lines.append("")
        lines.append("===== AI_BATCH_SUMMARY_END =====")
        lines.append("")

        for log in self._ai_request_logs:
            batch_idx = log.get("batch_index", "?")
            total_batches = log.get("total_batches", "?")
            raw_text = (log.get("raw_text") or "").strip()
            if not raw_text:
                raw_text = "(空响应)"

            lines.append(f"===== BATCH_{batch_idx}_OF_{total_batches}_RAW_BEGIN =====")
            lines.append(raw_text)
            lines.append(f"===== BATCH_{batch_idx}_OF_{total_batches}_RAW_END =====")
            lines.append("")

        return "\n".join(lines)

    async def _notify_manual_check_result(self, user, result_msg: str, success: bool):
        """手动扫描结束后，私信管理员并附带 AI 批处理报告 txt。"""
        status = "成功" if success else "失败"
        txt_content = self._build_last_ai_response_txt()
        file_name = f"unanswered_last_ai_response_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        file_obj = discord.File(io.BytesIO(txt_content.encode("utf-8")), filename=file_name)

        await user.send(
            f"🔔 **待办清单扫描任务已结束（{status}）**\n{result_msg}\n\n"
            f"已附带本次 AI 批处理执行报告（txt）。",
            file=file_obj
        )

    @app_commands.command(name="待办清单", description="[管理员] 强制执行一次未解决帖子扫描")
    async def manual_check(self, interaction: discord.Interaction):
        # 权限检查：仅管理员
        user_id = interaction.user.id
        admins = getattr(self.bot, 'admins', [])

        if user_id not in admins:
            await interaction.response.send_message("❌ 权限不足，仅限管理员使用", ephemeral=True)
            return

        # 黄金法则：永远先 defer
        await self._safe_defer(interaction)

        # 每次手动扫描前重置 AI 报告状态，避免误发旧记录
        self._last_ai_raw_response_text = None
        self._last_ai_response_note = "本次手动扫描尚未调用 AI"
        self._ai_request_logs = []
        self._ai_expected_total_batches = 0
        self._ai_expected_total_threads = 0

        run_success = False
        try:
            stats = await self.execute_check()
            run_success = True
            result_msg = f"✅ 扫描完成。\n自动归档: {stats['solved']} 个\n未解决汇报: {stats['unsolved']} 个"
        except Exception as e:
            self._last_ai_response_note = f"扫描流程异常: {e}"
            result_msg = f"❌ 扫描失败：{e}"

        # 使用 edit_original_response 更新占位符（因为 defer 时设置了 ephemeral=True，所以这里是私密的）
        await interaction.edit_original_response(content=result_msg)

        # 无论成功或失败，都私信通知并附带 AI 批处理报告
        try:
            await self._notify_manual_check_result(interaction.user, result_msg, run_success)
        except Exception as e:
            print(f"⚠️ 无法发送私信通知给 {interaction.user.name}: {e}")

    async def execute_check(self):
        # 每轮扫描都重置一次 AI 报告状态
        self._last_ai_raw_response_text = None
        self._last_ai_response_note = "本次扫描尚未调用 AI"
        self._ai_request_logs = []
        self._ai_expected_total_batches = 0
        self._ai_expected_total_threads = 0

        resolved_tag, threads_to_analyze, unchanged_results = await self._fetch_and_prepare_batch()

        if not resolved_tag:
            self._last_ai_response_note = "扫描提前结束：未找到目标论坛或已解决标签"
            return {"solved": 0, "unsolved": 0}

        # 1. AI 批次判定（每批最多 4 帖，间隔 10 秒）
        ai_results_map: Dict[int, Dict[str, Any]] = {}
        thread_batch_index_map: Dict[int, int] = {}

        if threads_to_analyze:
            batches = self._chunk_threads(threads_to_analyze)
            total_batches = len(batches)
            self._ai_expected_total_threads = len(threads_to_analyze)
            self._ai_expected_total_batches = total_batches

            print(f"🧾 [Unanswered] 本次待 AI 判定 {self._ai_expected_total_threads} 帖，预计发送 {total_batches} 个请求（每批最多{self._ai_batch_size}帖，间隔{self._ai_batch_interval_seconds}s）。")

            for idx, batch in enumerate(batches, start=1):
                for item in batch:
                    t_id = int(item.get("data", {}).get("id", 0))
                    if t_id:
                        thread_batch_index_map[t_id] = idx

                batch_log = await self._call_gemini_batch(batch, idx, total_batches)
                self._ai_request_logs.append(batch_log)

                parsed = batch_log.get("parsed", {})
                results_list = parsed.get("results", []) if isinstance(parsed, dict) else []
                for res in results_list:
                    t_id_raw = res.get("id")
                    try:
                        t_id = int(t_id_raw)
                    except Exception:
                        continue
                    ai_results_map[t_id] = res

                if idx < total_batches:
                    print(f"⏳ [Unanswered] 批次 {idx}/{total_batches} 完成，等待 {self._ai_batch_interval_seconds} 秒后发送下一批...")
                    await asyncio.sleep(self._ai_batch_interval_seconds)

            total_logs = len(self._ai_request_logs)
            api_success = sum(1 for x in self._ai_request_logs if x.get("ok"))
            json_success = sum(1 for x in self._ai_request_logs if x.get("json_ok"))
            api_failed = total_logs - api_success
            json_failed = api_success - json_success
            self._last_ai_response_note = (
                f"AI批次完成：共{total_logs}包，接口成功{api_success}包，JSON可解析{json_success}包，"
                f"接口失败{api_failed}包，解析失败{json_failed}包"
            )
        else:
            self._last_ai_response_note = "本次扫描无需 AI 判定（无新增/变更帖子）"

        batch_logs_map: Dict[int, Dict[str, Any]] = {
            int(log.get("batch_index")): log
            for log in self._ai_request_logs
            if log.get("batch_index") is not None
        }

        # 2. 结果汇总
        final_solved = []
        final_unsolved = []

        # 处理新分析的数据
        for item in threads_to_analyze:
            t = item['thread_obj']
            res = ai_results_map.get(t.id)

            status = "unsolved" 
            reason = self._build_ai_fallback_reason(t.id, thread_batch_index_map, batch_logs_map)
            reply_cnt = item['data']['true_reply_count']

            if res:
                status = str(res.get("status", "unsolved")).strip().lower()
                if status not in ("solved", "unsolved"):
                    status = "unsolved"
                reason = res.get("reason", "无理由")
                if not isinstance(reason, str):
                    reason = str(reason)

            reason = reason.strip() if isinstance(reason, str) else str(reason)
            if not reason:
                reason = "无理由"

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
        # 注意：这里需要重新获取最新的 tags 列表
        if not isinstance(thread.parent, discord.ForumChannel): return

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