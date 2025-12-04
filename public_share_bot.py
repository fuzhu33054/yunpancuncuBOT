# public_share_bot.py (毕业设计最终版 - 集成新功能并优化)

import os
import logging
import secrets
import re
import asyncio
import functools
from typing import Dict, List, Optional
from dotenv import load_dotenv

import psycopg2
from psycopg2 import pool

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TimedOut, BadRequest
from telegram.ext import (
    Application,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler
)

# 加载 .env 文件中的环境变量
load_dotenv()

# --- 环境变量读取 ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
PRIVATE_CHANNEL_ID = os.getenv("PRIVATE_CHANNEL_ID")
DATABASE_URL = os.getenv("DATABASE_URL")
REQUIRED_GROUP_ID = os.getenv("REQUIRED_GROUP_ID")
GROUP_INVITE_LINK = os.getenv("GROUP_INVITE_LINK")
PROXY_URL = os.getenv("PROXY_URL")

# --- 日志记录配置 ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- 常量 ---
UPLOAD_BUTTON_TEXT = "📤 上传文件"
FINISH_UPLOAD_BUTTON_TEXT = "✅ 完成上传"
FILES_PER_PAGE = 10

# --- 适配数据库 SSL 连接 (本地测试时自动跳过) ---
if DATABASE_URL and 'sslmode' not in DATABASE_URL and 'localhost' not in DATABASE_URL:
    if '?' in DATABASE_URL:
        DATABASE_URL += '&sslmode=require'
    else:
        DATABASE_URL += '?sslmode=require'
    logger.info("已为数据库连接添加 'sslmode=require' 参数。")

# --- 检查所有必要的环境变量 ---
if not all([BOT_TOKEN, PRIVATE_CHANNEL_ID, DATABASE_URL, REQUIRED_GROUP_ID, GROUP_INVITE_LINK]):
    raise ValueError("错误：请确保所有必需的环境变量都已设置。")

# --- 数据库连接池 ---
try:
    db_pool = psycopg2.pool.SimpleConnectionPool(
        1,
        10,
        dsn=DATABASE_URL,
        connect_timeout=10
    )
    logger.info("数据库连接池初始化成功。")
except psycopg2.OperationalError as e:
    logger.error(f"无法连接到数据库: {e}")
    raise e

# --- 数据库初始化函数 ---
def setup_database():
    conn = None
    try:
        conn = db_pool.getconn()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS files (
                share_id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
                uploader_id BIGINT NOT NULL, timestamp TIMESTAMPTZ DEFAULT NOW()
            )
        ''')
        cursor.execute("SELECT 1 FROM information_schema.columns WHERE table_name='files' AND column_name='file_caption'")
        if cursor.fetchone() is None:
            cursor.execute("ALTER TABLE files ADD COLUMN file_caption TEXT DEFAULT '未命名文件'")
            logger.info("成功添加 'file_caption' 字段到数据库。")
        cursor.execute("SELECT 1 FROM information_schema.columns WHERE table_name='files' AND column_name='file_type'")
        if cursor.fetchone() is None:
            cursor.execute("ALTER TABLE files ADD COLUMN file_type VARCHAR(50) DEFAULT '文件'")
            logger.info("成功添加 'file_type' 字段到数据库。")
        cursor.execute("SELECT 1 FROM pg_class WHERE relname = 'idx_uploader_id'")
        if cursor.fetchone() is None:
            cursor.execute("CREATE INDEX idx_uploader_id ON files(uploader_id)")
            logger.info("成功创建 'uploader_id' 索引。")
        conn.commit()
        cursor.close()
        logger.info("成功连接到 PostgreSQL 数据库并确认表结构。")
    except Exception as e:
        logger.error(f"数据库初始化失败: {e}")
        raise e
    finally:
        if conn: db_pool.putconn(conn)

# --- 检查用户是否在指定群组中的函数 ---
async def is_user_in_group(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=REQUIRED_GROUP_ID, user_id=user_id)
        return member.status not in [ChatMemberStatus.LEFT, ChatMemberStatus.BANNED]
    except Exception as e:
        logger.error(f"无法检查用户 {user_id} 的成员资格: {e}")
        return False

# --- 用于验证群组成员资格的装饰器 (★ 已修复 ★) ---
def require_group_membership(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        # ★★★ 核心修复点: 检查 effective_user 是否存在 ★★★
        if not update.effective_user:
            return 

        user_id = update.effective_user.id
        if await is_user_in_group(user_id, context):
            return await func(update, context, *args, **kwargs)
        else:
            # 假设你的机器人链接变量如下 (请替换为你实际的机器人链接)
            BOT_START_LINK = "https://t.me/sogoaibot?start=8438438776" 
            
            if update.callback_query:
                await update.callback_query.answer("⚠️ 操作受限，请先启动机器人加入我们的官方群组。", show_alert=True)
            else:
                await update.message.reply_text(
                    f"⚠️ **操作受限**\n\n"
                    f"您需要启动机器人然后加入我们的官方群组才能使用此功能。\n\n"
                    f"🚀 [启动机器人]({BOT_START_LINK})\n" # <--- 新增的第一行链接
                    f"👉 [点击这里加入群组]({GROUP_INVITE_LINK})", # <--- 原有的第二行链接
                    parse_mode="Markdown",
                    disable_web_page_preview=True
                )
            return None
    return wrapper

# --- 创建高级分页键盘的辅助函数 ---
def create_pagination_keyboard(current_page: int, total_pages: int, callback_prefix: str, share_id: Optional[str] = None) -> List[List[InlineKeyboardButton]]:
    keyboard = []
    
    if total_pages > 1:
        page_buttons = []
        start_page = max(1, current_page - 2)
        end_page = min(total_pages, start_page + 4)
        start_page = max(1, end_page - 4)

        for p in range(start_page, end_page + 1):
            text = f"· {p} ·" if p == current_page else str(p)
            callback_data = "noop" if p == current_page else f"{callback_prefix}:{p}"
            if share_id:
                callback_data += f":{share_id}"
            page_buttons.append(InlineKeyboardButton(text, callback_data=callback_data))
        keyboard.append(page_buttons)

    nav_row, ends_row = [], []
    if current_page > 1:
        prev_callback = f"{callback_prefix}:{current_page - 1}"
        first_callback = f"{callback_prefix}:1"
        if share_id:
            prev_callback += f":{share_id}"
            first_callback += f":{share_id}"
        nav_row.insert(0, InlineKeyboardButton("‹ 上一页", callback_data=prev_callback))
        ends_row.insert(0, InlineKeyboardButton("« 首页", callback_data=first_callback))
    
    if current_page < total_pages:
        next_callback = f"{callback_prefix}:{current_page + 1}"
        last_callback = f"{callback_prefix}:{total_pages}"
        if share_id:
            next_callback += f":{share_id}"
            last_callback += f":{share_id}"
        nav_row.append(InlineKeyboardButton("下一页 ›", callback_data=next_callback))
        ends_row.append(InlineKeyboardButton("末页 »", callback_data=last_callback))

    if nav_row: keyboard.append(nav_row)
    if ends_row: keyboard.append(ends_row)
        
    return keyboard

# --- 分页显示分享链接的核心函数 ---
async def show_shared_files_page(update: Update, context: ContextTypes.DEFAULT_TYPE, share_id: str, page: int = 1):
    conn = None
    try:
        conn = db_pool.getconn()
        cursor = conn.cursor()
        cursor.execute("SELECT message_id, file_caption FROM files WHERE share_id = %s", (share_id,))
        result = cursor.fetchone()
        cursor.close()

        if not result:
            await update.effective_message.reply_text("❌ 抱歉，这个分享链接无效或文件已被移除。")
            return

        message_ids_str, file_caption = result
        all_ids = [int(i) for i in message_ids_str.split(',')]
        total_files = len(all_ids)

        if total_files == 0:
            await update.effective_message.reply_text(f"ℹ️ “{file_caption}”中没有文件。")
            return

        total_pages = (total_files + FILES_PER_PAGE - 1) // FILES_PER_PAGE
        page = max(1, min(page, total_pages))
        offset = (page - 1) * FILES_PER_PAGE
        ids_to_send = all_ids[offset : offset + FILES_PER_PAGE]
        
        sent_messages = await context.bot.copy_messages(chat_id=update.effective_chat.id, from_chat_id=PRIVATE_CHANNEL_ID, message_ids=ids_to_send)
        context.user_data['last_page_file_ids'] = [msg.message_id for msg in sent_messages]

        # ▼▼▼▼▼▼▼▼▼▼ 新增：让程序暂停 1 秒钟 ▼▼▼▼▼▼▼▼▼▼
        # 这能确保图片/视频先加载出来，然后控制面板才会在最底部出现
        await asyncio.sleep(3) 
        # ▲▲▲▲▲▲▲▲▲▲ 结束新增 ▲▲▲▲▲▲▲▲▲▲

        keyboard = create_pagination_keyboard(page, total_pages, "spage", share_id)
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # ▼▼▼▼▼▼▼▼▼▼ 修改开始 ▼▼▼▼▼▼▼▼▼▼
        
        # 你的广告文本定义
        AD_TEXT = "极搜资源搜索搜片搜群" 
        AD_LINK = "https://t.me/jisou?start=a_8438438776" # 这里换成你的链接
        
        text = (
            f"▶️ 正在查看: {file_caption}\n"
            f"💎 [{AD_TEXT}]({AD_LINK})\n"
            f"📑 第 {page} 页 / 共 {total_pages} 页 (总计 {total_files} 个文件)"
        )
        
        new_panel = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            reply_markup=reply_markup,
            parse_mode="Markdown",
            disable_web_page_preview=True  # ★★★ 关键修改：禁止显示网页预览 ★★★
        )
        context.user_data['last_control_panel_id'] = new_panel.message_id

    except Exception as e:
        logger.error(f"分页显示分享ID {share_id} 失败: {e}")
        await update.effective_message.reply_text("❌ 处理文件时出错，请稍后再试。")
    finally:
        if conn: db_pool.putconn(conn)

# --- /start 命令处理器 ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ★★★ 安全检查: 忽略来自频道的更新 ★★★
    if not update.effective_user: return

    user = update.effective_user
    context.user_data.clear()
    
    target_share_id = context.args[0] if context.args else None
    
    if not target_share_id:
        target_share_id = context.user_data.get('pending_share_id')
    
    # 私聊重定向逻辑
    if update.effective_chat.type != ChatType.PRIVATE and target_share_id:
        bot_username = context.bot_data.get('bot_username', '')
        private_start_url = f"https://t.me/{bot_username}?start={target_share_id}"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔒 点击私聊获取文件", url=private_start_url)]])
        await update.message.reply_text("请在与我的私聊中获取文件，以保护您的隐私。", reply_markup=keyboard, quote=True)
        return

    if target_share_id:
        if await is_user_in_group(user.id, context):
            # ... 验证通过逻辑 ...
            verification_message = await update.message.reply_text("✅ 验证通过！正在为您准备文件...")
            context.user_data.pop('pending_share_id', None)
            await show_shared_files_page(update, context, share_id=target_share_id, page=1)
            await verification_message.delete()
        else:
            # --- 修改部分 ---
            context.user_data['pending_share_id'] = target_share_id
            bot_username = context.bot_data.get('bot_username', '')
            
            # 1. 重试链接 (用于 "我已加入，点此获取文件")
            retry_url = f"https://t.me/{bot_username}?start={target_share_id}"
            
            # 2. 外部机器人链接 (用于 "启动机器人")
            # ★★★ 请在这里填入你想让用户点击 "启动机器人" 时跳转的链接 ★★★
            EXTERNAL_BOT_LINK = "https://t.me/sogoaibot?start=8438438776" 

            # 定义三个按钮，垂直排列
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🚀 启动机器人", url=EXTERNAL_BOT_LINK)],      # 第1个按钮：无关验证，单纯跳转
                [InlineKeyboardButton("👉 点击这里加入群组", url=GROUP_INVITE_LINK)], # 第2个按钮：去加群
                [InlineKeyboardButton("✅ 我已加入，点此获取文件", url=retry_url)]    # 第3个按钮：重试获取
            ])
            
            reply_text = (
                "⚠️ **访问受限**\n\n"
                "您需要先启动机器人然后成为我们官方群组的成员，才能获取此文件。\n\n"
                "1. 先点击上方按钮启动然后点中间按钮加入群组。\n"
                "2. 加入成功后，点击最下方的“我已加入”按钮获取文件。"
            )
            
            await update.message.reply_text(reply_text, reply_markup=keyboard, parse_mode="Markdown")
            # --- 修改结束 ---
    else:
        context.user_data['state'] = 'default'
        keyboard = ReplyKeyboardMarkup([[KeyboardButton(text=UPLOAD_BUTTON_TEXT)]], resize_keyboard=True, one_time_keyboard=False)
        await update.message.reply_text("欢迎使用文件分享机器人！点击下方按钮上传文件或相册。\n\n使用 /help 查看更多指令。", reply_markup=keyboard)
# 注意这里 ↑ 补上了 )

# --- /help 命令处理器 ---
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ★★★ 安全检查: 忽略来自频道的更新 ★★★
    if not update.effective_user: return
    help_text = ("你好！我是一个文件分享机器人。\n\n""**用法一：上传文件 (仅限私聊)**\n""1\\. 点击 **'📤 上传文件'** 按钮进入上传模式。\n""2\\. 发送任意数量的文件、视频、图片或相册。\n""3\\. 全部发送完毕后，点击 **'✅ 完成上传'** 按钮，即可获得**一个包含所有文件的**分享链接。\n\n""**用法二：获取文件**\n""▪️ **点击** 朋友分享给你的链接，文件将会**分页显示**。\n""▪️ 如果机器人提示，请先按要求加入群组。\n\n""**文件管理 (仅限私聊)**\n""▪️ 使用 /myfiles 命令来查看和管理您上传过的文件。\n\n""**使用条件:**\n""为防止滥用，您必须先加入我们的官方群组才能使用机器人。")
    await update.message.reply_text(help_text, parse_mode="MarkdownV2")

# --- /myfiles 的分页函数 ---
async def show_my_files_page(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 1):
    user_id = update.effective_user.id
    conn = None
    try:
        conn = db_pool.getconn()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM files WHERE uploader_id = %s", (user_id,))
        total_files = cursor.fetchone()[0]

        if total_files == 0:
            text = "您还没有上传过任何文件。使用 '上传文件' 按钮来分享您的第一个文件吧！"
            query = update.callback_query
            if query: await query.edit_message_text(text, reply_markup=None)
            else: await update.message.reply_text(text)
            return
        
        total_pages = (total_files + FILES_PER_PAGE - 1) // FILES_PER_PAGE
        page = max(1, min(page, total_pages))
        offset = (page - 1) * FILES_PER_PAGE
        cursor.execute("SELECT share_id, file_caption FROM files WHERE uploader_id = %s ORDER BY timestamp DESC LIMIT %s OFFSET %s", (user_id, FILES_PER_PAGE, offset))
        files_on_page = cursor.fetchall()
        cursor.close()

        file_keyboard = [[InlineKeyboardButton(f"📄 {cap[:25]}...", callback_data=f"info:{sid}"), InlineKeyboardButton("🗑️ 删除", callback_data=f"delete:{sid}:{page}")] for sid, cap in files_on_page]
        pagination_keyboard = create_pagination_keyboard(page, total_pages, "page")
        
        full_keyboard = file_keyboard + pagination_keyboard
        reply_markup = InlineKeyboardMarkup(full_keyboard)
        text = f"这是您上传的文件列表 (第 {page} 页 / 共 {total_pages} 页):"

        query = update.callback_query
        if query:
            try:
                await query.edit_message_text(text=text, reply_markup=reply_markup)
            except BadRequest as e:
                if "Message is not modified" in str(e): pass
                else: raise e
        else:
            await update.message.reply_text(text=text, reply_markup=reply_markup)

    except Exception as e:
        logger.error(f"显示用户 {user_id} 文件列表第 {page} 页失败: {e}")
    finally:
        if conn: db_pool.putconn(conn)

# --- /myfiles 命令处理器 ---
@require_group_membership
async def my_files_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("请在与我的私聊中使用此命令来管理您的文件。", quote=True)
        return
    await show_my_files_page(update, context, page=1)

# --- 内联按钮回调处理器 ---
async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":", 2)
    action = parts[0]
    
    if action == "spage":
        last_page_ids = context.user_data.pop('last_page_file_ids', [])
        if last_page_ids:
            try: await context.bot.delete_messages(chat_id=query.message.chat_id, message_ids=last_page_ids)
            except Exception as e: logger.warning(f"删除旧文件消息失败: {e}")
        
        last_panel_id = context.user_data.pop('last_control_panel_id', None)
        if last_panel_id:
            try: await context.bot.delete_message(chat_id=query.message.chat_id, message_id=last_panel_id)
            except Exception as e: logger.warning(f"删除旧控制面板失败: {e}")

    if action == "page":
        await show_my_files_page(update, context, page=int(parts[1]))
    elif action == "spage":
        if len(parts) < 3: return
        await show_shared_files_page(update, context, share_id=parts[2], page=int(parts[1]))
    elif action == "delete":
        if len(parts) < 3: return
        share_id, current_page = parts[1], int(parts[2])
        user_id = query.from_user.id
        conn = None
        try:
            conn = db_pool.getconn()
            cursor = conn.cursor()
            cursor.execute("SELECT uploader_id, message_id FROM files WHERE share_id = %s", (share_id,))
            result = cursor.fetchone()

            if not result:
                await query.answer("🤔 文件好像已经被删除了。", show_alert=True)
                return

            uploader_id, message_ids_str = result
            if uploader_id != user_id:
                await query.answer("🚫 您没有权限删除此文件。", show_alert=True)
                return

            cursor.execute("DELETE FROM files WHERE share_id = %s", (share_id,))
            conn.commit()
            cursor.close()
            await query.answer("✅ 文件记录已删除。正在刷新列表...", show_alert=False)
            await show_my_files_page(update, context, page=current_page)

            try:
                message_ids = [int(i) for i in message_ids_str.split(',')]
                await context.bot.delete_messages(chat_id=PRIVATE_CHANNEL_ID, message_ids=message_ids)
                logger.info(f"成功从私有频道删除消息: {message_ids}")
            except BadRequest as e:
                if "message can't be deleted" in e.message: logger.warning(f"无法从私有频道删除消息 {message_ids_str}: 消息太旧或已被删除。")
                else: raise e
            except Exception as e:
                logger.error(f"删除私有频道消息 {message_ids_str} 时发生未知错误: {e}")

        except Exception as e:
            logger.error(f"删除文件 {share_id} 失败: {e}")
            await query.message.reply_text("❌ 删除文件时发生内部错误。")
        finally:
            if conn: db_pool.putconn(conn)
    elif action == "info":
        share_id = parts[1]
        bot_username = context.bot_data.get('bot_username', '')
        link = f"https://t.me/{bot_username}?start={share_id}"
        await query.message.reply_text(f"这是您选择的文件的分享链接：\n`{link}`", parse_mode="Markdown")
    elif action == "noop":
        return

# --- 按钮和文件处理器 (★ 已修复 ★) ---
def escape_markdown_v2(text: str) -> str:
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)
@require_group_membership
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE: return
    context.user_data.clear()
    context.user_data['state'] = 'awaiting_file'
    context.user_data['session_message_ids'] = []
    context.user_data['session_file_count'] = 0
    keyboard = ReplyKeyboardMarkup([[KeyboardButton(text=FINISH_UPLOAD_BUTTON_TEXT)]], resize_keyboard=True, one_time_keyboard=False)
    await update.message.reply_text("好的，现在请直接发送您要上传的任意数量的文件或相册。\n\n全部发送完毕后，点击下方的“完成上传”按钮来生成**一个**分享链接。", reply_markup=keyboard)
async def finish_upload_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ★★★ 安全检查: 忽略来自频道的更新 ★★★
    if not update.effective_user: return
    if update.effective_chat.type != ChatType.PRIVATE: return
    processing_message = await update.message.reply_text("好的，正在为您生成专属分享链接，请稍候...")
    user = update.effective_user
    user_id = user.id
    session_message_ids = context.user_data.pop('session_message_ids', [])
    total_files = context.user_data.pop('session_file_count', 0)
    if session_message_ids:
        conn = None
        try:
            share_id = secrets.token_urlsafe(8)
            bot_username = context.bot_data.get('bot_username')
            final_link = f"https://t.me/{bot_username}?start={share_id}"
            ids_str = ",".join(map(str, session_message_ids))
            caption = f"批量上传 (共 {total_files} 个文件)"
            file_type = "合集"
            conn = db_pool.getconn()
            cursor = conn.cursor()
            cursor.execute("INSERT INTO files (share_id, message_id, uploader_id, file_caption, file_type) VALUES (%s, %s, %s, %s, %s)",(share_id, ids_str, user_id, caption, file_type))
            conn.commit()
            cursor.close()
            user_message = (f"🎉 **上传已完成！**\n\n您本次上传的 **{total_files}** 个文件已全部绑定到下面这**一个**链接中。\n\n**您的专属分享链接：**\n`{final_link}`")
            await processing_message.edit_text(text=user_message, parse_mode="Markdown", disable_web_page_preview=True)
            escaped_link = escape_markdown_v2(final_link)
            escaped_full_name = escape_markdown_v2(user.full_name)
            escaped_username = escape_markdown_v2(user.username) if user.username else ""
            username_str = f"\\(@{escaped_username}\\)" if escaped_username else ""
            log_message = (f"*新文件上传日志 \\(合集\\)*\n\n*上传者:* {escaped_full_name} {username_str}\n*用户ID:* `{user_id}`\n*文件总数:* {total_files}\n*分享链接:* {escaped_link}")
            await context.bot.send_message(chat_id=PRIVATE_CHANNEL_ID, text=log_message, parse_mode="MarkdownV2", disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"完成上传并写入数据库失败: {e}")
            await processing_message.edit_text(text="❌ 生成链接时发生错误，请稍后再试。")
        finally:
            if conn: db_pool.putconn(conn)
    else:
        await processing_message.edit_text(text="您本次没有上传任何文件。")
    context.user_data.clear()
    context.user_data['state'] = 'default'
    keyboard = ReplyKeyboardMarkup([[KeyboardButton(text=UPLOAD_BUTTON_TEXT)]], resize_keyboard=True, one_time_keyboard=False)
    await update.message.reply_text("上传会话已结束。您可以再次点击按钮开始新的上传。", reply_markup=keyboard)
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ★★★ 安全检查: 忽略来自频道的更新 ★★★
    if not update.effective_user: return
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("请在与我的私聊中使用此命令。", quote=True)
        return
    context.user_data.clear()
    await finish_upload_handler(update, context)
async def process_and_collect_files_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    user_id, chat_id, message_ids = job.data
    try:
        forwarded = await context.bot.forward_messages(PRIVATE_CHANNEL_ID, chat_id, message_ids)
        forwarded_ids = [msg.message_id for msg in forwarded]
        user_data = context.application.user_data.get(user_id, {})
        if 'session_message_ids' not in user_data: user_data['session_message_ids'] = []
        if 'session_file_count' not in user_data: user_data['session_file_count'] = 0
        user_data['session_message_ids'].extend(forwarded_ids)
        user_data['session_file_count'] += len(forwarded_ids)
        logger.info(f"用户 {user_id} 的会话中新增 {len(forwarded_ids)} 个文件。当前总数: {user_data['session_file_count']}")
    except Exception as e:
        logger.error(f"处理并收集文件任务失败: {e}")
        try: await context.bot.send_message(chat_id=chat_id, text=f"⚠️ 处理其中一个文件时出错，该文件可能未保存。请重试或联系管理员。")
        except Exception: pass
    finally:
        media_group_id = job.name
        if media_group_id and media_group_id in context.bot_data: del context.bot_data[media_group_id]
@require_group_membership
async def file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE: return
    if context.user_data.get('state') != 'awaiting_file':
        await update.message.reply_text("请先点击 '📤 上传文件' 按钮，然后再发送文件。")
        return
    user = update.effective_user
    media_group_id = update.message.media_group_id
    if media_group_id:
        job_name = str(media_group_id)
        group_context = context.bot_data.setdefault(job_name, {})
        is_first_in_group = not group_context.get('message_ids')
        group_context.setdefault('message_ids', []).append(update.message.message_id)
        if is_first_in_group:
            try: await update.message.reply_text("收到了您的相册，正在处理中... 请在所有文件发送完毕后，再点击'完成上传'。", quote=True)
            except Exception: pass
        for job in context.job_queue.get_jobs_by_name(job_name): job.schedule_removal()
        context.job_queue.run_once(process_and_collect_files_job, 2, data=[user.id, update.effective_chat.id, group_context['message_ids']], name=job_name)
    else: 
        try: await update.message.reply_text("收到文件，正在处理...", quote=True)
        except Exception: pass
        context.job_queue.run_once(process_and_collect_files_job, 0, data=[user.id, update.effective_chat.id, [update.message.id]])
async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ★★★ 安全检查: 忽略来自频道的更新 ★★★
    if update.effective_chat.type == ChatType.CHANNEL: return
    if not update.effective_user: return

    if update.effective_chat.type != ChatType.PRIVATE: return
    bot_username = context.bot_data.get('bot_username', '')
    user_text = update.message.text
    pattern = re.compile(rf"https?://t\.me/{bot_username}\?start=([A-Za-z0-9_-]+)")
    match = pattern.match(user_text)
    if match:
        context.args = [match.group(1)]
        await start(update, context)
    elif context.user_data.get('state') == 'awaiting_file':
        await update.message.reply_text("我正在等待您发送文件或相册。如果您不想上传了，可以点击下方的“完成上传”按钮。")
    else:
        await update.message.reply_text("请点击 '📤 上传文件' 按钮来开始，或者发送一个我生成的分享链接。")
async def post_init(application: Application) -> None:
    bot_info = await application.bot.get_me()
    application.bot_data['bot_username'] = bot_info.username
    logger.info(f"机器人 {bot_info.username} 已成功初始化。")
def main() -> None:
    setup_database()
    builder = Application.builder().token(BOT_TOKEN).post_init(post_init)
    if PROXY_URL:
        builder.proxy_url(PROXY_URL)
        logger.info(f"正在使用代理: {PROXY_URL}")
    application = builder.build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("myfiles", my_files_command))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(f'^{UPLOAD_BUTTON_TEXT}$'), button_handler))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(f'^{FINISH_UPLOAD_BUTTON_TEXT}$'), finish_upload_handler))
    # ★★★ 修改: 明确忽略来自频道的帖子 ★★★
    application.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.Document.ALL & ~filters.ChatType.CHANNEL, file_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))
    application.add_handler(CallbackQueryHandler(button_callback_handler))
    logger.info(">>> 机器人正在启动... <<<")
    application.run_polling()

if __name__ == '__main__':
    main()
