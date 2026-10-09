# coding=utf-8
"""手机好价监控：OPPO / vivo 系新机优惠 → PushPlus 微信服务号

信息源（均为实测可抓的第一手/最快聚合源）：
1. 什么值得买 · 手机分类好价 —— 爆料聚合，官方旗舰店价格变动分钟级收录
   https://www.smzdm.com/fenlei/shouji/
2. IT之家 RSS —— 官方促销/降价新闻首发
   https://www.ithome.com/rss/

行为：
- 对比 output/phone_deal_history.json，只推送没见过的新条目
- 首次运行仅记录基线不推送
- 一条消息合并本批所有新条目（上限 MAX_PUSH 条）

环境变量：
- PUSHPLUS_TOKEN  必填

用法：
  python scripts/phone_deal.py           # 正常运行（增量推送）
  python scripts/phone_deal.py --test    # 测试：推送最新 5 条（忽略历史）
  python scripts/phone_deal.py --dry-run # 只打印不推送
"""

import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path

import requests

# ──────────────────────────── 配置 ────────────────────────────

MAX_PUSH = 10                # 单次消息最多条目数
HISTORY_KEEP = 500           # 历史保留
HISTORY_PATH = Path(__file__).resolve().parent.parent / "output" / "phone_deal_history.json"

PUSH_URL = "https://www.pushplus.plus/send"

# 关注品牌（OPPO 系：OPPO/一加/真我；vivo 系：vivo/iQOO）
BRAND_RE = re.compile(r"OPPO|vivo|iQOO|一加|OnePlus|真我|realme", re.I)
# 资讯需同时命中的促销词（IT之家只推带价格动作的新闻）
DEAL_WORD_RE = re.compile(
    r"优惠|降价|好价|直降|到手|券|促销|秒杀|补贴|首发价|首销|开售|预售|立减|白条|免息|11\.11|618|双11"
)

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

SMZDM_URL = "https://www.smzdm.com/fenlei/shouji/"
ITHOME_RSS = "https://www.ithome.com/rss/"


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ──────────────────────── 源一：什么值得买好价 ────────────────────────

def fetch_smzdm() -> list[dict]:
    """解析手机分类好价页：/p/ 直链 + 标题 + 附近价格"""
    r = requests.get(SMZDM_URL, headers=UA, timeout=25)
    r.raise_for_status()
    text = r.text
    items: dict[str, dict] = {}
    pat = re.compile(r'href="(https://www\.smzdm\.com/p/(\d+)/)"[^>]*>(?:<!--.*?-->)?([^<]{6,150})</a>', re.S)
    for m in pat.finditer(text):
        url, pid, title = m.group(1), m.group(2), m.group(3)
        if pid in items:
            continue
        title = re.sub(r"\s+", " ", re.sub(r"<.*?>", "", title)).strip()
        if not BRAND_RE.search(title):
            continue
        # 链接附近的价格字段（class 含 price 的元素）
        win = text[m.start(): m.start() + 1500]
        pm = re.search(r'class="[^"]*price[^"]*"[^>]*>\s*[¥￥]?\s*([\d]{2,6}(?:\.\d{1,2})?)', win)
        price = pm.group(1) if pm else ""
        items[pid] = {"url": url, "title": title, "price": price, "src": "smzdm"}
    out = list(items.values())
    log(f"[smzdm] 手机好价 {len(out)} 条（品牌过滤后）")
    return out


# ──────────────────────── 源二：IT之家 RSS ────────────────────────

def fetch_ithome() -> list[dict]:
    r = requests.get(ITHOME_RSS, headers=UA, timeout=25)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for it in root.findall(".//item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not (BRAND_RE.search(title) and DEAL_WORD_RE.search(title)):
            continue
        if not link.startswith("http"):
            continue
        out.append({"url": link, "title": title, "price": "", "src": "ithome"})
    log(f"[ithome] 促销资讯 {len(out)} 条")
    return out


# ──────────────────────── 历史（增量去重） ────────────────────────

def load_history() -> set[str]:
    try:
        return set(json.loads(HISTORY_PATH.read_text(encoding="utf-8")).get("urls", []))
    except (OSError, ValueError):
        return set()


def save_history(urls: list[str]) -> None:
    merged = list(dict.fromkeys(sorted(load_history()) + urls))[-HISTORY_KEEP:]
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(
        json.dumps({"urls": merged, "updated": date.today().isoformat()},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


# ──────────────────────── 消息组装与推送 ────────────────────────

def build_text(deals: list[dict], news: list[dict]) -> str:
    now = datetime.now().strftime("%m-%d %H:%M")
    total = len(deals) + len(news)
    lines = [
        "━━━━━━━━━━━━━━━━━━",
        f"📱 手机好价速报 · {now} · 新 {total} 条",
        "━━━━━━━━━━━━━━━━━━",
    ]
    if deals:
        lines += ["", f"━━ 好价爆料 · {len(deals)} 条 ━━"]
        for i, d in enumerate(deals[:MAX_PUSH], 1):
            head = f"▸ {i}. {d['title']}"
            if d["price"]:
                head += f"　¥{d['price']}"
            lines += [head, f"　　{d['url']}", ""]
        if len(deals) > MAX_PUSH:
            lines.append(f"（另有 {len(deals) - MAX_PUSH} 条见 smzdm 手机频道）")
    if news:
        lines += ["", f"━━ 促销资讯 · {len(news)} 条 ━━"]
        for i, n in enumerate(news, 1):
            lines += [f"▸ {i}. {n['title']}", f"　　{n['url']}", ""]
    lines += ["━━━━━━━━━━━━━━━━━━", "每 30 分钟自动巡检 · 什么值得买 / IT之家"]
    return "\n".join(lines)


def push(token: str, title: str, content: str) -> None:
    r = requests.post(
        PUSH_URL,
        json={"token": token, "title": title, "content": content,
              "template": "txt", "channel": "wechat"},
        timeout=30,
    )
    r.raise_for_status()
    body = r.json()
    if body.get("code") != 200:
        raise RuntimeError(f"PushPlus 返回失败: {body}")
    log(f"推送成功 messageid={body.get('data')}")


# ──────────────────────────── 主流程 ────────────────────────────

def main() -> int:
    argv = sys.argv[1:]
    dry = "--dry-run" in argv
    test = "--test" in argv
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()

    deals, news = [], []
    for name, fn in (("smzdm", fetch_smzdm), ("ithome", fetch_ithome)):
        try:
            (deals if name == "smzdm" else news).extend(fn())
        except (requests.RequestException, ET.ParseError) as e:
            log(f"源 {name} 抓取失败: {type(e).__name__} {e}")

    if not deals and not news:
        log("两个源都没拿到数据，退出")
        return 1

    history = load_history()
    first_run = not HISTORY_PATH.exists()

    if test:
        combo = (deals + news)[:5]
        deals = [x for x in combo if x["src"] == "smzdm"]
        news = [x for x in combo if x["src"] == "ithome"]
        log(f"TEST 模式：推送最新 {len(combo)} 条")
    else:
        new_deals = [d for d in deals if d["url"] not in history]
        new_news = [n for n in news if n["url"] not in history]
        if first_run and not dry:
            save_history([x["url"] for x in deals + news])
            log(f"首次运行，基线记录 {len(deals) + len(news)} 条（不推送）")
            return 0
        deals, news = new_deals, new_news
        fresh = deals + news

    if not deals and not news:
        log("无新条目")
        return 0

    title = f"📱 手机好价 · 新 {len(deals) + len(news)} 条"
    content = build_text(deals, news)

    if dry:
        print("──── DRY RUN ────")
        print(title)
        print(content)
        return 0

    if not token:
        log("缺少 PUSHPLUS_TOKEN")
        return 1
    push(token, title, content)
    save_history([x["url"] for x in (deals + news)])
    log(f"历史 {len(load_history())} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
