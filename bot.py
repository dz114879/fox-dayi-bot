import discord
from discord.ext import commands
from discord import app_commands
import psutil
import os
from dotenv import load_dotenv
from datetime import datetime, timedelta
import json
import openai
import asyncio
import mimetypes
import base64
import sqlite3
from cogs.logger import log_slash_command

load_dotenv()

# 从 .env 文件加载配置
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_BASE_URL = os.getenv("OPENAI_API_BASE_URL")
OPENAI_MODEL = os.getenv("OPENAI_MODEL")

# 设置机器人意图
intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # 添加这一行以获取服务器成员列表

# 并发控制
MAX_PARALLEL = int(os.getenv("MAX_PARALLEL", 5))  # 默认值为5
bot = commands.Bot(command_prefix='/', intents=intents)
bot.current_parallel_dayi_tasks = 0



# 初始化 OpenAI 客户端
if not all([OPENAI_API_KEY, OPENAI_API_BASE_URL, OPENAI_MODEL]):
    print(" [31m[错误] [0m 缺少必要的 OpenAI 环境变量。请检查 .env 文件。")
    bot.openai_client = None
else:
    bot.openai_client = openai.AsyncOpenAI(
        api_key=OPENAI_API_KEY,
        base_url=OPENAI_API_BASE_URL,
    )



# 动态加载prompt文件夹中的知识库
def load_knowledge_bases():
    """动态加载prompt文件夹中的所有txt文件作为知识库选项"""
    prompt_dir = 'prompt'
    knowledge_bases = []
    prompt_file_map = {}
    
    if not os.path.exists(prompt_dir):
        print(f" [警告] prompt文件夹不存在，将使用默认知识库")
        return [app_commands.Choice(name="无特定知识库", value="无")], {"无": "prompt/None.txt"}
    
    try:
        # 读取prompt文件夹中的所有txt文件
        for filename in os.listdir(prompt_dir):
            if filename.endswith('.txt'):
                file_path = os.path.join(prompt_dir, filename)
                # 去掉.txt扩展名作为value
                base_name = filename[:-4]
                
                # 创建友好的显示名称
                display_name = get_display_name(base_name)
                
                knowledge_bases.append(app_commands.Choice(name=display_name, value=base_name))
                prompt_file_map[base_name] = file_path
        
        # 如果没有找到任何txt文件，添加默认选项
        if not knowledge_bases:
            knowledge_bases.append(app_commands.Choice(name="无特定知识库", value="无"))
            prompt_file_map["无"] = "prompt/None.txt"
        
        print(f"✅ 已加载 {len(knowledge_bases)} 个知识库: {[choice.name for choice in knowledge_bases]}")
        
    except Exception as e:
        print(f" [错误] 加载知识库时出错: {e}")
        knowledge_bases = [app_commands.Choice(name="无特定知识库", value="无")]
        prompt_file_map = {"无": "prompt/None.txt"}
    
    return knowledge_bases, prompt_file_map

def get_display_name(base_name):
    """根据文件名生成友好的显示名称"""
    name_map = {
        "API": "API",
        "DC": "Discord",
        "Others": "酒馆杂项",
        "None": "无",
        "BuildCli": "Build&CLI特化"
    }
    return name_map.get(base_name, f"{base_name}")

# 在启动时加载知识库
KNOWLEDGE_BASES, PROMPT_FILE_MAP = load_knowledge_bases()

# 设置机器人的setup_hook来注册持久化视图
@bot.event
async def setup_hook():
    """机器人启动时的设置钩子，用于注册持久化视图"""
    # 在同步/注册斜杠命令前初始化数据库相关的内存数据（admins、trusted_users 等），
    # 避免 app_commands.check 在 on_ready 之前触发导致 AttributeError。
    load_database()

    # 加载所有cogs
    await load_cogs()
    print('✅ 所有扩展已加载')


class QuotaError(app_commands.AppCommandError):
    """自定义异常，用于表示用户配额不足"""
    pass

class FrequencyError(app_commands.AppCommandError):
    """自定义异常，用于表示用户请求频率过高"""
    pass

class ParallelLimitError(app_commands.AppCommandError):
    """自定义异常，用于表示并发达到上限"""
    pass

def deduct_quota(interaction: discord.Interaction) -> bool:
    """扣除用户配额并更新活动时间。管理员和受信任用户不受配额限制。假定用户已注册。"""
    user_id = interaction.user.id
    
    # 管理员和受信任用户不受配额限制，但仍然更新时间
    if user_id in bot.admins or user_id in bot.trusted_users:
        try:
            conn = sqlite3.connect('users.db')
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET time = ? WHERE id = ?",
                         (datetime.now().isoformat(), str(user_id)))
            conn.commit()
            conn.close()
            # 同时更新内存中的数据
            user_data = next((user for user in bot.users_data if int(user['id']) == user_id), None)
            if user_data:
                user_data['time'] = datetime.now().isoformat()
        except sqlite3.Error as e:
            print(f"[错误] 更新管理员/受信任用户时间时出错: {e}")
        return True

    # 对于普通用户，扣除配额
    try:
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()
        
        # 检查当前配额
        cursor.execute("SELECT quota FROM users WHERE id = ?", (str(user_id),))
        result = cursor.fetchone()
        
        if result and result[0] > 0:
            # 扣除配额并更新时间
            new_quota = result[0] - 1
            current_time = datetime.now().isoformat()
            cursor.execute("UPDATE users SET quota = ?, time = ? WHERE id = ?",
                         (new_quota, current_time, str(user_id)))
            conn.commit()
            
            # 同时更新内存中的数据
            user_data = next((user for user in bot.users_data if int(user['id']) == user_id), None)
            if user_data:
                user_data['quota'] = new_quota
                user_data['time'] = current_time
            
            conn.close()
            return True
        else:
            conn.close()
            raise QuotaError("错误：您的配额已用尽。")
            
    except sqlite3.Error as e:
        print(f"[错误] 扣除配额时出错: {e}")
        return False
    
    return False

def deduct_quota_no_time_update(interaction: discord.Interaction) -> bool:
    """扣除用户配额，但不更新活动时间。管理员和受信任用户不受配额限制。假定用户已注册。"""
    user_id = interaction.user.id

    # 管理员和受信任用户不受配额限制
    if user_id in bot.admins or user_id in bot.trusted_users:
        return True

    # 对于普通用户，扣除配额
    try:
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()
        
        # 检查当前配额
        cursor.execute("SELECT quota FROM users WHERE id = ?", (str(user_id),))
        result = cursor.fetchone()
        
        if result and result[0] > 0:
            # 扣除配额
            new_quota = result[0] - 1
            cursor.execute("UPDATE users SET quota = ? WHERE id = ?",
                         (new_quota, str(user_id)))
            conn.commit()
            
            # 同时更新内存中的数据
            user_data = next((user for user in bot.users_data if int(user['id']) == user_id), None)
            if user_data:
                user_data['quota'] = new_quota
            
            conn.close()
            return True
        else:
            conn.close()
            raise QuotaError("您的配额已用尽。")
            
    except sqlite3.Error as e:
        print(f"[错误] 扣除配额时出错: {e}")
        return False

    return False

def refund_quota(interaction: discord.Interaction, amount: int = 1):
    """返还用户指定的配额数量。"""
    user_id = interaction.user.id
    
    # 管理员和受信任用户不受配额限制，因此无需返还
    if user_id in bot.admins or user_id in bot.trusted_users:
        return

    try:
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()
        
        # 获取当前配额并增加
        cursor.execute("SELECT quota FROM users WHERE id = ?", (str(user_id),))
        result = cursor.fetchone()
        
        if result:
            new_quota = result[0] + amount
            cursor.execute("UPDATE users SET quota = ? WHERE id = ?",
                         (new_quota, str(user_id)))
            conn.commit()
            
            # 同时更新内存中的数据
            user_data = next((user for user in bot.users_data if int(user['id']) == user_id), None)
            if user_data:
                user_data['quota'] = new_quota
            
            print(f"配额已返还给用户 {user_id}，数量: {amount}。新配额: {new_quota}。")
        
        conn.close()
        
    except sqlite3.Error as e:
        print(f"[错误] 返还配额时出错: {e}")

def is_admin(interaction: discord.Interaction) -> bool:
    """检查用户是否为管理员"""
    # 某些情况下（例如 on_ready 之前）bot.admins 可能尚未初始化。
    # 使用 getattr 提供安全默认值，避免 Check 阶段抛异常导致 “该应用程序未响应”。
    admins = getattr(bot, "admins", [])
    return interaction.user.id in admins

def is_admin_or_trusted(interaction: discord.Interaction) -> bool:
    """检查用户是否为管理员或受信任用户"""
    admins = getattr(bot, "admins", [])
    trusted_users = getattr(bot, "trusted_users", [])
    return interaction.user.id in admins or interaction.user.id in trusted_users

def is_registered(interaction: discord.Interaction) -> bool:
    """检查用户是否已注册"""
    registered_users = getattr(bot, "registered_users", [])
    return interaction.user.id in registered_users

def load_database():
    """从 users.db SQLite数据库加载数据"""
    try:
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()
        
        # 加载管理员
        cursor.execute("SELECT id FROM admins")
        bot.admins = [int(row[0]) for row in cursor.fetchall()]
        
        # 加载受信任用户
        cursor.execute("SELECT id FROM trusted_users")
        bot.trusted_users = [int(row[0]) for row in cursor.fetchall()]
        
        # 加载kn_owner用户组
        try:
            cursor.execute("SELECT id FROM kn_owner")
            bot.kn_owner = [int(row[0]) for row in cursor.fetchall()]
        except sqlite3.OperationalError:
            # 如果kn_owner表不存在，初始化为空列表
            bot.kn_owner = []
        
        # 加载用户数据
        cursor.execute("SELECT id, quota, time, warning_count FROM users")
        bot.users_data = []
        for row in cursor.fetchall():
            user_data = {
                'id': row[0],
                'quota': row[1],
                'time': row[2],
                'banned': False,  # 默认值，因为数据库中没有banned字段
                'warning_count': row[3] if len(row) > 3 else 0  # 兼容旧数据
            }
            bot.users_data.append(user_data)
        
        bot.registered_users = [int(user['id']) for user in bot.users_data]
        
        conn.close()
    except sqlite3.Error as e:
        print(f"[错误] [0m SQLite数据库错误: {e}。将使用空数据库。")
        bot.admins = []
        bot.trusted_users = []
        bot.kn_owner = []
        bot.users_data = []
        bot.registered_users = []
    except Exception as e:
        print(f"[错误] [0m 加载数据库时发生未知错误: {e}。将使用空数据库。")
        bot.admins = []
        bot.trusted_users = []
        bot.kn_owner = []
        bot.users_data = []
        bot.registered_users = []

def save_database():
    """将数据保存到 users.db SQLite数据库"""
    try:
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()
        
        # 清空并重新插入管理员数据
        cursor.execute("DELETE FROM admins")
        for admin_id in bot.admins:
            cursor.execute("INSERT INTO admins (id) VALUES (?)", (str(admin_id),))
        
        # 清空并重新插入受信任用户数据
        cursor.execute("DELETE FROM trusted_users")
        for user_id in bot.trusted_users:
            cursor.execute("INSERT INTO trusted_users (id) VALUES (?)", (str(user_id),))
        
        # 清空并重新插入kn_owner用户数据
        try:
            cursor.execute("DELETE FROM kn_owner")
            for user_id in getattr(bot, 'kn_owner', []):
                cursor.execute("INSERT INTO kn_owner (id) VALUES (?)", (str(user_id),))
        except sqlite3.OperationalError:
            # 如果kn_owner表不存在，跳过
            pass
        
        # 清空并重新插入用户数据
        cursor.execute("DELETE FROM users")
        for user in bot.users_data:
            warning_count = user.get('warning_count', 0)  # 兼容旧数据
            cursor.execute("INSERT INTO users (id, quota, time, warning_count) VALUES (?, ?, ?, ?)",
                         (user['id'], user['quota'], user['time'], warning_count))
        
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        print(f" [31m[错误] [0m 保存数据到 users.db 时出错: {e}")
    except Exception as e:
        print(f" [31m[错误] [0m 保存数据库时发生未知错误: {e}")


def encode_image_to_base64(image_path):
    """
    将图片文件编码为Base64数据URI。
    """
    # 推断文件的MIME类型
    mime_type, _ = mimetypes.guess_type(image_path)
    if mime_type is None:
        mime_type = "application/octet-stream" # 默认类型

    # 读取���件内容
    with open(image_path, "rb") as image_file:
        base64_encoded_data = base64.b64encode(image_file.read()).decode('utf-8')

    # 返回格式化的Data URI
    return f"data:{mime_type};base64,{base64_encoded_data}"
 
@bot.event
async def on_ready():
    """机器人启动时触发"""
    # load_database() 已在 setup_hook() 中调用，这里不再重复加载，
    # 以免出现两次读取/打印以及潜在的数据覆盖。
    print(f'✅ 机器人���登录: {bot.user}')
    print(f'📊 连接到 {len(bot.guilds)} 个服务器')
    print(f'👑 管理员ID: {bot.admins}')
    print(f'🤝 受信任用户ID: {bot.trusted_users}')
    print(f'👥 用户数据库已加载，包含 {len(bot.users_data)} 个用户条目。')
    
    # 同步斜杠命令
    try:
        synced = await bot.tree.sync()
        print(f'✅ 已同步 {len(synced)} 个斜杠命令')
    except Exception as e:
        print(f' ❌ 同步命令失败: {e}')

@bot.tree.command(name='ping', description='显示机器人延迟和系统信息')
@app_commands.check(is_admin)
@app_commands.check(deduct_quota_no_time_update)
async def ping(interaction: discord.Interaction):
    """显示延迟、内存使用率、CPU使用率等系统信息"""
    # 计算延迟
    latency = round(bot.latency * 1000, 2)
    
    # 获取系统信息
    memory = psutil.virtual_memory()
    cpu_percent = psutil.cpu_percent(interval=1)
    
    # 创建嵌入消息
    embed = discord.Embed(
        title="Pong!",
        color=discord.Color.green()
    )
    embed.add_field(name="延迟", value=f"{latency} ms", inline=True)
    embed.add_field(name="内存使用率", value=f"{memory.percent}%", inline=True)
    embed.add_field(name="CPU使用率", value=f"{cpu_percent}%", inline=True)
    
    # 添加更多详细信息
    embed.add_field(
        name="内存详情", 
        value=f"已用: {memory.used / (1024**3):.2f} GB / 总计: {memory.total / (1024**3):.2f} GB",
        inline=False
    )
    
    await interaction.response.send_message(embed=embed)
    log_slash_command(interaction, True)













@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """处理应用命令错误"""
    log_slash_command(interaction, False)
    
    # 检查interaction是否已被响应，避免重复响应
    if interaction.response.is_done():
        print(f' 未处理的斜杠命令错误: {error}')
        return
    
    if isinstance(error, QuotaError):
        await interaction.response.send_message(f'❌ {error}', ephemeral=True)
    elif isinstance(error, FrequencyError):
        await interaction.response.send_message(f'❌ {error}', ephemeral=True)
    elif isinstance(error, ParallelLimitError):
        await interaction.response.send_message(f'❌ {error}', ephemeral=True)
    elif isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message('❌ 你没有权限使用此命令��请先 /register 注册。', ephemeral=True)
    else:
        print(f' 未处理的斜杠命令错误: {error}')
        await interaction.response.send_message('❌ 执行命令时发生未知错误。', ephemeral=True)

# 错误处理
@bot.event
async def on_command_error(ctx, error):
    """处理命令错误"""
    if isinstance(error, commands.CommandNotFound):
        # 静默忽略未找到的命令，不发送任何消息
        return
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ 你没有权限使用此命令")
    else:
        print(f'错误: {error}')

async def load_cogs():
    """加载 cogs 文件夹下的所有扩展"""
    cogs_dir = 'cogs'
    if not os.path.exists(cogs_dir):
        print(f" [警告] [0m 未找到 '{cogs_dir}' 文件夹，跳过加载 cogs。")
        return
        
    for filename in os.listdir(cogs_dir):
        # 确保是 Python 文件且不是 __init__.py
        if filename.endswith('.py') and filename != '__init__.py':
            try:
                # 扩展名是 cogs.文件名（不带.py）
                await bot.load_extension(f'{cogs_dir}.{filename[:-3]}')
                print(f'✅ 已成功加载 cog: {filename}')
            except Exception as e:
                print(f'❌ 加载 cog {filename} 时发生错误: {e}')

async def main():
    """机器人启动主函数"""
    token = os.getenv('DISCORD_BOT_TOKEN')
    if not token:
        print('❌ 错误: 未设置 DISCORD_BOT_TOKEN 环境变量。')
        print('请在 .env 文件中或系统环境中设置 DISCORD_BOT_TOKEN。')
        return

    async with bot:
        print('🚀 正在启动机器人...')
        await bot.start(token)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🤖 机器人被手动关闭。")