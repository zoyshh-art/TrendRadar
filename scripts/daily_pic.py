# coding=utf-8
"""每日美图推送

百度图片接口直爬真人写真（免 key），推送到 PushPlus 微信服务号（HTML 模板）。

- 图源：百度图片 acjson（baidu 自家缓存 CDN，https、无防盗链、国内秒开）
- 选取：只取竖图原图（高>宽、宽>=600），按日期随机（同一天重跑结果一致）
- 去重：output/pic_history.json 记录近期已推 URL，避免重复
- 推送：PushPlus channel=wechat（微信服务号），template=html，content 用 <img>

环境变量：
- PUSHPLUS_TOKEN   必填，PushPlus 用户 token

用法：
  PUSHPLUS_TOKEN=xxx python scripts/daily_pic.py [--dry-run]
"""

import json
import os
import random
import re
import sys
import urllib.parse
from datetime import date, datetime
from pathlib import Path

import requests

# ──────────────────────────── 配置 ────────────────────────────

NUM_IMAGES = 3                 # 每次推送张数
HISTORY_KEEP = 100             # 历史保留条数
HISTORY_PATH = Path(__file__).resolve().parent.parent / "output" / "pic_history.json"

# 每日关键词池（按日期轮换，百度图片内容受其审查，均为安全范围）
KEYWORDS = [
    "美女写真 摄影",
    "真人写真 模特",
    "人像摄影 少女写真",
    "日系写真 少女",
    "比基尼 写真 女生",
    "时尚写真 女模",
]

PUSH_URL = "https://www.pushplus.plus/send"

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://image.baidu.com/",
}
MOBILE_UA = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                  "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
}


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ──────────────────────── 百度图片搜索 ────────────────────────

def baidu_search(session: requests.Session, word: str, pn: int = 0, rn: int = 30) -> list[dict]:
    w = urllib.parse.quote(word)
    url = (
        "https://image.baidu.com/search/acjson"
        "?tn=resultjson_com&logid=1&ipn=rj&ct=201326592&fp=result"
        f"&word={w}&queryWord={w}"
        "&cl=2&lm=-1&ie=utf-8&oe=utf-8&st=-1&ic=0&face=0&istype=2&nc=1"
        f"&pn={pn}&rn={rn}&gsm=1e&channel=h5&cross_domain=1"
    )
    r = session.get(
        url,
        headers={"Referer": f"https://image.baidu.com/search/index?tn=baiduimage&word={w}"},
        timeout=20,
    )
    r.raise_for_status()
    try:
        data = json.loads(r.text.replace("\\'", "'"), strict=False).get("data") or []
        return [d for d in data if isinstance(d, dict) and d.get("thumbURL")]
    except (ValueError, AttributeError):
        return []


def verify_image(url: str, timeout: int = 12) -> bool:
    """模拟微信 <img> 加载：无 Referer 直连，确认是可渲染的图片"""
    try:
        r = requests.get(url, headers=MOBILE_UA, timeout=timeout, stream=True)
        ct = r.headers.get("Content-Type", "")
        first = next(r.iter_content(2048), b"")
        r.close()
        return r.status_code == 200 and "image" in ct and len(first) > 500
    except requests.RequestException:
        return False


def pick_images(session: requests.Session, rng: random.Random) -> list[dict]:
    """多关键词多页取池 → 过滤 → 随机选 N 张（逐一验证）"""
    day_idx = date.today().toordinal()
    words = KEYWORDS[day_idx % len(KEYWORDS):] + KEYWORDS[: day_idx % len(KEYWORDS)]
    pages = [0, 30, 60]
    pool: list[dict] = []
    seen: set[str] = set()

    for word in words[:3]:  # 每次用 3 个关键词，控制请求量
        for pn in pages:
            try:
                items = baidu_search(session, word, pn=pn)
            except requests.RequestException as e:
                log(f"  搜索失败 {word} pn={pn}: {type(e).__name__}")
                continue
            for it in items:
                u = it.get("thumbURL", "")
                w, h = it.get("width") or 0, it.get("height") or 0
                # 只要 https 竖图大图（真人写真多为竖构图）
                if not u.startswith("https://"):
                    continue
                if not (h > w >= 600):
                    continue
                if it.get("type") in ("gif", "webp"):
                    continue
                if u in seen:
                    continue
                seen.add(u)
                pool.append(it)
            if len(pool) >= 40:
                break
        if len(pool) >= 40:
            break

    log(f"候选池 {len(pool)} 张（{len(words[:3])} 关键词 × {len(pages)} 页）")
    if not pool:
        return []

    history = load_history()
    fresh = [it for it in pool if it["thumbURL"] not in history] or pool
    rng.shuffle(fresh)

    chosen: list[dict] = []
    for it in fresh:
        if len(chosen) >= NUM_IMAGES:
            break
        if verify_image(it["thumbURL"]):
            chosen.append(it)
            log(f"  选定 {it.get('width')}x{it.get('height')} {it['thumbURL'][:80]}")
    return chosen


# ──────────────────────── HTML 组装与推送 ────────────────────────

def clean_title(raw: str) -> str:
    t = re.sub(r"<.*?>", "", raw or "")
    t = t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
    t = re.sub(r"#\S+", "", t).strip()  # 去话题标签
    return t[:40] + ("…" if len(t) > 40 else "")


def build_html(images: list[dict]) -> str:
    today = date.today().isoformat()
    parts = [
        '<div style="font-family:-apple-system,Helvetica,Arial,sans-serif;color:#2b2b2b;line-height:1.6">',
        '<div style="text-align:center;border-bottom:2px solid #d33;background:none;padding-bottom:8px;">'
        f'<span style="font-size:18px;font-weight:bold;">📸 每日美图 · {today}</span></div>',
        '<div style="height:8px"></div>',
    ]
    for i, it in enumerate(images, 1):
        cap = clean_title(it.get("fromPageTitleEnc", ""))
        parts.append(
            f'<img src="{it["thumbURL"]}" '
            'style="width:100%;border-radius:6px;display:block;" />'
        )
        if cap:
            parts.append(
                f'<div style="text-align:center;color:#8a8a8a;font-size:13px;'
                f'margin:4px 0 14px 0;">{i}. {cap}</div>'
            )
        else:
            parts.append('<div style="height:14px"></div>')
    parts.append(
        '<div style="text-align:center;color:#b0b0b0;font-size:12px;border-top:1px dashed #ddd;'
        'padding-top:8px;">每日自动推送 · 图源百度图片 · 长按可保存</div>'
    )
    parts.append("</div>")
    return "".join(parts)


def push(token: str, title: str, content: str) -> None:
    r = requests.post(
        PUSH_URL,
        json={"token": token, "title": title, "content": content,
              "template": "html", "channel": "wechat"},
        timeout=30,
    )
    r.raise_for_status()
    body = r.json()
    if body.get("code") != 200:
        raise RuntimeError(f"PushPlus 返回失败: {body}")
    log(f"推送成功 messageid={body.get('data')}")


# ──────────────────────── 历史记录（去重） ────────────────────────

def load_history() -> set[str]:
    try:
        return set(json.loads(HISTORY_PATH.read_text(encoding="utf-8")).get("urls", []))
    except (OSError, ValueError):
        return set()


def save_history(urls: list[str]) -> None:
    old = sorted(load_history())
    merged = list(dict.fromkeys(old + urls))[-HISTORY_KEEP:]
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(
        json.dumps({"urls": merged, "updated": date.today().isoformat()},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


# ──────────────────────────── 主流程 ────────────────────────────

def main() -> int:
    dry = "--dry-run" in sys.argv
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()

    session = requests.Session()
    session.headers.update(UA)
    try:
        session.get("https://image.baidu.com/", timeout=15)  # 拿 BAIDUID cookie
    except requests.RequestException as e:
        log(f"cookie 预热失败（继续尝试）: {e}")

    # 同一天用固定种子：workflow 重跑结果一致，配合历史去重
    rng = random.Random(date.today().isoformat())
    images = pick_images(session, rng)
    if not images:
        log("没有可用图片，退出")
        return 1

    content = build_html(images)
    title = f"每日美图 · {date.today().isoformat()}"

    if dry:
        print("──── DRY RUN ────")
        print(title)
        print(content)
        return 0

    if not token:
        log("缺少 PUSHPLUS_TOKEN 环境变量")
        return 1
    push(token, title, content)
    save_history([it["thumbURL"] for it in images])
    log(f"历史已记录（{len(load_history())} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
