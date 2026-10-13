# coding=utf-8
"""每日赚钱机会雷达：多源信号挖掘 → 打分筛选 → 推送微信（PushPlus）

定位：推送"冷门但收益大、相对安全"的变现机会信号，不是新闻搬运。
信号源（全部 runner 美国节点已实测可达）：
  - GitHub 新星：近 7/14 天新建且快速起量的仓库（早期红利主信号）
  - GitHub 关键词新仓：monetization/saas/affiliate/automation 等变现词 + 新建
  - Hacker News Front/Show：真实产品需求信号（Show HN = 有人做、有人讨论）
  - Product Hunt：每日新品（海外付费意愿强的赛道）
  - V2EX：小众软件/独立开发者/接单（国内视角，冷门工具）
  - 少数派 / IT之家 / 钛媒体 / 量子位 / Solidot / 小众软件（中文资讯补充）

筛选逻辑：变现关键词命中 × 冷门度（新/低星=红利期）× 热度（起量速度）× 安全分（剔除灰产/高风险）
去重：标题归一化 + URL 去重，state 持久化，N 天内不重复推同一机会。

环境变量：
  PUSHPLUS_TOKEN（必填）
  PUSHPLUS_CHANNELS（默认 clawbot）
  MONEY_DRY_RUN=1 仅试跑不推送
用法：python scripts/money_radar.py [--dry-run]
"""

import io
import json
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

CN_TZ = timezone(timedelta(hours=8))
STATE_PATH = Path(__file__).resolve().parent.parent / "output" / "money_radar_state.json"
PUSH_URL = "https://www.pushplus.plus/send"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
GITHUB_HDR = {"Accept": "application/vnd.github+json", "User-Agent": "money-radar"}

# ──────────────────── 配置 ────────────────────
MAX_PUSH = 8                # 每轮最多推几条
MIN_SCORE = 2.0             # 入选分数门槛
DEDUP_DAYS = 14             # 同一机会 N 天内不重复

# 变现关键词（强信号，命中加权）
MONEY_KW = re.compile(
    r"monetiz|making money|make money|passive income|side hustle|revenue|profit|"
    r"saas|subscription|paywall|affiliate|ecommerce|e-commerce|shopify|stripe|"
    r"adsense|ad revenue|paid tier|freemium|pricing|billing|"
    r"marketplace|listing|lead gen|lead generation|outreach|sales|crm|"
    r"发票|赚钱|变现|副业|接单|带货|佣金|广告|流量主|广告联盟|分成|订阅|付费|招商|加盟|外包", re.I)
# 工具/自动化类（可包装成服务）
TOOL_KW = re.compile(
    r"automat|bot\b|scrap|crawl|workflow|no-?code|\bapi\b|agent|llm|ai[- ]|"
    r"dash|monitor|track|alert|schedul|cron|generat|prompt|template", re.I)
# 高风险/灰产（命中即剔除：交易/博彩/破解/代理注册等）
RISKY_KW = re.compile(
    r"\btrade-?bot|trading|hyperliquid|binance|arbitrage|pump|dump|"
    r"crypto|web3|token|coin|stock|forex|quant|"
    r"crack|hack|exploit|malware|ransomware|ddos|botnet|phish|download-?er|keygen|"
    r"1password|vpn|proxy|"
    r"偷|盗版|破解|刷单|刷量|赌博|博彩|色情|裸|约炮|代办|代实名|洗钱|"
    r"薅羊毛|批量注册|账号买卖|虚拟币|合约|开单", re.I)
# 明显不是"赚钱机会"的大类（基建库/学习资料/个人炫技/硬件驱动/游戏移植/纯工具作品）
LOW_VALUE_KW = re.compile(
    r"tutorial|course|awesome|roadmap|interview|leetcode|"
    r"learn|book|lecture|cheat-?sheet|简历|教程|学习|"
    r"driver|kernel|firmware|compile|emulator|汇编|内核|驱动|"
    r"rust rewrite|clean-?room|操作系统|"
    r"recompilation|native port|game port|移植|游戏|"
    r"isometric illustration|penetration test|pentest|渗透|挑战赛|"
    r"operating system|is a plugin|harness|", re.I)


def log(msg: str) -> None:
    print(f"[{datetime.now(CN_TZ).strftime('%H:%M:%S')}] {msg}", flush=True)


def now_cn() -> datetime:
    return datetime.now(CN_TZ).replace(tzinfo=None)


def days_ago_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


# ──────────────────── 状态 ────────────────────
def load_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return {"pushed": data.get("pushed", {}), "init": data.get("init")}
    except (OSError, ValueError):
        return {"pushed": {}, "init": None}


def save_state(st: dict) -> None:
    # 清理过期记录
    cutoff = (now_cn() - timedelta(days=DEDUP_DAYS)).isoformat()
    st["pushed"] = {k: v for k, v in st["pushed"].items() if v >= cutoff}
    st["updated"] = now_cn().isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def norm_key(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z一-鿿]+", "", (s or "").lower())[:24]


# ──────────────────── 采集器 ────────────────────
def gh_search(query: str, per_page: int = 8) -> list[dict]:
    """GitHub 搜索（带 5 分钟内存缓存，避免多关键词重复打同一接口触发限流）"""
    if not hasattr(gh_search, "_cache"):
        gh_search._cache = {}
    now_t = time.time()
    cache = gh_search._cache
    if query in cache and now_t - cache[query][0] < 300:
        return cache[query][1]
    try:
        url = (f"https://api.github.com/search/repositories?q={urllib.parse.quote(query)}"
               f"&sort=stars&order=desc&per_page={per_page}")
        r = requests.get(url, headers=GITHUB_HDR, timeout=20)
        if r.status_code == 403:
            log(f"  GitHub API 限流（403），跳过: {query[:40]}")
            cache[query] = (now_t, [])   # 限流的当轮也缓存空，避免反复打
            return []
        items = (r.json() or {}).get("items", []) or []
        cache[query] = (now_t, items)
        return items
    except Exception as e:
        log(f"  GitHub 搜索异常 {type(e).__name__}: {query[:40]}")
        return []


def parse_feed(url: str) -> list[dict]:
    """通用 RSS/Atom 解析，返回 [{title, link, desc}]"""
    try:
        r = requests.get(url, headers=UA, timeout=20)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception:
        return []
    out = []
    # RSS
    for item in root.iter():
        tag = item.tag.split("}")[-1] if "}" in item.tag else item.tag
        if tag == "item":
            t = _txt(item, "title")
            link = _txt(item, "link")
            desc = _txt(item, "description") or _txt(item, "content:encoded")
            if t:
                out.append({"title": _clean(t), "link": link, "desc": _strip_html(desc)})
        elif tag == "entry":
            t = _txt(item, "title")
            link = ""
            for el in item.iter():
                if el.tag.split("}")[-1] == "link" and el.get("href"):
                    link = el.get("href")
                    break
            desc = _txt(item, "summary") or _txt(item, "content")
            if t:
                out.append({"title": _clean(t), "link": link, "desc": _strip_html(desc)})
    return out


def _txt(el, name) -> str:
    for c in el:
        if c.tag.split("}")[-1] == name:
            return (c.text or "").strip()
    return ""


def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", s).strip()[:200]


def _clean(s: str) -> str:
    s = re.sub(r"<!\[CDATA\[|\]\]>", "", s or "")
    return re.sub(r"\s+", " ", s).strip()


# ──────────────────── 机会抽取 + 评分 ────────────────────
def make_opportunities() -> list[dict]:
    ops = []

    # 1) GitHub 近 7 天新星（早期红利最强信号）
    for it in gh_search(f"created:>{days_ago_iso(7)} stars:>30"):
        days = max(1, (datetime.now(timezone.utc) - datetime.fromisoformat(it["created_at"].replace("Z", "+00:00"))).days + 1)
        ops.append({
            "title": f"{it['full_name']}（{it['stargazers_count']}★/{days}天）",
            "link": it["html_url"],
            "desc": it.get("description") or "",
            "src": "GitHub新星",
            "stars": it["stargazers_count"], "days": days,
        })

    # 2) GitHub 变现关键词新仓（14 天）
    for q in ["monetization", "side hustle", "saas", "affiliate", "freemium"]:
        for it in gh_search(f"created:>{days_ago_iso(14)} {q} stars:>20", per_page=5):
            days = max(1, (datetime.now(timezone.utc) - datetime.fromisoformat(it["created_at"].replace("Z", "+00:00"))).days + 1)
            ops.append({
                "title": f"{it['full_name']}（{it['stargazers_count']}★/{days}天）",
                "link": it["html_url"], "desc": it.get("description") or "",
                "src": f"GitHub新仓·{q}", "stars": it["stargazers_count"], "days": days,
            })

    # 3) Hacker News Front / Show
    for url, tag in [("https://hnrss.org/show", "HN·Show"), ("https://hnrss.org/frontpage", "HN·热门")]:
        for it in parse_feed(url):
            ops.append({"title": it["title"], "link": it["link"], "desc": it["desc"],
                         "src": tag, "stars": 0, "days": 0})

    # 4) Product Hunt
    for it in parse_feed("https://www.producthunt.com/feed"):
        ops.append({"title": it["title"], "link": it["link"], "desc": it["desc"],
                     "src": "ProductHunt", "stars": 0, "days": 0})

    # 5) 国内源（独立开发/小众/接单视角）
    for url, tag in [
        ("https://www.v2ex.com/index.xml", "V2EX"),
        ("https://www.appinn.com/feed", "小众软件"),
        ("https://sspai.com/feed", "少数派"),
        ("https://www.qbitai.com/feed", "量子位"),
    ]:
        for it in parse_feed(url):
            ops.append({"title": it["title"], "link": it["link"], "desc": it["desc"],
                         "src": tag, "stars": 0, "days": 0})

    return score_and_filter(ops)


def score_and_filter(ops: list[dict]) -> list[dict]:
    """评分：变现信号 + 冷门红利 + 墙外需求源基线分 − 高星降权 − 低价值降权

    设计要点：
    - 墙外需求源（HN Show/Front、ProductHunt）天然代表"有人做、有人付费"，
      给基础分，不要求必须命中变现词（否则会全被刷掉、推送100%变GitHub）
    - GitHub 源靠"变现词 + 起量速度"竞争，天然高分
    - 风险词硬性剔除（加密交易/破解/代理等）
    """
    scored = []
    # 墙外需求源：本身即信号，给基础分
    DEMAND_SRC = {"HN·Show", "HN·热门", "ProductHunt", "V2EX"}
    for op in ops:
        text = f"{op['title']} {op.get('desc','')}"
        # 风险过滤（硬性）
        if RISKY_KW.search(text):
            continue
        score = 0.0
        # 变现信号（强）
        if MONEY_KW.search(text):
            score += 2.0
        # 工具/自动化（可包装成服务）
        if TOOL_KW.search(text):
            score += 1.0
        # 墙外需求源基线分：产品被公开发布 = 有市场验证
        if op["src"] in DEMAND_SRC:
            score += 1.5
        # GitHub 冷门红利：新 + 快起量 = 红利期（先来者赚信息差）
        stars = op.get("stars", 0)
        days = op.get("days", 0)
        if days and days <= 7 and stars >= 100:
            score += 2.5
        elif days and days <= 14 and stars >= 50:
            score += 1.5
        elif days and days <= 3:
            score += 0.5
        # 高星已爆的降权（不冷门了）
        if stars >= 3000:
            score -= 1.0
        # 资讯/教程/驱动类降权
        if LOW_VALUE_KW.search(text):
            score -= 1.5
        # 无链接降权
        if not op.get("link"):
            score -= 2.0
        op["score"] = round(score, 2)
        if score >= MIN_SCORE:
            scored.append(op)
    scored.sort(key=lambda x: -x["score"])
    return scored


def dedup_and_filter_pushed(ops: list[dict], state: dict) -> list[dict]:
    """URL + 标题去重 + N 天内推过的剔除"""
    out, seen = [], set()
    for op in ops:
        ukey = norm_key(op["link"]) or norm_key(op["title"])
        tkey = norm_key(op["title"])
        if ukey in seen:
            continue
        # state 里近 N 天推过
        if op["link"] in state["pushed"]:
            continue
        if any(k in state["pushed"] for k in [ukey] if k):
            continue
        seen.add(ukey)
        op["ukey"] = ukey
        op["tkey"] = tkey
        out.append(op)
    return out


# ──────────────────── 推送 ────────────────────
def build_content(ops: list[dict]) -> str:
    today = now_cn().strftime("%Y-%m-%d")
    lines = [f"💰【赚钱机会】{len(ops)} 条 · {today}", ""]
    for i, op in enumerate(ops, 1):
        src = op["src"]
        lines.append(f"{i}. [{src}·{op['score']:.1f}分] {op['title']}")
        d = op.get("desc", "")
        if d:
            lines.append(f"   {d[:110]}")
        if op.get("link"):
            lines.append(f"   {op['link']}")
        lines.append("")
    lines.append("— 每日筛选：新工具红利 × 变现潜力 × 低风险")
    return "\n".join(lines)


def push(token: str, title: str, content: str) -> None:
    channels = [c.strip() for c in os.environ.get("PUSHPLUS_CHANNELS", "clawbot").split(",") if c.strip()]

    def send(ch: str) -> tuple:
        try:
            r = requests.post(PUSH_URL, json={"token": token, "title": title,
                                              "content": content, "template": "txt", "channel": ch}, timeout=30)
            r.raise_for_status()
            b = r.json()
            return (ch, b.get("code") == 200, f"messageid={b.get('data')}" if b.get("code") == 200 else f"code={b.get('code')}")
        except Exception as e:
            return (ch, False, f"{type(e).__name__}")

    results = [send(c) for c in channels]
    if not any(ok for _, ok, _ in results) and "wechat" not in channels:
        results.append(send("wechat"))
    for ch, ok, detail in results:
        log(f"推送[{ch}] {'成功' if ok else '失败'} {detail}")
    if not any(ok for _, ok, _ in results):
        raise RuntimeError("所有渠道推送失败")


# ──────────────────── 主流程 ────────────────────
def main() -> int:
    dry = "--dry-run" in sys.argv or os.environ.get("MONEY_DRY_RUN") == "1"
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()

    state = load_state()
    first_run = state.get("init") is None

    log("开始扫描赚钱机会信号源…")
    ops = make_opportunities()
    log(f"初筛 {len(ops)} 条达标（score≥{MIN_SCORE}）")

    fresh = dedup_and_filter_pushed(ops, state)[:MAX_PUSH]

    if not fresh:
        log("今日无新机会（全部已推过/未达标），不推送")
        if not dry:
            state["init"] = now_cn().isoformat()
            save_state(state)
        return 0

    log(f"待推 {len(fresh)} 条：")
    for op in fresh:
        log(f"  [{op['score']:.1f}][{op['src']}] {op['title'][:50]}")

    content = build_content(fresh)
    title = f"💰【赚钱机会】{len(fresh)}条 · {now_cn().strftime('%m-%d')}"

    if dry:
        print("──── DRY RUN ────")
        print(title)
        print(content)
        return 0

    if not token:
        log("缺少 PUSHPLUS_TOKEN")
        return 1
    push(token, title, content)

    # 记录已推（按 URL）
    for op in fresh:
        if op.get("link"):
            state["pushed"][op["link"]] = now_cn().isoformat()
    state["init"] = now_cn().isoformat()
    save_state(state)
    log(f"已记录 {len(state['pushed'])} 条历史机会")
    return 0


if __name__ == "__main__":
    sys.exit(main())