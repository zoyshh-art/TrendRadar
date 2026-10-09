# coding=utf-8
"""手机好价监控：指定机型优惠 → PushPlus 微信服务号

监测范围（型号白名单）：
- OPPO Find X9 系列：Find X9 / X9 Pro / (未来变体全含)
- OPPO Find X10 系列：Find X10 / X10 Pro Max / X10 E / (未来 Ultra)
- vivo X300 系列：X300 / X300 Pro / X300s / X300 Ultra / X300 E / X300 FE
- vivo X500 系列：X500 / X500 Pro / X500 Pro Max
（前缀匹配，未来新增后缀自动覆盖）

推送规则：
- 同一「型号 + 价格」的消息最多推送 3 次，两次间隔至少 30 分钟
  （同款反复被爆料时：首见即推，30 分钟后若还在则第 2 次，60 分钟后第 3 次，之后不再推）
- 降价产生新价格 = 新消息，正常推送
- 资讯（IT之家）按链接去重，只推一次
- 首次运行仅记录基线不推送

信息源：
1. 什么值得买 · 手机分类好价 https://www.smzdm.com/fenlei/shouji/
2. IT之家 RSS https://www.ithome.com/rss/

环境变量：PUSHPLUS_TOKEN（必填）
用法：python scripts/phone_deal.py [--test | --dry-run]
"""

import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

import requests

# ──────────────────────────── 配置 ────────────────────────────

MAX_PUSH = 10                 # 单次消息最多条目数
MAX_SAME_PUSH = 3             # 同一「型号+价格」最多推送次数
SAME_PUSH_GAP_MIN = 30        # 同款两次推送最小间隔（分钟）
STATE_KEEP = 800              # 状态保留条数
STATE_MAX_AGE_DAYS = 60       # 状态保留天数
STATE_PATH = Path(__file__).resolve().parent.parent / "output" / "phone_deal_state.json"
LEGACY_PATH = Path(__file__).resolve().parent.parent / "output" / "phone_deal_history.json"

PUSH_URL = "https://www.pushplus.plus/send"

# ── 型号白名单（前缀匹配，覆盖全系列含未来后缀） ──
MODEL_RE = re.compile(
    "|".join([
        r"(?:OPPO\s*)?Find\s*X\s?(?:9|10)(?!\d)",   # OPPO Find X9 / X10 系列
        r"OPPO\s*X\s?(?:9|10)(?!\d)",                # 少数爆料省略 Find
        r"vivo\s*X\s?(?:300|500)(?!\d)",             # vivo X300 / X500 系列
    ]),
    re.I,
)

# IT之家资讯需同时命中的促销词
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


def model_key(match_text: str) -> str:
    """匹配文本 → 归一化型号键：'OPPO Find X9 Pro' → 'findx9'"""
    k = re.sub(r"[^a-z0-9]", "", match_text.lower())
    return k.replace("oppo", "").replace("vivo", "")


# ──────────────────────── 源一：什么值得买好价 ────────────────────────

def fetch_smzdm() -> list[dict]:
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
        mm = MODEL_RE.search(title)
        if not mm:
            continue
        win = text[m.start(): m.start() + 1500]
        pm = re.search(r'class="[^"]*price[^"]*"[^>]*>\s*[¥￥]?\s*([\d]{2,6}(?:\.\d{1,2})?)', win)
        price = pm.group(1) if pm else ""
        mk = model_key(mm.group(0))
        items[pid] = {
            "url": url, "title": title, "price": price,
            "src": "smzdm", "kind": "deal",
            "key": f"{mk}|{price}" if price else f"{mk}|{url}",
            "model": mk,
        }
    out = list(items.values())
    log(f"[smzdm] 命中机型好价 {len(out)} 条")
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
        if not (MODEL_RE.search(title) and DEAL_WORD_RE.search(title)):
            continue
        if not link.startswith("http"):
            continue
        out.append({"url": link, "title": title, "price": "",
                    "src": "ithome", "kind": "news", "key": f"n|{link}", "model": ""})
    log(f"[ithome] 命中机型促销资讯 {len(out)} 条")
    return out


# ──────────────── 状态：同价 3 次 / 间隔 30 分钟 ────────────────

def load_state() -> dict:
    """返回 {key: {"count": n, "last": iso}}；旧格式/损坏视为首次运行"""
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data.get("entries"), dict):
            return data["entries"]
    except (OSError, ValueError):
        pass
    return {}


def save_state(entries: dict) -> None:
    # 修剪：超期 + 超量
    cutoff = (datetime.now() - timedelta(days=STATE_MAX_AGE_DAYS)).isoformat(timespec="seconds")
    kept = {k: v for k, v in entries.items() if v.get("last", "") >= cutoff}
    if len(kept) > STATE_KEEP:
        for k in sorted(kept, key=lambda x: kept[x].get("last", ""))[: len(kept) - STATE_KEEP]:
            del kept[k]
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps({"entries": kept, "updated": datetime.now().isoformat(timespec="seconds")},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def gate(items: list[dict], entries: dict) -> list[dict]:
    """按规则筛出本批可推送条目；准入即刻写入内存状态（防同批重复）"""
    now = datetime.now()
    fresh = []
    for it in items:
        e = entries.get(it["key"])
        if it["kind"] == "news":
            if e is None:
                fresh.append(it)
                entries[it["key"]] = {"count": 1, "last": now.isoformat(timespec="seconds")}
        else:
            if e is None:
                fresh.append(it)
                entries[it["key"]] = {"count": 1, "last": now.isoformat(timespec="seconds")}
            elif e["count"] < MAX_SAME_PUSH:
                last = datetime.fromisoformat(e["last"])
                if now - last >= timedelta(minutes=SAME_PUSH_GAP_MIN):
                    e["count"] += 1
                    e["last"] = now.isoformat(timespec="seconds")
                    fresh.append(it)
    return fresh


# ──────────────────────── 消息组装与推送 ────────────────────────

def build_text(deals: list[dict], news: list[dict]) -> str:
    now = datetime.now().strftime("%m-%d %H:%M")
    total = len(deals) + len(news)
    lines = [
        "━━━━━━━━━━━━━━━━━━",
        f"📱 机型好价速报 · {now} · 新 {total} 条",
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
    lines += [
        "━━━━━━━━━━━━━━━━━━",
        "监测：Find X9/X10 · vivo X300/X500 全系列",
        "同款同价最多 3 次，间隔 30 分钟 · 每 5 分钟巡检",
    ]
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
    for name, fn, bucket in (("smzdm", fetch_smzdm, deals), ("ithome", fetch_ithome, news)):
        try:
            bucket.extend(fn())
        except (requests.RequestException, ET.ParseError) as e:
            log(f"源 {name} 抓取失败: {type(e).__name__} {e}")

    if not deals and not news:
        log("两个源都没拿到数据，退出")
        return 1

    entries = load_state()
    first_run = not STATE_PATH.exists() or not entries

    if test:
        combo = (deals + news)[:5]
        deals = [x for x in combo if x["kind"] == "deal"]
        news = [x for x in combo if x["kind"] == "news"]
        for it in combo:   # 测试也记账，避免后续重复推
            entries[it["key"]] = {"count": 1,
                                  "last": datetime.now().isoformat(timespec="seconds")}
        log(f"TEST 模式：推送最新 {len(combo)} 条")
    elif first_run and not dry:
        for it in deals + news:
            entries[it["key"]] = {"count": 1,
                                  "last": datetime.now().isoformat(timespec="seconds")}
        save_state(entries)
        if LEGACY_PATH.exists():
            LEGACY_PATH.unlink()   # 清掉旧格式文件
        log(f"首次运行，基线记录 {len(deals) + len(news)} 条（不推送）")
        return 0
    else:
        all_items = deals + news
        fresh = gate(all_items, entries)
        deals = [x for x in fresh if x["kind"] == "deal"]
        news = [x for x in fresh if x["kind"] == "news"]

    if not deals and not news:
        log("无新条目（或同款未到 30 分钟间隔）")
        save_state(entries)   # 记录间隔时钟
        return 0

    title = f"📱 机型好价 · 新 {len(deals) + len(news)} 条"
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
    save_state(entries)
    log(f"状态 {len(entries)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
