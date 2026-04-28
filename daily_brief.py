#!/usr/bin/env python3
"""
daily_brief.py — YouTube 訂閱清單每日摘要報告

每天從 channels.json 抓 RSS，找出新影片 → Claude 三層摘要 → Telegram 推送。

環境變數（放在 .env）:
    ANTHROPIC_API_KEY      - 必須（摘要用）
    TELEGRAM_BOT_TOKEN     - 必須
    TELEGRAM_CHAT_ID       - 必須

第一次執行會把所有現有影片標記為已看（不發信），之後只摘要新影片。
"""

import asyncio
import json
import os
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import anthropic
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env", override=True)
except ImportError:
    pass

from yt2epub import fetch_youtube_transcript, llm_call, LLM_PROVIDER


# ─────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
CHANNELS_FILE = BASE_DIR / "channels.json"
SEEN_FILE = BASE_DIR / "seen.json"
SUMMARIES_DIR = BASE_DIR / "summaries"
LOG_FILE = BASE_DIR / "daily_brief.log"

CLAUDE_MODEL = "claude-haiku-4-5"
MAX_VIDEOS_PER_RUN = 20  # 安全上限，避免一次燒太多 token
MAX_TRANSCRIPT_CHARS = 60000  # 截斷超長字幕（約 ~15k tokens）

# 推送過濾：哪些 relevance 等級值得進 Telegram 訊息流
# - high     ：直接相關，建議轉 epub
# - medium   ：部分相關
# - low      ：邊緣相關（預設過濾掉）
# - off-topic：完全無關（永遠過濾）
PUSH_RELEVANCE_THRESHOLD = {"high", "medium"}

# 沒字幕的影片直接跳過（無法轉 epub，看摘要意義不大）
# False 的話會用標題判斷相關性，相關的還是會推輕量通知
SKIP_NO_TRANSCRIPT = True

SUMMARY_SYSTEM_PROMPT = """你是一位專業的 podcast / 訪談摘要員，服務的對象是一位關注**科技、財經、投資、創業**的讀者。

你的任務是把英文長對談轉成繁體中文摘要，幫他決定**值不值得把整集轉成完整逐字稿 epub**。

回傳 JSON：

{
  "relevance": "high | medium | low | off-topic",
  "relevance_reason": "描述這集是什麼內容（25 字內）",
  "tags": ["tag1", "tag2", "tag3"],
  "summary": "150-200 字段落式摘要，把整集重點濃縮成一段話"
}

**規則：**

1. **relevance**（讀者關注科技/財經/投資/創業）：
   - `high`: 直接相關，建議轉 epub
   - `medium`: 部分相關
   - `low`: 邊緣相關
   - `off-topic`: 純娛樂/歷史/八卦

2. **relevance_reason**：
   - **描述這集的內容主題是什麼**，不是「對讀者多有用」「值不值得讀」
   - 25 字內，名詞片語為主
   - 好範例：「NVIDIA CEO 深度訪談，涵蓋 AI 硬體策略、創業決策、平台建設等核心科技商業主題」
   - 壞範例：「直接深入 NVIDIA CEO 的戰略思維，對創業者極具參考價值」（這是在賣，不是在描述）

3. **tags**：3-5 個。
   - **單一概念，不要用 `/` 或 `+` 串接**（用 `#AI` 不要用 `#AI/GPU`）
   - **短**，盡量 2-4 個字（用 `#創業` 不要用 `#創業策略`、用 `#硬體` 不要用 `#硬體策略`）
   - 好範例：`["AI", "創業", "硬體", "平台策略", "NVIDIA"]`
   - 壞範例：`["AI/GPU", "創業策略", "公司架構", "產品市場策略"]`
   - off-topic 也要標領域（如 `["歷史"]`）

4. **summary**（150-200 字段落式）：
   - **段落形式**，不要條列、不要 bullet
   - **嚴格控制 150-200 字**，超過要重寫精簡
   - **敘事結構**：開場立論 → 2-3 個具體案例/論點 → 收尾觀察 / 反思
   - 包含具體論點、人名、數據（不要寫「他們討論了 X」這種空話）
   - 所有事實（人名、公司、數據、年份、因果關係）**必須能在逐字稿中找到對應**，不要腦補
   - 好範例（敘事流暢、具體、有結構）：
     「Jensen Huang 在這集講述 NVIDIA 如何從 1997 年瀕臨破產的 3D 圖形晶片公司，演進成今日價值 1.1 兆美元的 AI 運算巨擘。核心故事包括：ReVo 128 的豪賭決策（用模擬而非實體原型，一次性流片成功）、對 DirectX 生態的適應、發現 CUDA 在 AI 時代的應用潛力、以及預見數據中心與高效能網路（Mellanox 收購）將成為未來核心。Huang 強調企業應像運算棧一樣組織，以任務（而非職級）驅動跨部門協作；競爭策略則是提前十年進入「零美元市場」並建立開發者生態。最後反思運氣與技能的平衡：再聰慧的決策也可能失敗，但當機會來臨時，預先定位才能把握。」
   - 壞範例（太長、像列項、推銷感）：
     「Jensen Huang 在這集分享 NVIDIA 從瀕臨破產到全球最有價值公司之一的關鍵決策邏輯。1997 年 GeForce 128 危機中，他用模擬軟體預先測試...（中略 250+ 字）...再持續十年領先直到市場顯現。」（太長、案例堆疊、缺收尾觀察）

5. 人名、公司、產品、金融術語（IPO、TAM、ARPU、LP、GP）保留英文，第一次出現可加中文括號。

6. 只回傳 JSON，不要前言、說明、markdown 代碼框。"""


# ─────────────────────────────────────────────
# RSS / 訂閱
# ─────────────────────────────────────────────

def fetch_rss(channel: dict) -> list[dict]:
    """從 YouTube RSS 抓取最新影片清單。"""
    kind = channel["type"]
    cid = channel["id"]
    if kind == "playlist":
        url = f"https://www.youtube.com/feeds/videos.xml?playlist_id={cid}"
    else:
        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    xml = urllib.request.urlopen(req, timeout=15).read()

    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
        "media": "http://search.yahoo.com/mrss/",
    }

    root = ET.fromstring(xml)
    videos = []
    for entry in root.findall("atom:entry", ns):
        vid = entry.find("yt:videoId", ns)
        title = entry.find("atom:title", ns)
        published = entry.find("atom:published", ns)
        link = entry.find("atom:link", ns)
        if vid is None or title is None:
            continue
        videos.append({
            "id": vid.text,
            "title": title.text,
            "published": published.text if published is not None else "",
            "url": link.get("href") if link is not None else f"https://www.youtube.com/watch?v={vid.text}",
        })
    return videos


def is_short(video_id: str) -> bool:
    """偵測 YouTube Short：請求 /shorts/<id>，若被 redirect 到 /watch 就不是 short。

    無法判定（網路錯誤、HEAD 不支援等）時回傳 False，避免誤過濾正常影片。
    """
    url = f"https://www.youtube.com/shorts/{video_id}"
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0"}, method="HEAD"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            final = r.geturl()
            return "/shorts/" in final
    except Exception:
        return False


def load_seen() -> dict:
    if not SEEN_FILE.exists():
        return {}
    with open(SEEN_FILE) as f:
        return json.load(f)


def save_seen(seen: dict):
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f, indent=2, ensure_ascii=False)


# ─────────────────────────────────────────────
# 摘要（Claude）
# ─────────────────────────────────────────────

TITLE_RELEVANCE_PROMPT = """你是一位內容過濾助手，幫一位關注**科技、財經、投資、創業**的讀者判斷影片是否值得他關注。

只看到標題與頻道名（沒有字幕內容），請給判斷：

回傳 JSON：
{
  "relevance": "high | medium | low | off-topic",
  "relevance_reason": "30 字內理由（中文）",
  "tags": ["3-5 個 tag"]
}

評分標準：
- high：明確是科技/財經/投資/創業深度訪談（例：Acquired、Invest Like the Best、Lex Fridman 訪談類）
- medium：可能相關但不確定（例：頻道剛好碰到主題，但格式不明）
- low：邊緣相關
- off-topic：純娛樂、運動、生活 vlog 等

只回傳 JSON，不要前言。"""


def check_title_relevance(video: dict, channel_name: str) -> dict:
    """無字幕時用：只用標題 + 頻道名做輕量相關性判斷。"""
    prompt = f"頻道：{channel_name}\n影片標題：{video['title']}"
    try:
        raw = llm_call(TITLE_RELEVANCE_PROMPT, prompt, max_tokens=400, thinking_budget=512)
        text = raw.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            text = text[start:end + 1]
        return json.loads(text)
    except Exception as e:
        return {
            "relevance": "unknown",
            "relevance_reason": f"標題判斷失敗：{e}",
            "tags": [],
        }


def summarize_video(video: dict, channel_name: str, _client=None) -> dict:
    """抓字幕 + 摘要（依 LLM_PROVIDER 走 Gemini 或 Claude）。

    _client 參數保留以維持舊呼叫端兼容，內部不再使用。
    """
    print(f"   📝 摘要中（{LLM_PROVIDER}）: {video['title'][:60]}...")

    segments, _ = fetch_youtube_transcript(video["url"])
    if not segments:
        # 無字幕：依 SKIP_NO_TRANSCRIPT 決定要不要還做相關性判斷
        if SKIP_NO_TRANSCRIPT:
            print(f"     ⚠️ 無字幕，跳過（不做標題判斷以省 token）")
            return {
                "no_transcript": True,
                "relevance": "skipped",
                "relevance_reason": "無字幕",
                "tags": [],
                "summary": "",
                "narrative_blocks": [],
            }
        print(f"     ⚠️ 無字幕，改用標題做相關性判斷")
        title_check = check_title_relevance(video, channel_name)
        return {
            "no_transcript": True,
            "relevance": title_check.get("relevance", "unknown"),
            "relevance_reason": title_check.get("relevance_reason", ""),
            "tags": title_check.get("tags", []),
            "summary": "",
            "narrative_blocks": [],
        }

    formatted = "\n".join(f"[{s['timestamp']}] {s['en']}" for s in segments)
    if len(formatted) > MAX_TRANSCRIPT_CHARS:
        formatted = formatted[:MAX_TRANSCRIPT_CHARS] + "\n[...逐字稿過長已截斷...]"

    prompt = (
        f"頻道：{channel_name}\n"
        f"影片標題：{video['title']}\n\n"
        f"以下是英文逐字稿（含時間戳），請按指示產生摘要：\n\n{formatted}"
    )

    raw = llm_call(
        SUMMARY_SYSTEM_PROMPT, prompt,
        max_tokens=4096,
        thinking_budget=2048,
    )
    text = raw.strip()
    # 去除 markdown 代碼框
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    # 抓出第一個 { 到最後一個 }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        text = text[start:end + 1]

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        debug_path = SUMMARIES_DIR / f"_raw_{video['id']}.txt"
        SUMMARIES_DIR.mkdir(exist_ok=True)
        debug_path.write_text(raw)
        return {
            "no_transcript": False,
            "relevance": "unknown",
            "relevance_reason": f"JSON 解析失敗: {e}（原始回應已存到 {debug_path.name}）",
            "tags": [],
            "summary": "",
        }


# ─────────────────────────────────────────────
# Telegram 推送
# ─────────────────────────────────────────────

RELEVANCE_BADGE = {
    "high": "⭐⭐⭐",
    "medium": "⭐⭐",
    "low": "⭐",
    "off-topic": "🔘",
    "unknown": "❔",
}


def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_video_message(item: dict) -> str:
    """組成單一影片的 Telegram 訊息（HTML 格式）。"""
    v = item["video"]
    ch = item["channel"]
    s = item["summary"]

    title = html_escape(v["title"])
    ch_safe = html_escape(ch)

    if s.get("no_transcript"):
        # 沒字幕 → 輕量通知（標題判斷有相關性才會走到這）
        badge = RELEVANCE_BADGE.get(s.get("relevance", "unknown"), "❔")
        reason = html_escape(s.get("relevance_reason", ""))
        tags = "  ".join(f"#{html_escape(t).replace(' ', '_')}" for t in s.get("tags", []))
        parts = [
            f"📺 <b>{ch_safe}</b>",
            f'📰 <a href="{v["url"]}">{title}</a>',
            "",
            f"{badge} <i>{reason}</i>",
        ]
        if tags:
            parts.append(tags)
        parts.append("")
        parts.append("⚠️ <b>無字幕</b>，無法摘要 / 轉 epub。標題看起來相關，自行決定是否點 YouTube 看。")
        return "\n".join(parts)

    badge = RELEVANCE_BADGE.get(s.get("relevance", "unknown"), "❔")
    relevance_reason = html_escape(s.get("relevance_reason", ""))
    tags = "  ".join(f"#{html_escape(t).replace(' ', '_')}" for t in s.get("tags", []))

    parts = [
        f"📺 <b>{ch_safe}</b>",
        f'📰 <a href="{v["url"]}">{title}</a>',
        "",
        f"{badge} <i>{relevance_reason}</i>",
    ]
    if tags:
        parts.append(tags)

    summary = s.get("summary", "")
    if summary:
        parts.append("")
        parts.append(html_escape(summary))

    return "\n".join(parts)


def build_buttons(video_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎧 轉成 epub → Kobo", callback_data=f"convert:{video_id}")],
    ])


def save_summary(item: dict):
    """把摘要存檔，bot_service 之後讀檔回應按鈕。"""
    SUMMARIES_DIR.mkdir(exist_ok=True)
    vid = item["video"]["id"]
    out = SUMMARIES_DIR / f"{vid}.json"
    payload = {
        "video": item["video"],
        "channel": item["channel"],
        "summary": item["summary"],
        "saved_at": datetime.now().isoformat(),
    }
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False))


async def send_to_telegram(items: list[dict], date: str):
    """推送到 Telegram。每部影片一則訊息 + 按鈕。"""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not all([token, chat_id]):
        print("❌ 缺少 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        sys.exit(1)

    app = Application.builder().token(token).build()
    bot = app.bot

    # 開頭訊息
    await bot.send_message(
        chat_id=chat_id,
        text=f"📡 <b>Daily Brief — {date}</b>\n今天有 {len(items)} 部新影片",
        parse_mode=ParseMode.HTML,
    )

    for item in items:
        text = build_video_message(item)
        if item["summary"].get("no_transcript"):
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=build_buttons(item["video"]["id"]),
                disable_web_page_preview=False,
            )

    print(f"📨 已推送 {len(items)} 則訊息到 Telegram")


# ─────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────

def log(msg: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def main():
    is_init = "--init" in sys.argv
    skip_email = "--no-email" in sys.argv or is_init

    log("=" * 60)
    log(f"daily_brief 開始 (init={is_init}, no-email={skip_email})")

    if not CHANNELS_FILE.exists():
        log("❌ 找不到 channels.json")
        sys.exit(1)

    with open(CHANNELS_FILE) as f:
        channels = json.load(f)

    seen = load_seen()
    new_videos = []

    for ch in channels:
        ch_key = f"{ch['type']}:{ch['id']}"
        try:
            videos = fetch_rss(ch)
            ch_seen = set(seen.get(ch_key, []))
            log(f"  📡 {ch['name']}: RSS 有 {len(videos)} 部，已看 {len(ch_seen)}")

            shorts_skipped = 0
            for v in videos:
                if v["id"] in ch_seen:
                    continue
                if is_short(v["id"]):
                    shorts_skipped += 1
                    ch_seen.add(v["id"])  # 標記為已看，下次不再檢查
                    continue
                new_videos.append({"channel": ch["name"], "video": v, "ch_key": ch_key})
                ch_seen.add(v["id"])

            if shorts_skipped:
                log(f"     略過 {shorts_skipped} 部 Shorts")
            seen[ch_key] = list(ch_seen)
        except Exception as e:
            log(f"  ⚠️  {ch['name']} 失敗: {e}")

    log(f"找到 {len(new_videos)} 部新影片")

    # 第一次跑：只標記不摘要
    if is_init:
        save_seen(seen)
        log("✅ 初始化完成，所有現有影片已標記為已看")
        return

    # 沒新影片就不寄信
    if not new_videos:
        log("📭 沒有新影片，不寄信")
        save_seen(seen)
        return

    # 安全上限
    if len(new_videos) > MAX_VIDEOS_PER_RUN:
        log(f"⚠️  超過 {MAX_VIDEOS_PER_RUN} 部，只處理最新 {MAX_VIDEOS_PER_RUN} 部")
        new_videos = new_videos[:MAX_VIDEOS_PER_RUN]

    summaries = []
    skipped_no_transcript = 0
    skipped_off_topic = 0

    for i, item in enumerate(new_videos, 1):
        log(f"  [{i}/{len(new_videos)}] {item['channel']}: {item['video']['title'][:60]}")
        try:
            summary = summarize_video(item["video"], item["channel"])
        except Exception as e:
            log(f"  ⚠️  摘要失敗: {e}")
            summary = {
                "no_transcript": False,
                "relevance": "unknown",
                "relevance_reason": f"摘要失敗：{e}",
                "tags": [],
                "summary": "",
            }

        rec = {
            "channel": item["channel"],
            "video": item["video"],
            "summary": summary,
        }
        save_summary(rec)  # 永遠存檔

        # 過濾規則：
        # 1) SKIP_NO_TRANSCRIPT=True 時，沒字幕一律跳過
        # 2) 否則用 relevance 過濾（low/off-topic 跳過）
        if SKIP_NO_TRANSCRIPT and summary.get("no_transcript"):
            log(f"     ⏩ 跳過（無字幕）")
            skipped_no_transcript += 1
            continue

        relevance = summary.get("relevance", "unknown")
        if relevance not in PUSH_RELEVANCE_THRESHOLD:
            no_t = summary.get("no_transcript", False)
            log(f"     ⏩ 跳過（{'無字幕+' if no_t else ''}relevance={relevance}）")
            if no_t:
                skipped_no_transcript += 1
            else:
                skipped_off_topic += 1
            continue

        summaries.append(rec)

    save_seen(seen)

    date = datetime.now().strftime("%Y-%m-%d")

    if skipped_no_transcript or skipped_off_topic:
        log(f"📊 過濾統計：無字幕 {skipped_no_transcript} 部、低相關 {skipped_off_topic} 部")

    if not summaries:
        log("📭 沒有符合推送條件的影片（全被過濾），不推送")
    elif skip_email:
        log(f"⏩ 跳過推送（--no-email）— 摘要已存到 {SUMMARIES_DIR}")
    else:
        asyncio.run(send_to_telegram(summaries, date))

    log(f"✅ 完成（推送 {len(summaries)} / 共 {len(new_videos)} 部新影片）")


if __name__ == "__main__":
    main()
