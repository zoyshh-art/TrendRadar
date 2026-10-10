# coding=utf-8
"""每日美图推送

四源配额混推（百度/360/Civitai-AI/必应），每源约 3 张风格混合 → PushPlus 微信服务号
（HTML 模板）推送。默认 10 张/次，只取竖图大图。

图源（均免 key）：
1. 百度图片 acjson —— 辣度词池（情趣内衣/黑丝/肉丝袜/内衣秀/比基尼/车模/JK/女仆/AI美女）
2. 360 图片 image.so.com —— 车模/比基尼类出量稳定（敏感词查询会被折叠）
3. Civitai 公开 API —— AI 生成性感图（Soft/Mature 两档，prompt 作标题），墙外源 runner 可达
4. 必应图片 async —— adlt=off 关闭安全搜索；标题带 purl 走内容闸

质量闸（防止工厂设备/菜谱/素材图等误配）：
- 标题/来源页命中黑名单（REJECT_PAT）或二次元（ANIME_PAT）→ 直接丢弃
- 肤色占比（skin_ratio）过低且标题无人像词 → 丢弃
- 标题无人像词 → 丢弃（证件照/男性头像等高肤色垃圾靠标题区分）
- 候选按 人像词得分 + 肤色占比 排序，配额混推后按分补满

- 选取：只取竖图（高>宽），按日期固定随机种子（同日重跑结果一致）
- 去重：output/pic_history.json 记录近期已推 URL
- 推送：PushPlus channel=wechat，template=html，content 用 <img>

环境变量：
- PUSHPLUS_TOKEN   必填，PushPlus 用户 token

用法：
  PUSHPLUS_TOKEN=xxx python scripts/daily_pic.py [--dry-run]
"""

import html as htmllib
import io
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

# Windows 控制台默认 GBK，打印含 emoji 的标题/内容会 UnicodeEncodeError → 强制 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ──────────────────────────── 配置 ────────────────────────────

NUM_IMAGES = 10                # 每次推送张数
HISTORY_KEEP = 300             # 历史保留条数（10 张/天 → 约 30 天不重复）
HISTORY_PATH = Path(__file__).resolve().parent.parent / "output" / "pic_history.json"

# Pexels 站内 Web API 的公开 Secret-Key（官网前端硬编码，非个人账号 key）
PEXELS_KEY = "H2jk9uKnhRmL6WPwh89zBezWvr"
PEXELS_QUERIES = [
    "sexy model photoshoot",
    "bikini model beach",
    "lingerie model portrait",
    "sensual woman portrait",
    "swimsuit model summer",
]

# 必应图片关键词池（大尺度写真；runner 侧以 adlt=off 关闭安全搜索）
# 注意避开歧义词（如“吊带”→工业吊装带、“肉丝”→菜谱），靠标题黑名单兜底
BING_QUERIES = [
    "情趣内衣 模特 写真",
    "黑丝 长腿 美女",
    "丝袜 美腿 写真",
    "内衣秀 超模",
    "比基尼 女神 写真",
    "车模 写真",
    "湿身 写真 美女",
    "私房 写真 性感",
]

# 百度图片关键词池（按日期轮换；风格混池：辣度向/丝袜向/泳装车模/JK制服/AI美女）
BAIDU_KEYWORDS = [
    "情趣内衣 写真",
    "黑丝 写真",
    "肉丝袜 写真",
    "丝袜 长腿 写真",
    "内衣秀 写真",
    "比基尼 写真",
    "泳装 性感 写真",
    "车模 写真",
    "JK制服 写真",
    "女仆装 写真",
    "湿身 写真 美女",
    "AI美女 写真",
]

# 360 图片关键词池（image.so.com；敏感词查询会被折叠成 1-2 条，只用实测能出量的词）
SO360_QUERIES = ["性感美女", "比基尼美女", "泳装美女", "车模", "美女"]

# 内容闸：标题/来源页命中即整条丢弃（机械工厂/工业吊装/科技硬件/菜谱/图表素材等误配图）
REJECT_PAT = re.compile(
    r"吊装|起重|机械|设备|工厂|车间|生产线|水泥|料斗|储罐|钢结构|齿轮|阀门|风机|水泵|机床|数控|"
    r"挖掘机|装载机|工程车|发动机|变速箱|汽车之家|轮胎|电路|芯片|主板|显卡|硬盘|内存条|硅表面|晶圆|钝化|"
    r"招股书|财报|股票|基金|K线|标识|logo|标志|矢量|psd|素材下载|千图网|昵图网|众图网|包图网|设计图|"
    r"青椒|京酱|菜谱|做法|家常菜|下饭|美食|食谱|烘焙|欧洲半岛|地图|地理|考试|试题|试卷|论文|专利|"
    r"公筷|公勺|文明用餐|文明就餐|就餐|公益|海报|小报|宣传画|宣传栏|模板|素材|文明城市|倡议书|"
    r"证件照|身份证|一寸照|两寸照|简历|形象照|职业照|毕业照|全家福|婚纱摄影|孕照|儿童摄影|"
    r"街拍|穿搭|ootd|无不良引导|正常穿搭|背影合集"
)
# 二次元/插画类（要真人）
ANIME_PAT = re.compile(
    r"动漫|二次元|插画|原画|同人|漫画|立绘|画集|赛马娘|虚拟主播|vtuber|pixiv|"
    r"原神|崩坏|碧蓝航线|明日方舟|手办|壁纸 原创"
)
# 人像正向词（命中越多越优先，也用于放宽肤色比下限；裸词“女”误伤面太大已去掉）
# 含英文词：Civitai(AI图，标题=prompt) / 必应英文标题用得上
PERSON_PAT = re.compile(
    r"美女|性感|写真|模特|比基尼|泳装|黑丝|肉丝|丝袜|美腿|长腿|内衣|情趣|私房|少妇|女神|"
    r"少女|女郎|大尺度|湿身|诱惑|尤物|维密|人像|车模|靓女|小姐姐|妹|"
    r"sexy|lingerie|bikini|swimsuit|woman|girl|nsfw|portrait|\bmodel\b"
)

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
    for q in queries:
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
    """拉取必应图片搜索结果，解析 iusc.m 属性拿自家缓存缩略图 turl

    adlt=off 关闭安全搜索：默认中度安全搜索会滤掉大尺度写真
    （国内 cn.bing 会无视该参数并降级结果，故本地试跑仅供参考，以 runner 为准）
    """
    w = urllib.parse.quote(query)
    session.cookies.set("SRCHHPGUSR", "ADLT=OFF", domain=".bing.com")
    try:
        r = session.get(
            "https://www.bing.com/images/async",
            params={"q": query, "first": "0", "count": "35", "relp": "35",
                    "mmasync": "1", "adlt": "off", "scenario": "ImageBasicHover",
                    "datsrc": "N_I", "layout": "RowBased"},
            headers={"Referer": f"https://www.bing.com/images/search?q={w}&adlt=off"},
            timeout=20,
        )
    finally:
        session.cookies.clear()
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
                    "fromPageTitleEnc": j.get("t") or "",
                    "_purl": j.get("purl") or "",
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


def pool_360(session: requests.Session, rng: random.Random, need: int) -> list[dict]:
    """360 图片（image.so.com 开放 JSON）：img 字段为大图，title 走内容闸"""
    words = SO360_QUERIES[:]
    rng.shuffle(words)
    pool, seen = [], set()
    for word in words[:4]:
        for pn in (0, 1):
            try:
                r = session.get(
                    "https://image.so.com/j",
                    params={"q": word, "pn": pn, "rn": 48, "src": "srp"},
                    headers={"Referer": "https://image.so.com/"}, timeout=20,
                )
                items = (r.json() or {}).get("list") or []
            except (requests.RequestException, ValueError) as e:
                log(f"  360搜索失败 {word}: {e}")
                items = []
            for it in items:
                img = it.get("img") or it.get("thumb") or ""
                if img.startswith("http://"):
                    img = "https://" + img[7:]
                if not img or img in seen:
                    continue
                w_, h_ = int(it.get("width") or 0), int(it.get("height") or 0)
                if w_ and h_ and h_ <= w_:
                    continue
                seen.add(img)
                pool.append({"thumbURL": img, "width": w_, "height": h_,
                             "fromPageTitleEnc": it.get("title") or "",
                             "_purl": it.get("link") or it.get("comm_purl") or "",
                             "source": "360图片",
                             "_aspect": (h_ / w_) if (w_ and h_) else 0})
            if len(pool) >= need * 4:
                break
        if len(pool) >= need * 4:
            break
    log(f"[360图片] 候选 {len(pool)} 张")
    return pool


def pool_civitai(session: requests.Session, rng: random.Random, need: int) -> list[dict]:
    """Civitai AI 生成图（公开 API，免 key）：Soft/Mature 两档，prompt 作标题走内容闸

    墙外源：本地大概率超时，runner(US) 可达；图床 image.civitai.com
    """
    pool, seen = [], set()
    for nsfw in ("Mature", "Soft"):
        try:
            r = session.get(
                "https://civitai.com/api/v1/images",
                params={"limit": 30, "sort": "Most Reactions", "period": "Day",
                        "nsfw": nsfw},
                timeout=25,
            )
            items = (r.json() or {}).get("items") or []
        except (requests.RequestException, ValueError) as e:
            log(f"  Civitai请求失败({nsfw}): {type(e).__name__}")
            items = []
        for it in items:
            u = it.get("url") or it.get("thumbnailUrl") or ""
            w_, h_ = int(it.get("width") or 0), int(it.get("height") or 0)
            if not u or u in seen:
                continue
            if w_ and h_ and h_ <= w_:        # 只要竖图
                continue
            prompt = str(((it.get("meta") or {}).get("prompt")) or "")[:80]
            if not prompt:
                # Civitai 服务端已按 nsfw=Soft/Mature 过滤，无 prompt 时用标签兜底
                # （AI 源豁免“无题闸”：内容本身已是目标向）
                prompt = "AI性感写真 lingerie sexy portrait"
            seen.add(u)
            pool.append({"thumbURL": u, "width": w_, "height": h_,
                         "fromPageTitleEnc": prompt,
                         "_purl": "https://civitai.com/",
                         "source": f"AI(Civitai·{nsfw})",
                         "_aspect": (h_ / w_) if (w_ and h_) else 0})
    log(f"[AI·Civitai] 候选 {len(pool)} 张")
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


def skin_ratio(data: bytes) -> float | None:
    """HSV 肤色占比：真人写真显著高于工厂/器材/图表类图（0.0~1.0）

    无 Pillow 或解码失败时返回 None（跳过该信号，不影响主流程）
    """
    try:
        from PIL import Image, ImageChops
    except ImportError:
        return None
    try:
        im = Image.open(io.BytesIO(data)).convert("RGB").resize((80, 80))
        h_, s_, v_ = im.convert("HSV").split()
        mh = h_.point(lambda x: 255 if (x <= 34 or x >= 232) else 0)
        ms = s_.point(lambda x: 255 if 30 <= x <= 180 else 0)
        mv = v_.point(lambda x: 255 if x >= 60 else 0)
        mask = ImageChops.multiply(ImageChops.multiply(mh, ms), mv)
        return mask.histogram()[255] / (80 * 80)
    except Exception:
        return None


def verify_image(url: str, timeout: int = 20) -> tuple[bool, tuple | None, bytes | None]:
    """模拟微信 <img> 加载：下载整图确认可用，解析尺寸并回传内容（供肤色比计算）"""
    try:
        r = requests.get(url, headers=MOBILE_UA, timeout=timeout)
        ct = r.headers.get("Content-Type", "")
        if r.status_code != 200 or "image" not in ct or len(r.content) < 1000:
            return False, None, None
        return True, _image_size(r.content), r.content
    except requests.RequestException:
        return False, None, None


def text_gate(it: dict) -> int | None:
    """标题/来源页内容闸：命中黑名单/二次元 → None；返回人像词得分"""
    title = str(it.get("fromPageTitleEnc") or "")
    blob = title + " " + str(it.get("_purl") or "")
    if re.search(r"https?://", title):      # 标题里夹垃圾推广链接
        return None
    if REJECT_PAT.search(blob):
        return None
    if ANIME_PAT.search(title):
        return None
    return len(PERSON_PAT.findall(title))


def _verify_candidate(src_name: str, it: dict) -> dict | None:
    """下载校验单张：可用性/竖图/肤色闸/无题闸/分数闸，通过则返回带 _score 的 it"""
    ok, dims, content = verify_image(it["thumbURL"])
    if not ok:
        return None
    if it.get("source") == "必应图片" and not it.get("width"):
        # 必应无尺寸元数据：实测尺寸把关竖图，并换成放大版 URL
        if dims is None or dims[1] <= dims[0]:
            return None
        it["width"], it["height"] = dims
        if dims[0] < 768:
            base = it["thumbURL"]
            big = base + ("&" if "?" in base else "?") + \
                f"w=768&h={round(768 * dims[1] / dims[0])}&rs=1&pid=ImgDetMain"
            ok2, dims2, _ = verify_image(big)
            if ok2:
                it["thumbURL"] = big
                if dims2 and dims2[1] > dims2[0]:
                    it["width"], it["height"] = dims2
    elif dims and not it.get("width"):
        it["width"], it["height"] = dims
    # 肤色占比闸：无人像词 + 肤色过低 = 大概率不是人物图（工厂/器材/图表）
    skin = skin_ratio(content) if content else None
    it["_skin"] = skin
    if skin is not None and skin < 0.06 and it["_score"] == 0:
        log(f"  [{src_name}] 肤色闸丢弃 skin={skin:.2f} {it['thumbURL'][:60]}")
        return None
    # 标题无人像词一律不要：证件照/男性头像等高肤色垃圾图靠标题区分
    if it["_score"] == 0:
        log(f"  [{src_name}] 无题闸丢弃 {str(it.get('fromPageTitleEnc'))[:30]} "
            f"{it['thumbURL'][:50]}")
        return None
    it["_score"] += (skin or 0) * 1.5
    # 综合分下限：标题人像词太少且肤色一般 = 审查降级/垃圾图
    if it["_score"] < 1.2:
        log(f"  [{src_name}] 分数闸丢弃 score={it['_score']:.2f} "
            f"{str(it.get('fromPageTitleEnc'))[:30]}")
        return None
    return it


def _verified_pool(src_name: str, pool: list[dict], history: set, rng: random.Random) -> list[dict]:
    """内容闸 → 下载校验 → 源内按综合分（人像词 + 肤色占比）降序"""
    fresh = [it for it in pool if it["thumbURL"] not in history]
    # 已推过的不重复用（除非该源实在没有新图）
    fresh = fresh or pool
    gated = []
    for it in fresh:
        score = text_gate(it)
        if score is None:
            continue
        it["_score"] = score
        gated.append(it)
    if len(gated) < len(fresh):
        log(f"  [{src_name}] 内容闸丢弃 {len(fresh) - len(gated)} 条（误配/二次元）")
    rng.shuffle(gated)
    verified: list[dict] = []
    for it in gated:
        if len(verified) >= NUM_IMAGES * 2:      # 单源验证上限，够用即可
            break
        v = _verify_candidate(src_name, it)
        if v is not None:
            verified.append(v)
    verified.sort(key=lambda x: x["_score"], reverse=True)
    return verified


def pick_images(session: requests.Session, rng: random.Random) -> list[dict]:
    """四源配额混推：每源先出 ~3 张（风格混合），不够再按分补满

    百度（辣度词池）+ 360（车模/比基尼）+ Civitai（AI 生成性感图）+ 必应（adlt=off）
    """
    day_idx = date.today().toordinal()
    builders_all = [
        ("百度", lambda: pool_baidu(session, rng, NUM_IMAGES)),
        ("360", lambda: pool_360(session, rng, NUM_IMAGES)),
        ("AI", lambda: pool_civitai(session, rng, NUM_IMAGES)),
        ("必应", lambda: pool_bing(session, rng, NUM_IMAGES)),
    ]
    rot = day_idx % len(builders_all)
    builders = builders_all[rot:] + builders_all[:rot]
    log(f"今日主图源: {builders[0][0]}（配额混推：每源约 {max(3, NUM_IMAGES // len(builders_all))} 张）")

    history = load_history()
    per_src = max(3, NUM_IMAGES // len(builders_all))
    by_src: list[tuple[str, list[dict]]] = []
    for src_name, build in builders:
        by_src.append((src_name, _verified_pool(src_name, build(), history, rng)))

    chosen: list[dict] = []

    def _try_add(it: dict) -> bool:
        if len(chosen) >= NUM_IMAGES:
            return False
        if any(c["thumbURL"] == it["thumbURL"] for c in chosen):
            return True
        chosen.append(it)
        log(f"  选定 [{it.get('source')}] {it.get('width')}x{it.get('height')} "
            f"score={it['_score']:.2f} {it['thumbURL'][:70]}")
        return True

    # 配额 pass：每源头部若干张 → 一次推送混风格
    for _, verified in by_src:
        for it in verified[:per_src]:
            if not _try_add(it):
                break
    # 补满 pass：剩余候选按综合分降序
    rest = [it for _, v in by_src for it in v[per_src:]]
    rest.sort(key=lambda x: x["_score"], reverse=True)
    for it in rest:
        if not _try_add(it):
            break
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
        f'<span style="font-size:18px;font-weight:bold;">📸 【美图】今日写真集 · {today}</span></div>',
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
    title = f"【美图】性感写真 {NUM_IMAGES} 张 · {date.today().isoformat()}"

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
