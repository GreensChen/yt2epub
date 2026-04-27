#!/usr/bin/env python3
"""
bot_service.py — Telegram bot 常駐服務

監聽 daily_brief.py 推送的訊息上的按鈕點擊：
- 📝 narrative   → 傳對談摘要
- 📋 transcript  → 傳重點逐字稿（時間戳 + 原文 + 翻譯）
- 🎧 convert     → 跑 yt2epub.py 轉成 epub 同步到 Kobo

啟動方式：
    python3 bot_service.py
（一般由 launchd 自動啟動）
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env", override=True)
except ImportError:
    pass


# ─────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
SUMMARIES_DIR = BASE_DIR / "summaries"
LOG_FILE = BASE_DIR / "bot_service.log"

# 同時最多跑幾個 epub 轉檔（避免電腦過熱）
MAX_CONCURRENT_CONVERSIONS = 1
conversion_lock = asyncio.Semaphore(MAX_CONCURRENT_CONVERSIONS)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
logger = logging.getLogger("bot_service")


def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def load_summary(video_id: str) -> Optional[dict]:
    f = SUMMARIES_DIR / f"{video_id}.json"
    if not f.exists():
        return None
    return json.loads(f.read_text())


# ─────────────────────────────────────────────
# 訊息切段（Telegram 4096 字上限）
# ─────────────────────────────────────────────

def split_messages(text: str, limit: int = 3800) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind("\n\n", 0, limit)
        if cut == -1:
            cut = text.rfind("\n", 0, limit)
        if cut == -1:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


# ─────────────────────────────────────────────
# 按鈕 handler
# ─────────────────────────────────────────────

async def cb_convert(query, video_id: str):
    """🎧 跑 yt2epub.py。"""
    data = load_summary(video_id)
    if not data:
        await query.answer("❌ 找不到摘要檔", show_alert=True)
        return

    url = data["video"]["url"]
    title = data["video"]["title"]
    channel = data.get("channel", "")
    safe_title = html_escape(title)

    # 標記為轉檔中
    await query.answer("⏳ 排隊中...")
    await query.edit_message_reply_markup(
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⏳ 轉檔中...", callback_data="noop"),
        ]])
    )

    async with conversion_lock:
        logger.info(f"開始轉檔: {video_id}  {title}")
        await query.message.reply_html(f"🎬 開始轉檔: <b>{safe_title}</b>\n（通常 1-3 分鐘）")

        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_BASE_URL"}
        env["PYTHONUNBUFFERED"] = "1"

        # 直接寫到 log 檔避免 PIPE 在連續 API call 卡死的怪事
        log_path = BASE_DIR / f"yt2epub_run_{video_id}.log"
        log_fp = open(log_path, "wb")
        logger.info(f"subprocess log: {log_path}")
        cmd = [
            sys.executable, "-u",
            str(BASE_DIR / "yt2epub.py"),
            url,
            "--title", title,
        ]
        if channel:
            cmd += ["--podcast-name", channel]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(BASE_DIR),
            stdout=log_fp,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        await proc.wait()
        log_fp.close()
        output = log_path.read_text(errors="replace")
        logger.info(f"yt2epub exit={proc.returncode}\n{output[-2000:]}")

    # 長時間任務後 callback query 已過期，改用 chat_id+message_id 直接編輯訊息
    chat_id = query.message.chat_id
    message_id = query.message.message_id
    bot = query.get_bot()

    async def safe_edit_markup(markup):
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, reply_markup=markup,
            )
        except Exception as e:
            logger.warning(f"無法更新原訊息按鈕（可能訊息太舊）: {e}")

    if proc.returncode == 0:
        # 從 stdout 抓 epub 路徑
        epub_path = ""
        for line in output.splitlines():
            if "ePub: " in line:
                epub_path = line.split("ePub: ", 1)[1].strip()
        await safe_edit_markup(InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ 已轉檔到 Kobo", callback_data="noop"),
        ]]))
        await query.message.reply_html(
            f"✅ <b>{safe_title}</b>\n轉檔完成，已同步到 Dropbox/Kobo。\n<code>{html_escape(epub_path)}</code>"
        )
    else:
        # 失敗 → 還原按鈕讓使用者重試
        await safe_edit_markup(InlineKeyboardMarkup([
            [InlineKeyboardButton("🔁 重試轉 epub", callback_data=f"convert:{video_id}")],
        ]))
        # 錯誤訊息只回傳最後 500 字
        err_tail = output[-500:].strip()
        await query.message.reply_html(
            f"❌ <b>轉檔失敗</b>: {safe_title}\n<pre>{html_escape(err_tail)}</pre>"
        )


# ─────────────────────────────────────────────
# 主分派
# ─────────────────────────────────────────────

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    data = query.data or ""
    if data == "noop":
        await query.answer()
        return

    if ":" not in data:
        await query.answer()
        return

    action, video_id = data.split(":", 1)
    logger.info(f"按鈕點擊: action={action}  video_id={video_id}")

    try:
        if action == "convert":
            await cb_convert(query, video_id)
        else:
            await query.answer(f"未知動作: {action}")
    except Exception as e:
        logger.exception(f"按鈕 handler 失敗: {e}")
        try:
            await query.message.reply_text(f"❌ 處理失敗: {e}")
        except Exception:
            pass


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "👋 yt2epub bot 上線\n\n"
        "每天下午 3 點推送 YouTube 訂閱清單摘要。\n\n"
        "<b>即時摘要</b>：直接傳 YouTube 連結（watch / youtu.be / shorts），\n"
        "我會回 150-200 字段落式摘要 + 🎧 轉 epub 按鈕。\n\n"
        "指令：\n"
        "  /run — 立刻跑一次 daily_brief\n"
        "  /list — 列出最近的摘要"
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    files = sorted(SUMMARIES_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        await update.message.reply_text("沒有摘要紀錄")
        return
    lines = ["最近 10 部："]
    for f in files[:10]:
        try:
            data = json.loads(f.read_text())
            ts = datetime.fromisoformat(data["saved_at"]).strftime("%m/%d")
            lines.append(f"• {ts} [{data['channel']}] {data['video']['title'][:50]}")
        except Exception:
            continue
    await update.message.reply_text("\n".join(lines))


# ─────────────────────────────────────────────
# 即時 URL 摘要：傳 YouTube 連結 → 抓字幕 → Claude 摘要 → 按鈕
# ─────────────────────────────────────────────

YOUTUBE_URL_RE = re.compile(
    r"(https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?[^\s]*v=|shorts/|live/|embed/)|youtu\.be/)[\w\-]{11}[^\s]*)"
)


def extract_youtube_url(text: str) -> Optional[str]:
    m = YOUTUBE_URL_RE.search(text or "")
    return m.group(1) if m else None


def fetch_video_meta(url: str) -> dict:
    """用 YouTube oEmbed 抓 title + author_name（不需 API key）。"""
    oembed = f"https://www.youtube.com/oembed?url={urllib.parse.quote(url, safe='')}&format=json"
    req = urllib.request.Request(oembed, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


async def handle_youtube_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """用戶傳 YouTube 連結 → 立即跑摘要 → 推送訊息 + 按鈕。"""
    msg_text = update.message.text or ""
    url = extract_youtube_url(msg_text)
    if not url:
        return

    # daily_brief 跟 yt2epub 都已 import 過，避免循環匯入這裡才載
    from daily_brief import (
        summarize_video,
        build_video_message,
        build_buttons,
        save_summary,
    )
    from yt2epub import extract_video_id

    video_id = extract_video_id(url)
    progress = await update.message.reply_text(f"⏳ 抓字幕 + 摘要中... ({video_id})")

    try:
        meta = fetch_video_meta(url)
        title = meta.get("title", video_id)
        channel = meta.get("author_name", "")
    except Exception as e:
        logger.warning(f"oEmbed 失敗 {url}: {e}")
        title = video_id
        channel = ""

    video = {
        "id": video_id,
        "title": title,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "published": "",
    }

    try:
        summary = await asyncio.to_thread(summarize_video, video, channel)
    except Exception as e:
        logger.exception(f"摘要失敗 {video_id}")
        await progress.edit_text(f"❌ 摘要失敗：{e}")
        return

    item = {"channel": channel, "video": video, "summary": summary}
    save_summary(item)

    text = build_video_message(item)
    if summary.get("no_transcript"):
        await progress.edit_text(text, parse_mode=ParseMode.HTML)
        return

    await progress.delete()
    await update.message.reply_html(
        text,
        reply_markup=build_buttons(video_id),
        disable_web_page_preview=False,
    )


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚀 觸發 daily_brief，請稍候...")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(BASE_DIR / "daily_brief.py"),
        cwd=str(BASE_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        tail = stdout.decode("utf-8", errors="replace")[-1000:]
        await update.message.reply_text(f"❌ daily_brief 失敗:\n{tail}")


# ─────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("❌ 缺少 TELEGRAM_BOT_TOKEN")
        sys.exit(1)

    app = Application.builder().token(token).build()
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("run", cmd_run))
    # 任何含 YouTube URL 的純文字訊息 → 即時摘要
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Regex(YOUTUBE_URL_RE),
            handle_youtube_url,
        )
    )

    logger.info("✅ bot_service 啟動")
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
