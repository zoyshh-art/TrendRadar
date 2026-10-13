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
    r"operating system|is a plugin|harness", re.I)
# 求助/抱怨帖（论坛噪音，不是机会）
ASK_KW = re.compile(
    r"求推荐|求助|请问|有没有|请教|求助帖|求教|怎么办|无法|报错|"
    r"求一份|有没有人|好用吗|能用吗|还能|吗[?？]|？$|"
    r"\[讨论\]|\[求助\]|\[吐槽\]|\[问与答\]|\[Apple\]|\[YouTube\]|\[问与答\]", re.I)

# V2EX 板块白名单：只有"作品/接单"板块是机会，其余（问与答/Apple/YouTube/新闻）是噪音
V2EX_KEEP = re.compile(r"\[(分享创造|酷工作|外包|创业|招聘|自由职业|独立开发)\]", re.I)
V2EX_DROP = re.compile(r"\[(问与答|Apple|YouTube|Google|编程|游戏|二手交易|酷工作招聘|闲聊|问与答|职场|机制| Sugi )\]", re.I)

# ──────────────────── 变现路径手册（核心增值：告诉用户"这玩意怎么赚钱"）
# 按关键词归类到可落地的变现套路；命中即贴标签，帮助用户直接判断可行性
PLAYBOOK: list[tuple[str, "re.Pattern", str]] = [
    ("视频/素材代做", re.compile(r"video|short-?video|promo|clip|reels|tiktok|剪辑|宣传片|宣传", re.I),
     "接单帮中小商家/独立开发者做产品宣传片，短视频切片代运营；按条或按月打包收费"),
    ("数据采集/监控服务", re.compile(r"scrap|crawl|monitor|track|alert|spy|dashboard|采集|监控", re.I),
     "做行业数据订阅（竞品/价格/舆情监控），按月收订阅费；或直接卖定制采集方案"),
    ("自动化工作流交付", re.compile(r"automat|workflow|no-?code|n8n|orchestrat|pipeline|自动化|工作流", re.I),
     "给企业做流程自动化落地实施（一次性实施费 + 后续维护），客单价最高的一条路"),
    ("AI 应用/SaaS 化", re.compile(r"saas|platform|app|dashboard|api\b|agent|llm|ai[- ]", re.I),
     "把开源内核套成垂直 SaaS，按订阅/按量收费；国内注意 ICP 与生成式 AI 备案"),
    ("电商/带货", re.compile(r"shopify|store|ecommerce|e-commerce|stripe|shop|商城|带货", re.I),
     "独立站代建 + 商品选品代运营，或做联盟营销（affiliate）分佣"),
    ("内容/流量变现", re.compile(r"newsletter|substack|blog|seo|news|rss|content|公众号|流量主|ad-?sense|广告", re.I),
     "做垂直内容号（公众号/小红书/SEO 站），走流量主/广告/付费专栏，变现慢但稳"),
    ("开发者周边", re.compile(r"template|boilerplate|starter|kit|sdk|toolkit|模板|脚手架", re.I),
     "上架模板市场/卖源码包（Gumroad、GitHub Sponsors、小报童），一次制作多次售卖"),
    ("接单/外包平台", re.compile(r"freelanc|contract|client|agency|hiring|外包|接单", re.I),
     "去接单平台或社群接定制开发单，单价高但需作品集背书"),
]

# 安全等级：按内容自动打标（相对安全 = 少碰灰产、盗版、博彩）
SAFE_HINT_KW = re.compile(r"open-?source|self-?host|privacy|合法|合规|self-hosted", re.I)

# ── 自检：正则尾部多一个 "|" 会匹配空串，导致该过滤器命中所有条目。
#    历史上踩过这个坑（LOW_VALUE 曾恒为真），此处做启动断言兜底。
for _name, _rx in [("MONEY_KW", MONEY_KW), ("TOOL_KW", TOOL_KW), ("RISKY_KW", RISKY_KW),
                   ("LOW_VALUE_KW", LOW_VALUE_KW), ("ASK_KW", ASK_KW), ("SAFE_HINT_KW", SAFE_HINT_KW)]:
    if _rx.pattern.rstrip().endswith("|"):
        raise RuntimeError(f"{_name} 正则以 '|' 结尾，会匹配空串（会误杀全部条目），请修正")
    if _rx.search("hello world this is a generic sentence 2024"):
        raise RuntimeError(f"{_name} 对普通文本误命中，正则疑似损坏，请修正")


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
        ("https://www.v2ex.com/index.xml", "V2EX·作品"),
        ("https://www.appinn.com/feed", "小众软件"),
        ("https://sspai.com/feed", "少数派"),
        ("https://www.qbitai.com/feed", "量子位"),
    ]:
        for it in parse_feed(url):
            ops.append({"title": it["title"], "link": it["link"], "desc": it["desc"],
                         "src": tag, "stars": 0, "days": 0})

    return score_and_filter(ops)


def classify_play(text: str) -> tuple[str, str]:
    """归类到变现路径手册，返回 (类别, 变现套路)。未命中返回空。"""
    for name, pat, how in PLAYBOOK:
        if pat.search(text):
            return name, how
    return "", ""


def score_and_filter(ops: list[dict]) -> list[dict]:
    """评分 + 硬门槛过滤

    门槛（任一不满足直接淘汰，不是扣分）：
      1. 风险词：加密交易/破解/灰产 → 淘汰
      2. 低价值词：教程/驱动/游戏移植/炫技 → 淘汰
      3. 求助抱怨帖：论坛噪音 → 淘汰
      4. 无链接：不可点击 = 无价值 → 淘汰
    评分（仅对通过门槛的）：
      变现信号 + 墙外需求源基线 + GitHub 冷门红利 − 高星已爆
    变现路径：命中手册则贴标签并加分（用户最需要的是"怎么赚"）
    """
    scored = []
    DEMAND_SRC = {"HN·Show", "HN·热门", "ProductHunt", "V2EX·作品"}
    for op in ops:
        title = op.get("title", "")
        desc = op.get("desc", "")
        text = f"{title} {desc}"

        # ── 硬门槛 ──
        if RISKY_KW.search(text):
            continue
        if LOW_VALUE_KW.search(text):
            continue
        if ASK_KW.search(title):
            continue
        if op["src"] == "V2EX·作品":
            # V2EX 首页混着大量提问帖/新闻贴，只留"作品/接单/创业"板块
            if not V2EX_KEEP.search(title):
                continue
        if not op.get("link"):
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
        # 变现路径归因（核心增值）
        cat, how = classify_play(text)
        if cat:
            score += 1.5
            op["cat"] = cat
            op["how"] = how
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
# ──────────────────── 中文概括（翻译 + 扩写 200-300 字） ────────────────────
# 主通道：LLM（OpenAI 兼容协议，DeepSeek/OpenAI/Moonshot/Qwen 通用）
#   环境变量 LLM_API_KEY（必填）+ LLM_BASE_URL（可选）+ LLM_MODEL（可选）
# 降级通道：MyMemory 免 key 机器翻译（只能直译，扩不动 200 字）
LLM_BASE = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "40"))
MAX_CONCURRENCY = int(os.environ.get("LLM_CONCURRENCY", "4"))

DIGEST_PROMPT = """你是中文商业分析师。请把下面这条「赚钱机会信号」写成一段 **200-300 字的中文分析**，要求：

1. **翻译并归纳**原始标题与描述（原文可能是英文）
2. 说明它**到底是什么东西**、解决什么问题
3. 分析**为什么现在值得看**（冷门红利期 / 需求已验证 / 竞争者还少）
4. 指出**变现路径**和**主要风险或门槛**
5. 用平实的中文，不要营销腔，不要 emoji，不要标题，不要分点

严格要求：
- 篇幅 **200-300 个汉字**，这是硬要求
- 不要输出任何解释性前后缀，直接给正文
- 如果原文信息很少（只有一句话描述），就基于已知信息合理推断，但**不要编造具体数字、融资金额、用户量**

信号来源：{src}
信号类别：{cat}
原文标题：{title}
原文描述：{desc}
链接：{link}
信号评分：{score}分
"""

_fallback_cn: dict[str, str] = {}


def _my_memory_translate(text: str) -> str:
    """降级：MyMemory 免 key 机器翻译（限长 500 字符）"""
    if not text.strip():
        return ""
    if text in _fallback_cn:
        return _fallback_cn[text]
    try:
        r = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": text[:480], "langpair": "en|zh-CN"},
            timeout=15,
        )
        out = ((r.json() or {}).get("responseData") or {}).get("translatedText", "")
        out = re.sub(r"<[^>]+>", "", out or "").strip()
        _fallback_cn[text] = out
        return out
    except Exception:
        return ""


def _cn_char_len(s: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", s or ""))


def _signal_note(op: dict) -> str:
    """基于真实信号数据生成中文分析句（不编造任何未采集到的信息）"""
    src = op.get("src", "")
    stars, days = op.get("stars", 0), op.get("days", 0)
    if src.startswith("GitHub"):
        if days and days <= 7 and stars >= 100:
            return (f"这个项目开源才 {days} 天就积累 {stars} 个星标，属于典型的早期红利期——"
                    f"同类工具大多还没被中文开发者注意到，现在进场能吃到信息差。")
        if days:
            return (f"项目开源 {days} 天、{stars} 个星标，属于刚进入视野的早期信号，"
                    f"体量不大说明还没被充分开发验证。")
        return "来自 GitHub 的开源项目，可以直接查看代码和许可证，不存在闭源绑架的风险。"
    if "HN" in src:
        return ("它在 Hacker News 公开亮相并引发讨论，说明有真实开发者在做、也有开发者愿意关注，"
                "属于需求已被初步验证的信号，比单纯的自说自话可信度更高。")
    if "ProductHunt" in src:
        return ("它登上了 Product Hunt 的新品榜，这个榜单的读者是全球付费意愿最强的独立开发者群体，"
                "说明确实有人愿意为这类工具掏钱。")
    if "V2EX" in src:
        return ("这是国内独立开发者在 V2EX 主动公开的作品，敢发出来说明已经做到自认为可用的状态，"
                "国内同类项目少，容易在细分需求上形成差异。")
    return "来自公开信息源，可直接打开链接查看细节。"


def _gate_note(op: dict) -> str:
    """门槛与风险提示（按来源和类别给出真实需要注意的点）"""
    src = op.get("src", "")
    cat = op.get("cat", "")
    bits = []
    if src.startswith("GitHub"):
        bits.append("需要注意开源项目没有商业支持，文档完整度、长期维护和数据安全都要自己验证")
    elif "ProductHunt" in src or "HN" in src:
        bits.append("海外工具直接搬到国内通常会遇到访问稳定性与合规要求，需要评估落地成本")
    elif "V2EX" in src:
        bits.append("个人作品没有售后与稳定性承诺，商用前务必先在自己场景里跑通验证")
    if "SaaS" in cat:
        bits.append("国内做成对外产品还要注意 ICP 备案与生成式 AI 备案")
    if "电商" in cat:
        bits.append("涉及平台规则的自动化要留意平台条款，改动频繁且可能被处罚")
    return "；".join(bits) + "。" if bits else ""


def _ensure_punct(s: str) -> str:
    """确保段落以句末标点收尾，避免与下一段黏连"""
    s = (s or "").strip()
    if not s:
        return ""
    return s if s[-1] in "。！？；…" else s + "。"


def _fit_cn(text: str, lo: int = 200, hi: int = 300) -> str:
    """按「汉字数」把正文裁进 [lo, hi] 区间（不是字符数——中英混排会算错）。
    超长时按段落整体裁，优先保住信号分析与变现思路。"""
    if _cn_char_len(text) <= hi:
        return text
    lines = [l for l in text.split("\n") if l.strip()]
    kept = []
    n = 0
    for line in lines:
        ln = _cn_char_len(line)
        if n + ln > hi:
            # 当前段超预算：若还没保住任何内容，硬截到剩余额度
            if not kept:
                out, cnt = [], 0
                for ch in line:
                    if re.match(r"[\u4e00-\u9fff]", ch):
                        cnt += 1
                        if cnt > hi:
                            break
                    out.append(ch)
                return "".join(out).rstrip("，。；") + "。"
            break
        kept.append(line)
        n += ln
    return "\n".join(kept)


def digest_one(op: dict, key: str) -> None:
    """把 op 翻译并组装为 200-300 字中文，写回 op['cn_title']/op['cn_digest']"""
    title, desc = op.get("title", ""), op.get("desc", "")

    # ── 降级通道（无 LLM key）：翻译 + 结构化信号拼装 ──
    if not key:
        cn_desc = _my_memory_translate(desc[:420])
        # 项目名/仓库名翻译无意义，只翻译描述性标题（含空格且非路径）
        translatable = bool(re.search(r"[一-鿿]", title)) is False and " " in title and "/" not in title
        cn_title = _my_memory_translate(title) if translatable else title
        segs = [s for s in [cn_desc, _signal_note(op), (f"变现思路：{op['how']}。" if op.get("how") else ""),
                             _gate_note(op)] if s]
        # 每段独立成行，读起来有节奏（_cn_char_len 只统计汉字，不受换行影响）
        text = "\n".join(_ensure_punct(s) for s in segs)
        n = _cn_char_len(text)
        if n < 200:
            # 信息不足时补一段基于已知维度的研判，不编造具体数字
            pad = (f"综合看，它属于{op.get('cat') or '工具类'}方向，目前竞争者还不多，"
                   f"适合先用低成本方式验证需求，跑通后再决定要不要投入做产品。"
                   f"需要提醒的是，这类早期信号变化快，建议先收藏观察一两周，"
                   f"看它的星标、社区讨论和更新频率是否持续，再决定投入。")
            text = text + pad
        text = _fit_cn(text)
        if _cn_char_len(text) < 120:
            text = (cn_desc or desc or title) + _signal_note(op)
        op["cn_title"] = cn_title or title
        op["cn_digest"] = text
        op["degraded"] = True
        return

    prompt = DIGEST_PROMPT.format(
        src=op.get("src", ""), cat=op.get("cat", "未分类"),
        title=title, desc=desc or "(无描述)", link=op.get("link", ""),
        score=op.get("score", 0),
    )
    try:
        r = requests.post(
            f"{LLM_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": "你是严谨的中文商业分析师，输出必须严格遵守字数要求。"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.4,
                "max_tokens": 900,
            },
            timeout=LLM_TIMEOUT,
        )
        r.raise_for_status()
        body = r.json()
        text = (((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        # 去掉模型偶尔加的前缀/代码块
        text = re.sub(r"^```[a-z]*\n|\n```$", "", text).strip()
        text = re.sub(r"^(好的|以下是|分析如下)[：:]\s*", "", text).strip()

        if _cn_char_len(text) >= 120:
            op["cn_digest"] = text
            # 标题单独翻译（短，失败就用原文）
            op["cn_title"] = _llm_translate_title(title, key) or title
        else:
            raise ValueError(f"输出过短({_cn_char_len(text)}字)")
    except Exception as e:
        log(f"  概括失败 [{op.get('title','')[:36]}]: {type(e).__name__} {str(e)[:80]}")
        op["cn_title"] = _my_memory_translate(title) or title
        cn_desc = _my_memory_translate(desc[:400])
        op["cn_digest"] = cn_desc or desc[:200] or "（原文无描述）"
        op["degraded"] = True


def _llm_translate_title(title: str, key: str) -> str:
    try:
        r = requests.post(
            f"{LLM_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": f"把这个标题翻译成简洁中文，不要解释：{title}"}],
                "temperature": 0.2, "max_tokens": 120,
            },
            timeout=25,
        )
        r.raise_for_status()
        out = (((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        return re.sub(r"^[\"「『]|[\"」』]$", "", out.split("\n")[0])[:60]
    except Exception:
        return ""


def enrich_chinese(ops: list[dict], key: str) -> None:
    """并发为每条生成 200-300 字中文概括"""
    if not ops:
        return
    need = [op for op in ops if not op.get("cn_digest")]
    if not need:
        return
    log(f"[概括] 需补全 {len(need)} 条（目标 200-300 字中文，{'LLM: ' + LLM_MODEL if key else '降级: 机器直译'}）")
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as pool:
        list(pool.map(lambda op: digest_one(op, key), need))
    ok = sum(1 for op in need if not op.get("degraded"))
    avg = (sum(_cn_char_len(op.get("cn_digest", "")) for op in need) / len(need)) if need else 0
    inrange = sum(1 for op in need if 200 <= _cn_char_len(op.get("cn_digest", "")) <= 300)
    log(f"[概括] 完成：生成 {ok}，降级 {len(need) - ok}，平均 {avg:.0f} 字，其中 {inrange}/{len(need)} 落在 200-300 区间")


def build_content(ops: list[dict]) -> str:
    today = now_cn().strftime("%Y-%m-%d")
    lines = [f"💰【赚钱机会】{len(ops)} 条 · {today}", ""]
    for i, op in enumerate(ops, 1):
        cat = op.get("cat") or "待判断"
        lines.append(f"{i}.【{cat}·{op['score']:.1f}分】{op.get('cn_title') or op['title']}")
        d = op.get("cn_digest", "")
        if d:
            lines.append(d)
        if op.get("how"):
            lines.append(f"💵怎么赚：{op['how']}")
        if op.get("link"):
            lines.append(f"🔗 {op['link']}")
        lines.append("")
    lines.append("筛选口径：冷门红利期 × 变现潜力 × 低风险（已滤加密/破解/灰产/教程/炫技项目）")
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

    enrich_chinese(fresh, os.environ.get("LLM_API_KEY", "").strip())

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