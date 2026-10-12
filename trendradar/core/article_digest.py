# coding=utf-8
"""新闻正文概括：抓取文章页面，为每条新闻生成 200-300 字中文概括。

RSS 自带的 description 大多只有 50-180 字（部分源为 0），达不到推送要求。
本模块在推送前抓取原文页面，抽取正文，按句累加生成 200-300 字的概括。

流程：
  1. fetch_article  并发抓取文章页面（失败自动跳过，不影响推送）
  2. extract_body   HTMLParser 密度启发式抽取正文段落（stdlib，无第三方依赖）
  3. make_digest    清洗噪音行 → 按句累加到目标字数区间
  4. enrich_rss_groups  为 RSS 分组条目回填 summary（已有合格概括则跳过）

任何一步失败都退回原有摘要，绝不让推送失败。
"""

import html as _html
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}

# 正文里的噪音行（来源/责编/分享/字号等模板尾巴）
_JUNK_LINE_PATTERNS = [
    r"^来源[:：]",
    r"^作者[:：]",
    r"^责编[:：]",
    r"^责任编辑[:：]",
    r"^编辑[:：]",
    r"^记者[:：]",
    r"^字号[:：]",
    r"^打印",
    r"^分享",
    r"^收藏",
    r"^评论",
    r"^\d+\s*浏览",
    r"^扫一扫",
    r"^扫码",
    r"^视频",
    r"^播放",
    r"^广告",
    r"^相关阅读",
    r"^相关新闻",
    r"^推荐阅读",
    r"^点击进入专题",
    r"^专题[:：]",
    r"^【?编辑[:：]",
    r"^原标题[:：]\s*$",
    r"^本文由.*发表",
    r"^本文系.*原创",
    r"^声明[:：]",
    r"^免责声明",
]
_JUNK_SENTENCE_PATTERNS = [
    r"^【编辑[:：]",
    r"成为《?纽约时报》?会员",
    r"点击(浏览|查看|订阅|下载)",
    r"扫码(关注|下载|阅读)",
    r"关注(我们|公众号)",
    r"^\d{1,2}:\d{2}(\s*/\s*\d{1,2}:\d{2})?$",
    r"^https?://",
    r"^www\.",
    r"^\d+\s*人(已|参与)",
    r"字号",
]
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?；;…])")


def fetch_article(url: str, timeout: int = 12) -> str:
    """抓取文章页面 HTML；失败抛异常（由调用方兜底）"""
    if not url or not url.startswith(("http://", "https://")):
        raise ValueError("bad url")
    resp = requests.get(url, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    if len(resp.content) > 3 * 1024 * 1024:
        raise ValueError("page too large")
    if resp.encoding and resp.encoding.lower() not in ("utf-8", "utf8"):
        # GBK 等非 UTF-8 站点让 requests 用嗅探编码
        resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


class _BodyExtractor(HTMLParser):
    """按块收集候选正文段落：p/li 全文 + 含长文本的 div"""

    _SKIP = {"script", "style", "head", "noscript", "iframe", "template", "svg", "form"}
    _CLEAN = {"p", "li", "div", "article", "section"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.blocks: List[str] = []   # 文档序的文本块
        self._buf: List[str] = []
        self._buf_tag: Optional[str] = None

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in self._CLEAN:
            self._flush()
            self._buf_tag = tag

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag in self._CLEAN:
            self._flush()

    def handle_data(self, data):
        if self.skip_depth:
            return
        if self._buf_tag:
            self._buf.append(data)

    def _flush(self):
        if self._buf_tag and self._buf:
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            if text:
                self.blocks.append(text)
        self._buf = []
        self._buf_tag = None

    def close(self):
        super().close()
        self._flush()


def _clean_lines(text: str) -> List[str]:
    """拆行 + 丢噪音；对单个长块再按句拆"""
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    out: List[str] = []
    for ln in lines:
        if any(re.search(p, ln) for p in _JUNK_LINE_PATTERNS):
            continue
        if len(ln) < 12:  # 单块里的短碎句
            continue
        out.append(ln)
    return out


def extract_body(page_html: str) -> str:
    """从文章页面 HTML 抽取正文文本（取最长连续有效段落块）"""
    parser = _BodyExtractor()
    try:
        parser.feed(page_html)
        parser.close()
    except Exception:
        pass

    blocks = _clean_lines("\n".join(parser.blocks))

    # div 块常常是整段正文（含多个 p），优先取 CJK 长度最长的块做种子，
    # 再向两侧扩展相邻有效块，拼出完整正文
    if not blocks:
        return ""

    def cjk_len(s: str) -> int:
        return sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")

    lengths = [cjk_len(b) for b in blocks]
    best = max(range(len(blocks)), key=lambda i: lengths[i])
    body = [blocks[best]]
    for i in range(best - 1, -1, -1):
        if cjk_len(blocks[i]) >= 25:
            body.insert(0, blocks[i])
        else:
            break
    for i in range(best + 1, len(blocks)):
        if cjk_len(blocks[i]) >= 25:
            body.append(blocks[i])
        else:
            break

    text = "\n".join(body)
    # 去掉块内被 HTML 压在一起的句间残空白
    return re.sub(r"[ \t]+", " ", text).strip()


def make_digest(
    text: str,
    title: str = "",
    min_chars: int = 200,
    max_chars: int = 300,
) -> str:
    """把正文清洗成 min-max 字数的中文概括（按句累加，宁短不长）

    - 丢掉与标题重复的首句、噪音句、过短碎片
    - 先攒到 min_chars；下一句会让总数超过 max_chars 且已 >= min 时收尾
    - 正文不足 min_chars 时有多少给多少（尽力而为，不编造）
    """
    if not text:
        return ""

    sentences: List[str] = []
    for block in re.split(r"\n+", text):
        block = block.strip()
        if not block:
            continue
        for seg in _SENT_SPLIT_RE.split(block):
            seg = seg.strip()
            if not seg:
                continue
            if any(re.search(p, seg) for p in _JUNK_SENTENCE_PATTERNS):
                continue
            if len(seg) < 10:  # 碎句并在上一句后面意义不大，直接丢弃
                continue
            sentences.append(seg)

    # 去重 + 去标题重复首句
    norm_title = re.sub(r"\s+", "", title or "")
    kept: List[str] = []
    seen = set()
    for seg in sentences:
        norm_seg = re.sub(r"\s+", "", seg)
        if norm_seg in seen:
            continue
        seen.add(norm_seg)
        if norm_title and len(norm_title) >= 6 and norm_seg.startswith(norm_title[: min(12, len(norm_title))]):
            continue
        kept.append(seg)

    out: List[str] = []
    total = 0
    for seg in kept:
        if total >= min_chars and total + len(seg) > max_chars:
            break
        if total + len(seg) > max_chars and not out:
            # 首句就超长：按句号内部逗号截断到 max_chars
            out.append(seg[:max_chars])
            total = max_chars
            break
        out.append(seg)
        total += len(seg)
        if total >= max_chars:
            break

    digest = "".join(out).strip()
    if digest and digest[-1] not in "。！？!?…；;":
        if len(digest) >= max_chars:
            digest = digest[: max_chars - 1] + "。"
        else:
            digest += "。"
    return digest


def enrich_rss_groups(
    groups: List[Dict],
    enabled: bool = True,
    min_chars: int = 200,
    max_chars: int = 300,
    timeout: int = 12,
    concurrency: int = 8,
) -> List[Dict]:
    """为 RSS 分组条目的 summary 回填 200-300 字正文概括（原地修改并返回）

    已有合格概括（长度在 [min_chars, max_chars]）则不重复抓取。
    """
    if not groups or not enabled:
        return groups

    targets: List[Tuple[int, Dict]] = []  # (group_idx, title_data)
    for gi, g in enumerate(groups):
        for td in g.get("titles") or []:
            cur = str(td.get("summary") or "").strip()
            if min_chars <= len(cur) <= max_chars:
                continue
            url = (td.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                continue
            targets.append((gi, td))

    if not targets:
        print(f"[概括] 无需补全：现有摘要均在 {min_chars}-{max_chars} 字区间")
        return groups

    print(f"[概括] 需补全 {len(targets)} 条的正文概括（并发 {concurrency}，单篇超时 {timeout}s）")
    lock = threading.Lock()
    stats = {"ok": 0, "short": 0, "fail": 0}

    def _work(td: Dict) -> None:
        url = td.get("url", "")
        try:
            page = fetch_article(url, timeout=timeout)
            body = extract_body(page)
            digest = make_digest(body, td.get("title", ""), min_chars, max_chars)
            if digest:
                with lock:
                    td["summary"] = digest
                    stats["ok"] += 1
            else:
                stats["fail"] += 1
        except Exception:
            stats["fail"] += 1

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        list(pool.map(_work, [td for _, td in targets]))

    in_range = sum(
        1
        for g in groups
        for td in g.get("titles") or []
        if min_chars <= len(str(td.get("summary") or "")) <= max_chars
    )
    print(
        f"[概括] 完成：抓取成功 {stats['ok']}，失败 {stats['fail']}；"
        f"当前 {in_range} 条摘要落在 {min_chars}-{max_chars} 字区间"
    )
    return groups
