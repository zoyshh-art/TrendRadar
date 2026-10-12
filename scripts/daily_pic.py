# coding=utf-8
"""每日美图推送

全 AI 生成十张打头（文生图 prompt 控尺度）→ PushPlus 微信服务号（HTML 模板）推送。
默认 10 张/次，只取竖图大图。

图源：
1. 文生图 flux（主力 10/10）—— Pollinations 免 key API，prompt 控制尺度与质量
   （情趣内衣/蕾丝/湿身/私房/纯欲等写真实拍风格；生成 URL 即图床）
   匿名档限流（HTTP 402）：图片间隔 8-14s + 退避重试；连续失败自动回退下列源
2. AI 写真 —— 百度搜 AI 词（生成不足时补位）
3. 百度图片 acjson —— 真人辣度词池（情趣内衣/黑丝/肉丝袜/内衣秀/比基尼/车模/JK/女仆）
4. 360 图片 image.so.com —— 车模/比基尼类出量稳定（敏感词查询会被折叠）
5. 必应图片 async —— adlt=off 关闭安全搜索；对数据中心 IP 间歇反爬，自动兜底

质量闸（防止工厂设备/菜谱/素材图/水印图等误配）：
- 标题/来源页命中黑名单（REJECT_PAT，含抖音/快手水印平台）或二次元（ANIME_PAT）→ 直接丢弃
- 肤色占比（skin_ratio）过低且标题无人像词 → 丢弃
- 标题无人像词 → 丢弃（证件照/男性头像等高肤色垃圾靠标题区分）
- 候选按 人像词得分 + 肤色占比 + 尺寸（≥1000px 加分）排序，配额出图后按分补满

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
# 生成图库存池：现场生成的图 URL 入库持久化（URL 永久有效），
# 文生图接口限流（HTTP 402）时用库存兜底，库存富余后推送 0 生成请求
GEN_BANK_PATH = Path(__file__).resolve().parent.parent / "output" / "gen_bank.json"
GEN_BANK_KEEP = 40             # 库存保留条数（已目检的优先保留，淘汰先动未目检的）

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
# murl 原图直链实测可达 1024~2048px（多来自 sinaimg.cn 等墙内 CDN）
# 注意避开歧义词（如“吊带”→工业吊装带、“肉丝”→菜谱），靠标题黑名单兜底
BING_QUERIES = [
    "私房写真 性感模特",
    "性感尤物 私房 写真 大胆",
    "黑丝 长腿 写真 性感",
    "丝袜 高跟 写真 性感 模特",
    "湿身 写真 美女",
    "比基尼 写真 模特 性感",
    "睡衣 吊带 写真 性感 美女",
    "吊带裙 写真 模特 高清",
]

# AI 生成图关键词池（百度图源，国内 CDN 可达；用户偏好 AI 图 → 配额最高 5/10）
AI_WORDS = [
    "AI美女 写真",
    "AI性感写真",
    "AI黑丝丝袜 美女",
    "AI比基尼 美女",
    "AI辣妹 写真",
    "AI性感美女",
    "AI女仆 写真",
    "AI泳装美女",
    "AI美女 私房",
]

# 百度图片关键词池（真人写真，按日期轮换；风格混池：辣度向/丝袜向/泳装车模/JK制服）
BAIDU_KEYWORDS = [
    "情趣内衣 写真",
    "黑丝 写真",
    "丝袜 长腿 写真",
    "私房 性感 写真",
    "睡衣 写真 性感",
    "吊带 写真 美女",
    "比基尼 写真",
    "泳装 性感 写真",
    "车模 写真",
    "湿身 写真 美女",
]

# 360 图片关键词池（image.so.com；敏感词查询会被折叠成 1-2 条，只用实测能出量的词）
SO360_QUERIES = ["AI美女", "性感美女", "比基尼美女", "泳装美女", "车模", "美女"]

# 内容闸：标题/来源页命中即整条丢弃（机械工厂/工业吊装/科技硬件/菜谱/图表素材等误配图）
REJECT_PAT = re.compile(
    r"吊装|起重|机械|设备|工厂|车间|生产线|水泥|料斗|储罐|钢结构|齿轮|阀门|风机|水泵|机床|数控|"
    r"挖掘机|装载机|工程车|发动机|变速箱|汽车之家|轮胎|电路|芯片|主板|显卡|硬盘|内存条|硅表面|晶圆|钝化|"
    r"招股书|财报|股票|基金|K线|标识|logo|标志|矢量|psd|素材下载|千图网|昵图网|众图网|包图网|设计图|"
    r"青椒|京酱|菜谱|做法|家常菜|下饭|美食|食谱|烘焙|欧洲半岛|地图|地理|考试|试题|试卷|论文|专利|"
    r"公筷|公勺|文明用餐|文明就餐|就餐|公益|海报|小报|宣传画|宣传栏|模板|素材|文明城市|倡议书|"
    r"证件照|身份证|一寸照|两寸照|简历|形象照|职业照|毕业照|全家福|婚纱摄影|孕照|儿童摄影|"
    r"街拍|穿搭|ootd|无不良引导|正常穿搭|背影合集|"
    r"抖音|快手|直播|水印|美女直播|游戏主播|"
    # 露骨内容闸（真人源标题有信息量，可据此拦；AI 生成标题无效，靠人工目检）
    r"全裸|裸体|裸照|赤裸|裸身|色情|情色|无码|一丝不挂|敏感部位|性暗示|做爱|性爱|床照情|"
    r"nsfw|uncensored|福利姬|露出胸|走光裙|"
    # 色情站垃圾（实测必应"黑丝"词会返回此类：肛交/深喉/约炮…），一律拦
    r"肛交|口交|深喉|做愛|性交|裸聊|自慰|约炮|一夜情|偷拍|偷窥|乱伦|调教|成人影片|无码影片|"
    r"色情网|色情小说|av女优|AV女优|无码区|约炮视频|成人视频|情色视频|做视频|gv视频|资源合集|"
    # 商品页/内容农场（丝袜平铺、穿搭搭配文章——既不自然也没尺度）
    r"阿里巴巴|京东|淘宝|天猫|1688|值得买|smzdm|桔子时尚|juziss|知乎|zhuanlan|搭配|显瘦|显高|"
    r"怎么穿|包邮|促销|优惠券|批发|厂家|平铺|三角裤|文胸|连裤袜|大腿袜|吊带袜|内裤批发|"
    # JK/校园主题（国内平台明确违规擦边，且非用户诉求）
    r"jk|JK|水手服|校服|校园|校花|学生妹|中学生|高中|大学妹|萝莉|小学生"
)
# 垃圾来源域名（电商商品图/内容农场文章图）
BAD_HOST_PAT = re.compile(
    r"zhimg\.com|zdmimg\.com|juziss\.com|smzdm\.com|cbu01\.alicdn\.com|"
    r"detail\.tmall\.com|item\.jd\.com|zhuanlan\.zhihu|taobao\.com/list|1688\.com"
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


def pool_ai(session: requests.Session, rng: random.Random, need: int) -> list[dict]:
    """AI 生成图：百度搜 AI 词（国内 CDN 可达，微信内不裂图）

    图源为各平台 AI 画师作品（搜狐/小红书/图虫等转载），标题自带"AI美女"标签
    """
    words = AI_WORDS[:]
    rng.shuffle(words)
    pool, seen = [], set()
    for word in words[:5]:
        for pn in (0, 30, 60):
            try:
                items = baidu_search(session, word, pn=pn)
            except requests.RequestException as e:
                log(f"  AI搜索失败 {word} pn={pn}: {type(e).__name__}")
                continue
            for it in items:
                u = it.get("thumbURL", "")
                w, h = it.get("width") or 0, it.get("height") or 0
                if not u.startswith("https://") or u in seen:
                    continue
                if not (h > w >= 600):       # 只要竖图大图
                    continue
                if it.get("type") in ("gif", "webp"):
                    continue
                seen.add(u)
                it["source"] = "AI写真"
                pool.append(it)
            if len(pool) >= need * 4:
                break
        if len(pool) >= need * 4:
            break
    log(f"[AI写真] 候选 {len(pool)} 张")
    return pool


# ──────────────────────── 图源三：必应图片 ────────────────────────

def _query_keywords(query: str) -> list[str]:
    """从查询词拆出相关性关键词：标题/来源页至少命中一个，否则视为无关结果丢弃"""
    toks = [t for t in query.split() if len(t) >= 2]
    return toks or [query]


def bing_fetch(session: requests.Session, query: str) -> list[dict]:
    """拉取必应图片搜索结果，解析 iusc.m 属性：优先取原图直链 murl，退化才用缓存缩略图 turl

    adlt=off 关闭安全搜索：默认中度安全搜索会滤掉大尺度写真
    murl 实测可达 1024~2048px 竖图原图（多来自 sinaimg.cn 等墙内 CDN）→ 尺寸问题的解
    端点：cn.bing.com 更稳（www 端点偶发不理会 query 返回默认流，如返回医学图）
    """
    w = urllib.parse.quote(query)
    hosts = ("https://cn.bing.com/images/async", "https://www.bing.com/images/async")
    r = None
    for host in hosts:
        session.cookies.set("SRCHHPGUSR", "ADLT=OFF", domain=".bing.com")
        try:
            r = session.get(
                host,
                params={"q": query, "first": "0", "count": "35", "relp": "35",
                        "mmasync": "1", "adlt": "off", "scenario": "ImageBasicHover",
                        "datsrc": "N_I", "layout": "RowBased"},
                headers={"Referer": f"https://{urllib.parse.urlparse(host).netloc}/images/search?q={w}&adlt=off"},
                timeout=20,
            )
        except requests.RequestException:
            continue
        finally:
            session.cookies.clear()
        if r.status_code == 200 and len(r.text) >= 5000:
            break
    if r is None or r.status_code != 200 or len(r.text) < 5000:
        raise requests.RequestException(f"bing 返回异常 status={r.status_code if r is not None else 'None'} len={len(r.text) if r is not None else 0}")
    out = []
    for ma in re.findall(r'm="(\{[^"]+\})"', r.text):
        try:
            j = json.loads(htmllib.unescape(ma))
        except ValueError:
            continue
        t = j.get("t") or ""
        purl = j.get("purl") or ""
        # 查询相关性：标题/来源页必须命中 query 的核心词（防默认流/无关图）
        if not any(kw in t or kw in purl for kw in _query_keywords(query)):
            continue
        murl = j.get("murl") or ""
        turl = j.get("turl") or ""
        mw, mh = j.get("mw") or 0, j.get("mh") or 0
        use_murl = murl.startswith("https://")
        use_turl = (not use_murl) and turl.startswith("https://")
        if not (use_murl or use_turl):
            continue
        if mw and mh and mh <= mw:        # 有尺寸信息时先过滤非竖图
            continue
        url = murl if use_murl else turl
        host = urllib.parse.urlparse(url).netloc.lower()
        out.append({"thumbURL": url, "width": mw, "height": mh,
                    "fromPageTitleEnc": j.get("t") or "",
                    "_purl": j.get("purl") or "",
                    "source": "必应图片",
                    "_is_turl": (not use_murl),
                    "_host": host,
                    "_aspect": (mh / mw) if (mw and mh) else 0})
    return out


# 墙内优先（微信加载快/不裂）：命中的域排在前面
_CN_HOSTS = ("sinaimg.cn", "qpic.cn", "huanqiu.com", "baidu.com", "so.com",
             "sogou.com", "qq.com", "163.com", "zcool.com.cn", "tuchong.com",
             "photophoto.cn", "nipic.com", "16pic.com", "aliimg.com", "taobao.com")


def _cn_first(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda it: 0 if any(h in it.get("_host", "") for h in _CN_HOSTS) else 1)


def pool_bing(session: requests.Session, rng: random.Random, need: int) -> list[dict]:
    queries = BING_QUERIES[:]
    rng.shuffle(queries)
    pool, seen = [], set()
    for q in queries[:6]:                      # 必应偶发返回默认流/垃圾流，多跑几个词直到出够量
        if len(pool) >= need * 3:
            break
        for attempt in range(3):
            try:
                items = bing_fetch(session, q)
                break
            except requests.RequestException as e:
                log(f"  必应搜索失败 {q} 第{attempt + 1}次: {e}")
                items = []
                time.sleep(2 + attempt * 3)
        items = _cn_first(items)
        n_murl = 0
        for it in items:
            if it["thumbURL"] in seen:
                continue
            aspect = it.pop("_aspect", 0)
            if it.get("_is_turl") and aspect:
                # 仅缓存缩略图走放大参数；murl 原图不能拼参数（会破坏 URL）
                it["thumbURL"] += ("&" if "?" in it["thumbURL"] else "?") + \
                    f"w=768&h={round(768 * aspect)}&rs=1&pid=ImgDetMain"
            if not it.get("_is_turl"):
                n_murl += 1
            seen.add(it["thumbURL"])
            pool.append(it)
        if len(pool) >= need * 4:
            break
    log(f"[必应图片] 候选 {len(pool)} 张")
    return pool


# 文生图关键词池（flux 模型）：(中文标签, 英文 prompt 主体)
# prompt 即尺度：情趣/蕾丝/湿身/私房/纯欲 等写真实拍风格
GEN_STYLES = [
    ("情趣内衣写真", "beautiful young woman wearing a black lace lingerie set, soft studio lighting, alluring gaze, photorealistic portrait"),
    ("蕾丝吊带", "beautiful young woman wearing a sheer lace camisole and matching shorts, warm bedroom lighting, photorealistic portrait"),
    ("湿身诱惑", "beautiful young woman wearing a bikini, wet hair and wet skin, water droplets on skin, dramatic lighting, photorealistic portrait"),
    ("纯欲风", "beautiful young woman wearing a silk slip dress, pure yet alluring mood, soft focus, pastel tones, photorealistic portrait"),
    ("私房慵懒", "beautiful young woman wearing an oversized white shirt, cozy bedroom, lazy alluring expression, photorealistic portrait"),
    ("泳装池畔", "attractive young woman wearing a bikini beside a swimming pool, summer sunlight, wet skin, photorealistic portrait"),
    ("黑丝御姐", "elegant woman wearing a black mini dress with black stockings, sitting pose, alluring look, photorealistic portrait"),
    ("浴室写真", "beautiful young woman wearing a bath towel, wet hair, steamy bathroom, soft steam light, photorealistic portrait"),
    ("红色礼服", "beautiful young woman wearing a form-fitting red evening dress, dim ambient light, cinematic, photorealistic portrait"),
    ("少女心事", "young woman wearing lingerie lying on a bed, innocent yet alluring expression, soft morning light, photorealistic portrait"),
    ("镜前自拍", "beautiful young woman taking a mirror selfie wearing a fitted tank top and underwear, alluring look, photorealistic portrait"),
    ("午后窗台", "beautiful young woman wearing a summer dress by a sunlit window, backlit curves, photorealistic portrait"),
    ("晚安睡衣", "beautiful young woman wearing a satin slip dress, bedside lamp warm light, soft alluring expression, photorealistic portrait"),
]
GEN_MODIFIERS = [
    "85mm lens, shallow depth of field",
    "cinematic color grading, high detail skin texture",
    "professional photoshoot, rim lighting",
    "soft bokeh background, sharp focus on face",
    "medium shot, natural pose",
]
# 所有 prompt 追加：自然解剖结构（缓解 AI 肢体/比例不自然）
# + 穿衣/非露骨硬约束（flux 见 "wet skin/steam/loose shirt/bed" 极易滑向裸体，
#   实测出现过站立全裸；skin_ratio 无法区分裸体与内衣，skin 0.54 裸体 < 0.85 内衣，
#   故只能靠 prompt 层面硬约束 + 人工目检）
GEN_ANATOMY = ("anatomically correct, natural body proportions, realistic pose, "
               "fully clothed, non-explicit, no nudity")
GEN_API = "https://image.pollinations.ai/prompt"


def load_gen_bank() -> list[dict]:
    """读生成图库存池（持久化在图源仓 output/ 下，随每日回写）"""
    try:
        data = json.loads(GEN_BANK_PATH.read_text(encoding="utf-8"))
        items = data.get("items", [])
        return [it for it in items if isinstance(it, dict) and it.get("thumbURL")]
    except (OSError, ValueError):
        return []


def save_gen_bank(items: list[dict]) -> None:
    """写库存：超量时先淘汰未目检（vetted 缺失为假）的条目，已目检的最后动"""
    if len(items) > GEN_BANK_KEEP:
        vetted = [i for i in items if i.get("vetted")]
        raw = [i for i in items if not i.get("vetted")]
        keep_raw = max(0, GEN_BANK_KEEP - len(vetted))
        keep_ids = {id(x) for x in vetted} | {id(x) for x in raw[-keep_raw:]}
        items = [i for i in items if id(i) in keep_ids]
    GEN_BANK_PATH.parent.mkdir(parents=True, exist_ok=True)
    GEN_BANK_PATH.write_text(
        json.dumps({"items": items,
                    "updated": datetime.now().isoformat(timespec="seconds")},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def _bank_append(item: dict) -> None:
    """单张入库（去重 + 限量裁剪）"""
    try:
        bank = load_gen_bank()
        if item["thumbURL"] not in {b["thumbURL"] for b in bank}:
            bank.append(item)
            save_gen_bank(bank)
    except Exception as e:
        log(f"  [库存池] 写入失败（不影响推送）: {e}")


def pool_gen(session: requests.Session, rng: random.Random, need: int,
             bank: bool = True) -> list[dict]:
    """文生图源（flux 免 key）：prompt 控尺度，真人级写真质量

    - 匿名档有限流（HTTP 402）：图片间 8-14s 随机间隔 + 退避重试，连续失败即停
    - 生成 URL 即图床（Cloudflare CDN，微信内可直接 <img> 加载）
    - 每张生成即下载校验（防 402 HTML 页伪装成图）
    - bank=False 时不写入库存池（--dry-run 试跑用，避免白烧配额）
    """
    styles = GEN_STYLES[:]
    rng.shuffle(styles)
    want = min(len(styles), need + 3)   # 多备几张给校验/补满
    out: list[dict] = []
    fail_streak = 0
    for label, prompt in styles:
        if len(out) >= want:
            break
        if fail_streak >= 3:
            log("  [AI生成] 连续失败，停止生成（限流或接口异常），回退既有图源")
            break
        url = ""
        real_dims = (1024, 1280)
        for attempt in range(3):
            seed = rng.randrange(1, 10 ** 7)
            body = f"{prompt}, {rng.choice(GEN_MODIFIERS)}, {GEN_ANATOMY}"
            q = urllib.parse.quote(body)
            url = (f"{GEN_API}/{q}?width=1024&height=1280&nologo=true"
                   f"&model=flux&seed={seed}")
            try:
                r = requests.get(url, headers=MOBILE_UA, timeout=60)
                if r.status_code == 200 and "image" in r.headers.get("Content-Type", "") \
                        and len(r.content) > 8000:
                    # 免 key 档会降级实际尺寸：按真实像素回填（打分/展示更诚实）
                    dims = _image_size(r.content)
                    if dims:
                        real_dims = dims
                    break
                url = ""
                wait = 15 * (attempt + 1)
                log(f"  [AI生成] HTTP {r.status_code}（{attempt + 1}/3 次），{wait}s 后重试")
                time.sleep(wait)
            except requests.RequestException as e:
                url = ""
                wait = 15 * (attempt + 1)
                log(f"  [AI生成] 请求失败 {type(e).__name__}（{attempt + 1}/3 次），{wait}s 后重试")
                time.sleep(wait)
        if not url:
            fail_streak += 1
            continue
        fail_streak = 0
        out.append({
            "thumbURL": url,
            "width": real_dims[0],
            "height": real_dims[1],
            "fromPageTitleEnc": f"AI生成写真 · {label}",
            "source": "AI生成",
            "vetted": False,        # 未目检：定时任务不会推，需人工目检后标记（scripts/pic_vet.py）
        })
        log(f"  [AI生成] {label} seed 就绪（入库）" if bank else f"  [AI生成] {label} seed 就绪")
        if bank:
            _bank_append(out[-1])      # 逐张入库，中断不丢
        time.sleep(rng.uniform(8, 14))
    return out


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
                if w_ and (w_ < 600 or h_ <= w_):
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


def _image_size(data: bytes):
    try:
        from PIL import Image
        import io as _io
        im = Image.open(_io.BytesIO(data))
        return im.size
    except Exception:
        return None


# ─────────────── NSFW 自动闸（无人值守安全推的前提）───────────────
# 免 token：Hugging Face 公开 Space（Falconsai/nsfw_image_detection）匿名调用
# 有 HF_TOKEN：走 api-inference 直连模型
# 判别器不可用时 fail-closed（丢弃），宁可不推也不盲推——裸图已漏过两次
NSFW_MODEL = "Falconsai/nsfw_image_detection"
NSFW_SPACE = "Falconsai/nsfw_image_detection"
_nsfw_client = None
_nsfw_broken = False


def nsfw_check_enabled() -> bool:
    return (os.environ.get("NSFW_CHECK", "on").strip().lower()
            not in ("off", "0", "false", "no", "none"))


def nsfw_fail_open() -> bool:
    return (os.environ.get("NSFW_FAIL_OPEN", "").strip().lower()
            in ("1", "true", "on", "yes"))


def nsfw_label(image_bytes) -> str:
    """返回 normal / nsfw / unknown（判别器不可用→unknown，由上层决定丢弃或放行）"""
    global _nsfw_client, _nsfw_broken
    if _nsfw_broken or not image_bytes:
        return "unknown"
    token = os.environ.get("HF_TOKEN", "").strip()
    try:
        if token:
            r = requests.post(
                f"https://api-inference.huggingface.co/models/{NSFW_MODEL}",
                headers={"Authorization": f"Bearer {token}"},
                data=image_bytes, timeout=25)
            if r.status_code == 200:
                lab = (r.json() or [{}])[0] or {}
                return "normal" if str(lab.get("label", "")).lower() == "normal" else "nsfw"
            raise requests.RequestException(f"api-inference {r.status_code}: {r.text[:80]}")
        # 免 token：公开 Space
        if _nsfw_client is None:
            from gradio_client import Client
            _nsfw_client = Client(NSFW_SPACE, verbose=False)
        from PIL import Image
        import io as _io
        out = _nsfw_client.predict(Image.open(_io.BytesIO(image_bytes)))
        txt = json.dumps(out, ensure_ascii=False).lower()
        if "normal" in txt:
            return "normal"
        if "nsfw" in txt:
            return "nsfw"
        return "unknown"
    except Exception as e:
        log(f"  [NSFW] 判别器不可用（{'fail-closed 丢弃' if not nsfw_fail_open() else 'fail-open 放行'}）: "
            f"{type(e).__name__}: {str(e)[:90]}")
        _nsfw_broken = True
        return "unknown"


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
    if BAD_HOST_PAT.search(blob):          # 电商商品图/内容农场
        return None
    if REJECT_PAT.search(blob):
        return None
    # AI 生成图来源（标题带 AI 关键词）豁免二次元闸：AI 图允许动漫风格
    is_ai = bool(re.search(r"\bAI\b|AI美女|AI写真|AI性感|二次元|anime", title, re.IGNORECASE))
    if ANIME_PAT.search(title) and not is_ai:
        return None
    return len(PERSON_PAT.findall(title))


def _verify_candidate(src_name: str, it: dict) -> dict | None:
    """下载校验单张：可用性/竖图/肤色闸/无题闸/分数闸，通过则返回带 _score 的 it"""
    ok, dims, content = verify_image(it["thumbURL"])
    if not ok:
        return None
    if it.get("source") == "必应图片" and not it.get("width"):
        # 必应无尺寸元数据：实测尺寸把关竖图；仅缓存缩略图才拼放大参数（murl 是原图，拼接会坏 URL）
        if dims is None or dims[1] <= dims[0]:
            return None
        it["width"], it["height"] = dims
        if it.get("_is_turl") and dims[0] < 768:
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
    # 尺寸加分：高清图优先
    if (it.get("width") or 0) >= 1000:
        it["_score"] += 0.8
    # 留存原始字节给 NSFW 判别器（推送前会被 pop 掉，不进消息）
    if content:
        it["_content"] = content
    return it


def _verified_pool(src_name: str, pool: list[dict], history: set, rng: random.Random,
                   max_verify: int = 0) -> list[dict]:
    """内容闸 → 下载校验 → 源内按综合分（人像词 + 肤色占比 + 尺寸）降序"""
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
    cap = max_verify or NUM_IMAGES * 2      # 单源验证上限，够用即可
    for it in gated:
        if len(verified) >= cap:
            break
        v = _verify_candidate(src_name, it)
        if v is None:
            continue
        # NSFW 自动闸（无人值守的安全前提）：nsfw 丢弃；判别器不可用 → fail-closed 丢弃
        data = v.pop("_content", None)
        if nsfw_check_enabled():
            label = nsfw_label(data)
            if label == "nsfw":
                log(f"  [{src_name}] NSFW闸丢弃 {str(it.get('fromPageTitleEnc'))[:30]} "
                    f"{it['thumbURL'][:50]}")
                continue
            if label == "unknown" and not nsfw_fail_open():
                log(f"  [{src_name}] NSFW判别不可用→保守丢弃 "
                    f"{str(it.get('fromPageTitleEnc'))[:24]}")
                continue
        verified.append(v)
    verified.sort(key=lambda x: x["_score"], reverse=True)
    return verified


def pick_images(session: requests.Session, rng: random.Random, bank: bool = True,
                vetted_only: bool = True) -> list[dict]:
    """选图。默认 vetted_only=True：只推"已目检"库存图（定时任务安全策略）

    - vetted_only：仅取库存中 vetted=true 且未推过的图；不足则现场生成补库存
      （新图 vetted=false，只入库不推，等人工目检后再放行）；不碰真人兜底源
    - 非 vetted_only（手动 --allow-raw）：库存优先 + 现场生成 + AI图/真人源兜底
    """
    history = load_history()
    all_bank = [it for it in load_gen_bank() if it["thumbURL"] not in history]
    vetted = [it for it in all_bank if it.get("vetted")]
    log(f"生成图库存：可推未推 {len(all_bank)} 张，其中已目检 {len(vetted)} 张"
        f"（历史已推 {len(history)} 条）")

    if vetted_only:
        need_live = max(0, NUM_IMAGES - len(vetted))
        builders = [("AI生成·已目检", lambda: vetted, len(vetted))]
        if need_live > 0:
            # 现场生成只补库存（vetted=false），供后续人工目检放行
            log(f"已目检库存不足 {NUM_IMAGES} 张，现场生成 {need_live} 张补库存（不推，等目检）")
            builders.append(("AI生成·补库", lambda: pool_gen(session, rng, need_live, bank=bank), 0))
        log("定时策略：只推已目检库存图（无自动 NSFW 闸门，目检是唯一防线）")

        by_src = [(name, quota, _verified_pool(name, build(), history, rng, max_verify=quota))
                  for name, build, quota in builders]
        chosen: list[dict] = []
        for _, quota, verified in by_src:
            # 严格按配额取：补库那路 quota=0，只入库不推（防止未目检图泄漏进推送）
            for it in verified[:quota]:
                if len(chosen) >= NUM_IMAGES:
                    break
                chosen.append(it)
                log(f"  选定 [已目检] {it.get('fromPageTitleEnc')} {it['thumbURL'][:60]}")
        if len(chosen) < NUM_IMAGES:
            log(f"仅 {len(chosen)} 张已目检库存可推（不足 10 张属正常，等目检放行后补）")
        return chosen

    # ---- 手动全源模式（--allow-raw）：真人原图为主，AI 已目检库存点缀 ----
    # 必应 murl 原图（1024~2048px，墙内 CDN 优先）+ 百度 500px + 360 兜底 + AI 库存 ≤2
    builders = [
        ("必应原图", lambda: pool_bing(session, rng, NUM_IMAGES), 7),
        ("百度图片", lambda: pool_baidu(session, rng, NUM_IMAGES), 2),
        ("360图片", lambda: pool_360(session, rng, NUM_IMAGES), 1),
        ("AI生成·已目检", lambda: vetted, 1),
    ]
    log("手动全源模式：真人原图为主（必应 7 + 百度 1 + 360 1）+ 已目检 AI ≤1")

    by_src: list[tuple[str, int, list[dict]]] = []
    for src_name, build, quota in builders:
        by_src.append((src_name, quota,
                       _verified_pool(src_name, build(), history, rng,
                                      max_verify=max(quota * 3, 12))))

    chosen: list[dict] = []
    picked_titles: set[str] = set()

    def _try_add(it: dict) -> bool:
        if len(chosen) >= NUM_IMAGES:
            return False
        if any(c["thumbURL"] == it["thumbURL"] for c in chosen):
            return True
        # 同标题去重（同组摄影常有多个近似图，标题一模一样）
        tkey = re.sub(r"[\s|_\-]+", "", str(it.get("fromPageTitleEnc") or ""))[:18]
        if tkey and tkey in picked_titles:
            return True
        if tkey:
            picked_titles.add(tkey)
        chosen.append(it)
        log(f"  选定 [{it.get('source')}] {it.get('width')}x{it.get('height')} "
            f"score={it['_score']:.2f} {it['thumbURL'][:70]}")
        return True

    # 配额 pass：每源按配额出图（AI 打头）
    for _, quota, verified in by_src:
        for it in verified[:quota]:
            if not _try_add(it):
                break
    # 补满 pass：剩余候选按综合分降序
    rest = [it for _, q, v in by_src for it in v[q:]]
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
        url = it.get("thumbURL", "")
        if "pollinations.ai" in url:
            # AI 生成图：外层 overflow 裁角 + 1.12 倍居中放大，
            # 把右下角 pollinations 水印（横向82%-98%、纵向95.5%到底）整体顶出可视区
            parts.append(
                '<div style="overflow:hidden;border-radius:8px;">'
                f'<img src="{url}" style="width:100%;border-radius:0;'
                'display:block;transform:scale(1.12);" /></div>'
            )
        else:
            # 真人原图：原分辨率直出，不裁不缩
            parts.append(
                f'<img src="{url}" style="width:100%;border-radius:8px;display:block;" />'
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
    # 默认只推已目检库存（定时任务安全策略）；--allow-raw 手动全源模式
    allow_raw = "--allow-raw" in sys.argv
    images = pick_images(session, rng, bank=not dry, vetted_only=not allow_raw)
    if not images:
        if not allow_raw:
            log("无已目检库存可用，跳过推送（等人工目检放行 scripts/pic_vet.py 后恢复）")
            return 0
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
