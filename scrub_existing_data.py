#!/usr/bin/env python3
"""
scrub_existing_data.py — 清掉舊 _data.json 裡殘留的 [音樂] / [笑聲] / >> 噪音

跑一次：
    python3 scrub_existing_data.py

之後對每個 _data.json 跑 yt2epub --from-json 重生 epub。
"""

import json
import re
import subprocess
import sys
from pathlib import Path

# 從 yt2epub 借相同的清理函式
sys.path.insert(0, str(Path(__file__).parent))
from yt2epub import clean_caption_noise, clean_zh_fillers, OUTPUT_DIR


# 中文翻譯後也可能殘留的標記
ZH_NOISE_PATTERN = re.compile(
    r"\[\s*("
    r"音[樂楽]|笑聲|笑声|掌聲|掌声|歡呼|背景音[樂楽]|"
    r"沉默|聲音效果|交談聲|聽不清楚|不清楚|"
    r"music|laughter|laughs?|applause|cheers?|inaudible|silence"
    r")\s*\]",
    flags=re.IGNORECASE,
)


def scrub_text(text: str) -> str:
    if not text:
        return text
    # 拿掉 >> 切換符號
    text = re.sub(r">>+\s*", " ", text)
    # 拿掉中英文版的 [Music] 等標記
    text = ZH_NOISE_PATTERN.sub(" ", text)
    text = clean_caption_noise(text)  # 英文版也再走一遍
    # 收斂多空白
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def scrub_data_file(path: Path) -> int:
    """清一個 _data.json，回傳被改的 segment 數量。"""
    data = json.loads(path.read_text())
    changed = 0
    for seg in data.get("segments", []):
        for k in ("en", "zh"):
            if k in seg:
                old = seg[k]
                new = scrub_text(old)
                if k == "zh":
                    new = clean_zh_fillers(new)
                if old != new:
                    seg[k] = new
                    changed += 1
    if changed:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return changed


def main():
    data_files = sorted(OUTPUT_DIR.glob("*_data.json"))
    if not data_files:
        print(f"❌ {OUTPUT_DIR} 找不到任何 _data.json")
        sys.exit(1)

    print(f"找到 {len(data_files)} 個 data.json，開始清理...")
    print()

    for data_file in data_files:
        changed = scrub_data_file(data_file)
        marker = "✏️" if changed else "✓"
        print(f"  {marker} {data_file.name}  (改了 {changed} 段)")

    print()
    print("清理完成。下一步：對每個 data.json 跑 yt2epub --from-json 重生 epub")
    print()

    # 自動重生 epub + 推到 Dropbox/Kobo
    for data_file in data_files:
        print(f"→ 重生 {data_file.stem.removesuffix('_data')}.epub ...")
        subprocess.run(
            [sys.executable, "-u", str(Path(__file__).parent / "yt2epub.py"),
             "--from-json", str(data_file)],
            check=False,
        )

    print()
    print("✅ 全部完成！epub 已重生並同步到 Dropbox/Kobo")


if __name__ == "__main__":
    main()
