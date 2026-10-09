# coding=utf-8
"""每日美图推送

双图源轮换，真人写真 → PushPlus 微信服务号（HTML 模板）推送。

图源（均免 key）：
1. Pexels 站内接口 —— 国际模特/写真摄影，无水印高质量
   images.pexels.com 图床国内可达、无防盗链
2. 百度图片 acjson —— 中文写真内容，baidu 自家缓存 CDN 国内秒开

轮换：按日期交替主图源，主源不够时用次源补满。

- 选取：只取竖图（高>宽），按日期固定随机种子（同日重跑结果一致）
- 去重：output/pic_history.json 记录近期已推 URL
- 推送：PushPlus channel=wechat，template=html，content 用 <img>

环境变量：
- PUSHPLUS_TOKEN   必填，PushPlus 用户 token

用法：
  PUSHPLUS_TOKEN=xxx python scripts/daily_pic.py [--dry-run]
"""

import html as htmllib
import json
import os
import random
import re
import sys
import time
import urllib.parse
from datetime import date, datetime
from pathlib import Path

import requests

# ──────────────────────────── 配置 ────────────────────────────

NUM_IMAGES = 3                 # 每次推送张数
HISTORY_KEEP = 100             # 历史保留条数
HISTORY_PATH = Path(__file__).resolve().parent.parent / "output" / "pic_history.json"

# Pexels 站内 Web API 的公开 Secret-Key（官网前端硬编码，非个人账号 key）
PEXELS_KEY = "H2jk9uKnhRmL6WPwh89zBezWvr"
PEXELS_QUERIES = [
    "glamour model photoshoot",
    "fashion model portrait",
    "beauty portrait studio",
    "asian model photoshoot",
    "summer fashion model",
]

# 必应图片关键词池
BING_QUERIES = [
    "美女写真 摄影",
    "性感写真 模特",
    "人像写真 少女",
    "比基尼 写真 女生",
]

# 百度图片关键词池（按日期轮换）
BAIDU_KEYWORDS = [
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
PEXELS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://www.pexels.com/",
    "Secret-Key": PEXELS_KEY,
    "Accept-Language": "en-US,en;q=0.9",
}
MOBILE_UA = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                  "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
}


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ──────────────────────── 图源一：Pexels ────────────────────────

def pexels_search(query: str, page: int = 1, per_page: int = 15) -> list[dict]:
    r = requests.get(
        "https://www.pexels.com/en-us/api/v3/search/photos",
        params={"query": query, "page": page, "per_page": per_page,
                "orientation": "portrait"},
        headers=PEXELS_HEADERS,
        timeout=20,
    )
    r.raise_for_status()
    out = []
    for it in r.json().get("data") or []:
        if not isinstance(it, dict):
            continue
        a = it.get("attributes") or {}
        img = a.get("image")
        url = None
        if isinstance(img, dict):
            url = img.get("large") or img.get("medium") or img.get("small")
        elif isinstance(img, str):
            url = img
        if not url or not url.startswith("https://images.pexels.com/"):
            continue
        w, h = a.get("width") or 0, a.get("height") or 0
        if w and h and not h > w:      # 只要竖图
            continue
        out.append({
            "thumbURL": url,
            "width": w, "height": h,
            "fromPageTitleEnc": (a.get("alt") or query).strip(),
            "source": "Pexels",
        })
    return out


def pool_pexels(rng: random.Random, need: int) -> list[dict]:
    queries = PEXELS_QUERIES[:]
    rng.shuffle(queries)
    pool, seen = [], set()
    for q in queries[:3]:
        for page in (1, 2):
            try:
                items = pexels_search(q, page=page)
            except requests.RequestException as e:
                log(f"  Pexels 搜索失败 {q}: {type(e).__name__}")
                continue
            for it in items:
                if it["thumbURL"] in seen:
                    continue
                seen.add(it["thumbURL"])
                pool.append(it)
            if len(pool) >= need * 4:
                break
        if len(pool) >= need * 4:
            break
    log(f"[Pexels] 候选 {len(pool)} 张")
    return pool


# ──────────────────────── 图源二：百度图片 ────────────────────────

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


def pool_baidu(session: requests.Session, rng: random.Random, need: int) -> list[dict]:
    day_idx = date.today().toordinal()
    rot = day_idx % len(BAIDU_KEYWORDS)
    words = BAIDU_KEYWORDS[rot:] + BAIDU_KEYWORDS[:rot]
    pool, seen = [], set()
    for word in words[:3]:
        for pn in (0, 30, 60):
            try:
                items = baidu_search(session, word, pn=pn)
            except requests.RequestException as e:
                log(f"  百度搜索失败 {word} pn={pn}: {type(e).__name__}")
                continue
            for it in items:
                u = it.get("thumbURL", "")
                w, h = it.get("width") or 0, it.get("height") or 0
                if not u.startswith("https://"):
                    continue
                if not (h > w >= 600):       # 只要竖图大图
                    continue
                if it.get("type") in ("gif", "webp"):
                    continue
                if u in seen:
                    continue
                seen.add(u)
                it["source"] = "百度图片"
                pool.append(it)
            if len(pool) >= need * 4:
                break
        if len(pool) >= need * 4:
            break
    log(f"[百度图片] 候选 {len(pool)} 张")
    return pool


# ──────────────────────── 图源三：必应图片 ────────────────────────

def bing_fetch(session: requests.Session, query: str) -> list[dict]:
    """拉取必应图片搜索结果，解析 iusc.m 属性拿自家缓存缩略图 turl"""
    w = urllib.parse.quote(query)
    r = session.get(
        "https://www.bing.com/images/async",
        params={"q": query, "first": "0", "count": "35", "relp": "35",
                "mmasync": "1", "scenario": "ImageBasicHover",
                "datsrc": "N_I", "layout": "RowBased"},
        headers={"Referer": f"https://www.bing.com/images/search?q={w}"},
        timeout=20,
    )
    if r.status_code != 200 or len(r.text) < 5000:
        raise requests.RequestException(f"bing 返回异常 status={r.status_code} len={len(r.text)}")
    out = []
    for ma in re.findall(r'm="(\{[^"]+\})"', r.text):
        try:
            j = json.loads(htmllib.unescape(ma))
        except ValueError:
            continue
        turl = j.get("turl") or ""
        mw, mh = j.get("mw") or 0, j.get("mh") or 0
        if not turl.startswith("https://"):
            continue
        if mw and mh and mh <= mw:        # 有尺寸信息时先过滤非竖图
            continue
        out.append({"thumbURL": turl, "width": mw, "height": mh,
                    "fromPageTitleEnc": j.get("t") or query,
                    "source": "必应图片",
                    "_aspect": (mh / mw) if (mw and mh) else 0})
    return out


def pool_bing(session: requests.Session, rng: random.Random, need: int) -> list[dict]:
    queries = BING_QUERIES[:]
    rng.shuffle(queries)
    pool, seen = [], set()
    for q in queries[:3]:
        for attempt in range(3):
            try:
                items = bing_fetch(session, q)
                break
            except requests.RequestException as e:
                log(f"  必应搜索失败 {q} 第{attempt + 1}次: {e}")
                items = []
                time.sleep(2 + attempt * 3)
        for it in items:
            if it["thumbURL"] in seen:
                continue
            aspect = it.pop("_aspect", 0)
            if aspect:
                # 必应缓存图按原始比例放大（turl 本身是小图）
                it["thumbURL"] += ("&" if "?" in it["thumbURL"] else "?") + \
                    f"w=768&h={round(768 * aspect)}&rs=1&pid=ImgDetMain"
            seen.add(it["thumbURL"])
            pool.append(it)
        if len(pool) >= need * 4:
            break
    log(f"[必应图片] 候选 {len(pool)} 张")
    return pool


# ──────────────────────── 选图与验证 ────────────────────────

def _image_size(data: bytes) -> tuple | None:
    """解析 JPEG/PNG 头拿尺寸（免依赖）"""
    if len(data) < 24:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:2] == b"\xff\xd8":  # JPEG：扫 SOF 段
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seglen = int.from_bytes(data[i + 2:i + 4], "big")
            if seglen < 2:
                return None
            # SOF0-SOF15 中去掉 DHT/JPG/DAC
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h = int.from_bytes(data[i + 5:i + 7], "big")
                w = int.from_bytes(data[i + 7:i + 9], "big")
                return w, h
            i += 2 + seglen
    return None


def verify_image(url: str, timeout: int = 20) -> tuple[bool, tuple | None]:
    """模拟微信 <img> 加载：下载整图确认可用，并解析尺寸"""
    try:
        r = requests.get(url, headers=MOBILE_UA, timeout=timeout)
        ct = r.headers.get("Content-Type", "")
        if r.status_code != 200 or "image" not in ct or len(r.content) < 1000:
            return False, None
        return True, _image_size(r.content)
    except requests.RequestException:
        return False, None


def pick_images(session: requests.Session, rng: random.Random) -> list[dict]:
    """按日轮换主图源，主源不足时次源补满，逐一验证"""
    day_idx = date.today().toordinal()
    builders_all = [
        ("Pexels", lambda: pool_pexels(rng, NUM_IMAGES)),
        ("百度", lambda: pool_baidu(session, rng, NUM_IMAGES)),
        ("必应", lambda: pool_bing(session, rng, NUM_IMAGES)),
    ]
    rot = day_idx % len(builders_all)
    builders = builders_all[rot:] + builders_all[:rot]
    log(f"今日主图源: {builders[0][0]}")

    history = load_history()
    chosen: list[dict] = []
    for _, build in builders:
        if len(chosen) >= NUM_IMAGES:
            break
        pool = build()
        fresh = [it for it in pool if it["thumbURL"] not in history]
        # 已推过的不重复用（除非该源实在没有新图）
        fresh = fresh or pool
        rng.shuffle(fresh)
        for it in fresh:
            if len(chosen) >= NUM_IMAGES:
                break
            if any(c["thumbURL"] == it["thumbURL"] for c in chosen):
                continue
            ok, dims = verify_image(it["thumbURL"])
            if not ok:
                continue
            if it.get("source") == "必应图片" and not it.get("width"):
                # 必应无尺寸元数据：实测尺寸把关竖图，并换成放大版 URL
                if dims is None or dims[1] <= dims[0]:
                    continue
                it["width"], it["height"] = dims
                if dims[0] < 768:
                    base = it["thumbURL"]
                    big = base + ("&" if "?" in base else "?") + \
                        f"w=768&h={round(768 * dims[1] / dims[0])}&rs=1&pid=ImgDetMain"
                    ok2, dims2 = verify_image(big)
                    if ok2:
                        it["thumbURL"] = big
                        if dims2 and dims2[1] > dims2[0]:
                            it["width"], it["height"] = dims2
            chosen.append(it)
            log(f"  选定 [{it.get('source')}] {it.get('width')}x{it.get('height')} "
                f"{it['thumbURL'][:75]}")
    return chosen


# ──────────────────────── HTML 组装与推送 ────────────────────────

def clean_title(raw: str) -> str:
    t = re.sub(r"<.*?>", "", raw or "")
    t = t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
    t = re.sub(r"#\S+", "", t).strip()  # 去话题标签
    # 截掉网站后缀：标题_网站 / 标题 | 栏目 / 标题-网站
    for sep in ("_", "|"):
        if sep in t:
            t = t.split(sep)[0].strip()
    if len(t) > 40:
        t = t[:40] + "…"
    return t


def build_html(images: list[dict]) -> str:
    today = date.today().isoformat()
    parts = [
        '<div style="font-family:-apple-system,Helvetica,Arial,sans-serif;color:#2b2b2b;line-height:1.6">',
        '<div style="text-align:center;border-bottom:2px solid #d33;padding-bottom:8px;">'
        f'<span style="font-size:18px;font-weight:bold;">📸 每日美图 · {today}</span></div>',
        '<div style="height:8px"></div>',
    ]
    for i, it in enumerate(images, 1):
        cap = clean_title(it.get("fromPageTitleEnc", ""))
        parts.append(
            f'<img src="{it["thumbURL"]}" style="width:100%;border-radius:6px;display:block;" />'
        )
        if cap:
            parts.append(
                f'<div style="text-align:center;color:#8a8a8a;font-size:13px;'
                f'margin:4px 0 14px 0;">{i}. {cap}</div>'
            )
        else:
            parts.append('<div style="height:14px"></div>')
    srcs = " / ".join(dict.fromkeys(it.get("source", "") for it in images))
    parts.append(
        '<div style="text-align:center;color:#b0b0b0;font-size:12px;border-top:1px dashed #ddd;'
        f'padding-top:8px;">每日自动推送 · 图源 {srcs} · 长按可保存</div></div>'
    )
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
        session.get("https://www.bing.com/", timeout=15)      # 拿必应会话 cookie
    except requests.RequestException as e:
        log(f"搜索站 cookie 预热失败（继续尝试）: {e}")

    # 同一天固定种子：workflow 重跑结果一致，配合历史去重
    rng = random.Random(date.today().isoformat())
    images = pick_images(session, rng)
    if not images:
        log("没有可用图片，退出")
        return 1
    if len(images) < NUM_IMAGES:
        log(f"警告：只拿到 {len(images)} 张")

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
