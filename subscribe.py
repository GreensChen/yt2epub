#!/usr/bin/env python3
"""
subscribe.py — 管理 channels.json 訂閱清單

用法:
    python subscribe.py list                              # 列出全部訂閱
    python subscribe.py add <URL> [--name "頻道名"]       # 新增（自動標記現有為已看）
    python subscribe.py remove <name-or-id>               # 移除
"""

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path
from xml.etree import ElementTree as ET

BASE_DIR = Path(__file__).parent
CHANNELS_FILE = BASE_DIR / "channels.json"
SEEN_FILE = BASE_DIR / "seen.json"


def load_channels() -> list[dict]:
    if not CHANNELS_FILE.exists():
        return []
    with open(CHANNELS_FILE) as f:
        return json.load(f)


def save_channels(channels: list[dict]):
    with open(CHANNELS_FILE, "w") as f:
        json.dump(channels, f, indent=2, ensure_ascii=False)


def load_seen() -> dict:
    if not SEEN_FILE.exists():
        return {}
    with open(SEEN_FILE) as f:
        return json.load(f)


def save_seen(seen: dict):
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f, indent=2, ensure_ascii=False)


def resolve_url(url: str) -> tuple[str, str, str]:
    """解析 URL → (type, id, name)。"""
    if "playlist?list=" in url:
        pid = re.search(r"list=([^&]+)", url).group(1)
        rss = f"https://www.youtube.com/feeds/videos.xml?playlist_id={pid}"
        title = fetch_title(rss)
        return "playlist", pid, title

    handle_match = re.search(r"@([\w_-]+)", url)
    if handle_match:
        handle = handle_match.group(1)
        req = urllib.request.Request(
            f"https://www.youtube.com/@{handle}/videos",
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
        )
        html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", errors="ignore")
        for pat in [r'"channelId":"(UC[\w-]+)"', r'"externalId":"(UC[\w-]+)"', r'channel/(UC[\w-]+)']:
            m = re.search(pat, html)
            if m:
                cid = m.group(1)
                rss = f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
                title = fetch_title(rss)
                return "channel", cid, title
        raise ValueError(f"無法解析 channel handle: @{handle}")

    cid_match = re.search(r"channel/(UC[\w-]+)", url)
    if cid_match:
        cid = cid_match.group(1)
        rss = f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
        return "channel", cid, fetch_title(rss)

    raise ValueError(f"看不懂的 URL: {url}")


def fetch_title(rss_url: str) -> str:
    req = urllib.request.Request(rss_url, headers={"User-Agent": "Mozilla/5.0"})
    xml = urllib.request.urlopen(req, timeout=10).read()
    root = ET.fromstring(xml)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    title = root.find("atom:title", ns)
    return title.text if title is not None else "?"


def fetch_video_ids(channel: dict) -> list[str]:
    kind = channel["type"]
    cid = channel["id"]
    url = f"https://www.youtube.com/feeds/videos.xml?{'playlist_id' if kind=='playlist' else 'channel_id'}={cid}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    xml = urllib.request.urlopen(req, timeout=15).read()
    ns = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    root = ET.fromstring(xml)
    return [e.find("yt:videoId", ns).text for e in root.findall("atom:entry", ns) if e.find("yt:videoId", ns) is not None]


def cmd_list():
    channels = load_channels()
    seen = load_seen()
    if not channels:
        print("（清單為空）")
        return
    print(f"共 {len(channels)} 個訂閱：\n")
    for i, ch in enumerate(channels, 1):
        ch_key = f"{ch['type']}:{ch['id']}"
        seen_count = len(seen.get(ch_key, []))
        print(f"  {i:2d}. [{ch['type']:8s}] {ch['name']}")
        print(f"      id={ch['id']}  已看={seen_count}")


def cmd_add(url: str, custom_name: str = None):
    kind, cid, fetched_name = resolve_url(url)
    name = custom_name or fetched_name

    channels = load_channels()
    if any(c["type"] == kind and c["id"] == cid for c in channels):
        print(f"⚠️  已存在: [{kind}] {name}")
        return

    new = {"type": kind, "id": cid, "name": name}
    channels.append(new)
    save_channels(channels)
    print(f"✅ 新增: [{kind}] {name} ({cid})")

    # 把現有影片標記為已看
    print("   標記現有影片為已看（不會出現在第一份報告）...")
    try:
        ids = fetch_video_ids(new)
        seen = load_seen()
        ch_key = f"{kind}:{cid}"
        seen[ch_key] = ids
        save_seen(seen)
        print(f"   ✅ 標記了 {len(ids)} 部既有影片")
    except Exception as e:
        print(f"   ⚠️  標記失敗（之後會自動補）: {e}")


def cmd_remove(target: str):
    channels = load_channels()
    seen = load_seen()
    for i, ch in enumerate(channels):
        if ch["id"] == target or ch["name"] == target:
            channels.pop(i)
            save_channels(channels)
            seen.pop(f"{ch['type']}:{ch['id']}", None)
            save_seen(seen)
            print(f"✅ 已移除: {ch['name']}")
            return
    print(f"❌ 找不到: {target}")


def main():
    parser = argparse.ArgumentParser(description="管理 yt2epub 訂閱清單")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出全部訂閱")

    add = sub.add_parser("add", help="新增訂閱")
    add.add_argument("url", help="YouTube 頻道 / playlist URL")
    add.add_argument("--name", help="自訂顯示名稱")

    rm = sub.add_parser("remove", help="移除訂閱")
    rm.add_argument("target", help="要移除的 name 或 id")

    args = parser.parse_args()

    if args.cmd == "list":
        cmd_list()
    elif args.cmd == "add":
        cmd_add(args.url, args.name)
    elif args.cmd == "remove":
        cmd_remove(args.target)


if __name__ == "__main__":
    main()
