#!/usr/bin/env python3
"""
dropbox_auth.py — 一次性 OAuth 授權，拿 Dropbox refresh token

執行流程：
  1. 從 .env 讀 DROPBOX_APP_KEY / DROPBOX_APP_SECRET
  2. 印出授權連結，用戶在瀏覽器點同意
  3. 拿到一次性 code，貼回終端機
  4. 換成 refresh token，自動寫進 .env
"""

import os
import sys
import urllib.parse
import urllib.request
import json
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    print("❌ 請先 pip3 install python-dotenv")
    sys.exit(1)

ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(ENV_PATH, override=True)

APP_KEY = os.environ.get("DROPBOX_APP_KEY")
APP_SECRET = os.environ.get("DROPBOX_APP_SECRET")

if not APP_KEY or not APP_SECRET:
    print("❌ 找不到 DROPBOX_APP_KEY / DROPBOX_APP_SECRET")
    print("   請先把這兩個值加到 .env")
    sys.exit(1)

# ─────────────────────────────────────────────
# Step 1: 產生授權 URL
# ─────────────────────────────────────────────

auth_url = (
    "https://www.dropbox.com/oauth2/authorize?"
    + urllib.parse.urlencode({
        "client_id": APP_KEY,
        "response_type": "code",
        "token_access_type": "offline",  # ← 關鍵：拿 refresh token
    })
)

print("=" * 60)
print("Step 1: 開瀏覽器點下面這條 URL 授權")
print("=" * 60)
print()
print(auth_url)
print()
print("=" * 60)
print("Step 2: 同意後，網頁會給你一個 code（一串字），貼下面")
print("=" * 60)
code = input("\n請貼 code: ").strip()

if not code:
    print("❌ 沒有收到 code")
    sys.exit(1)

# ─────────────────────────────────────────────
# Step 2: 用 code 換 refresh token
# ─────────────────────────────────────────────

print("\n→ 正在跟 Dropbox 換 refresh token...")

data = urllib.parse.urlencode({
    "code": code,
    "grant_type": "authorization_code",
    "client_id": APP_KEY,
    "client_secret": APP_SECRET,
}).encode()

req = urllib.request.Request(
    "https://api.dropboxapi.com/oauth2/token",
    data=data,
    method="POST",
)
try:
    resp = urllib.request.urlopen(req, timeout=15)
    body = json.loads(resp.read())
except urllib.error.HTTPError as e:
    print(f"❌ Dropbox 回應錯誤: {e.read().decode()}")
    sys.exit(1)

refresh_token = body.get("refresh_token")
access_token = body.get("access_token")

if not refresh_token:
    print(f"❌ 沒拿到 refresh token: {body}")
    sys.exit(1)

print("✅ 拿到 refresh token")
print(f"   開頭：{refresh_token[:8]}...{refresh_token[-4:]}")
print(f"   長度：{len(refresh_token)} 字元")

# ─────────────────────────────────────────────
# Step 3: 寫進 .env
# ─────────────────────────────────────────────

env_content = ENV_PATH.read_text() if ENV_PATH.exists() else ""

# 移除舊的 DROPBOX_REFRESH_TOKEN（如果有）
lines = [l for l in env_content.splitlines() if not l.startswith("DROPBOX_REFRESH_TOKEN=")]
lines.append(f"DROPBOX_REFRESH_TOKEN={refresh_token}")

ENV_PATH.write_text("\n".join(lines) + "\n")
print(f"\n✅ 已寫入 {ENV_PATH}")
print("\n用以下指令測試 token 能用（可選）：")
print(f"""
python3 -c "
import dropbox, os
from dotenv import load_dotenv
load_dotenv('{ENV_PATH}', override=True)
dbx = dropbox.Dropbox(
    app_key=os.environ['DROPBOX_APP_KEY'],
    app_secret=os.environ['DROPBOX_APP_SECRET'],
    oauth2_refresh_token=os.environ['DROPBOX_REFRESH_TOKEN'],
)
print(dbx.users_get_current_account().email)
"
""")
