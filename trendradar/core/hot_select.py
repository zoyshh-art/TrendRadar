# coding=utf-8
"""每轮新闻精选：把 RSS 分组条目精炼到最多 max_items 条，当天热搜/热点优先。

选材策略（档内按热榜名次、发布时间排序）：
  1 档：标题与当日热榜严格命中（归一化互含 / 前 8 字相同）
  2 档：标题与当日热榜宽松命中（含中文双字组重叠率 ≥ 50% 且重叠 ≥ 3 组）
  补足档：各分类按组轮转取最新，保证每个分类都有代表（不挤掉低频源）

热榜接口整体失败时退化为纯分类轮转补足；无论何种情况条数上限始终生效。
仅影响推送内容，HTML 报告仍保留全量条目。
"""

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Tuple

import requests

# 默认热榜 API（newsnow 开源项目公共实例，与 crawler.fetcher 保持一致）
DEFAULT_HOT_API_URL = "https://newsnow.busiyi.world/api/s"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 双字组中需至少含一个汉字（排除纯数字年份等造成的假重叠）
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _normalize(title: str) -> str:
    """归一化：小写 + 去掉所有空白与非字母数字汉字字符"""
    return re.sub(r"[\s\W_]+", "", (title or "").lower(), flags=re.UNICODE)


def fetch_hot_titles(
    api_url: str,
    platforms: List[str],
    timeout: int = 10,
) -> List[Dict]:
    """并发抓取 newsnow 热榜，返回 [{"norm", "platform", "rank"}, ...]

    单个平台失败自动跳过；全部失败返回空列表（上层退化为按新旧精选）。
    """
    api = (api_url or "").strip() or DEFAULT_HOT_API_URL
    if not platforms:
        return []

    def _one(pid: str) -> List[Dict]:
        try:
            resp = requests.get(
                api,
                params={"id": pid, "latest": ""},
                headers=_HEADERS,
                timeout=timeout,
            )
            data = resp.json() or {}
            items = data.get("items") or (data.get("data") or {}).get("items") or []
            out: List[Dict] = []
            for rank, item in enumerate(items):
                norm = _normalize((item.get("title") or "").strip())
                if len(norm) >= 6:  # 过短热词噪音大，不参与匹配
                    out.append({"norm": norm, "platform": pid, "rank": rank})
            return out
        except Exception:
            return []

    hot: List[Dict] = []
    with ThreadPoolExecutor(max_workers=min(4, len(platforms))) as pool:
        for part in pool.map(_one, platforms):
            hot.extend(part)
    return hot


def _match_strict(rn: str, hn: str) -> bool:
    """严格命中：归一化互含，或前 8 字相同"""
    if len(rn) >= 8 and len(hn) >= 8 and rn[:8] == hn[:8]:
        return True
    if len(hn) >= 6 and hn in rn:
        return True
    if len(rn) >= 6 and rn in hn:
        return True
    return False


def _cjk_bigrams(s: str) -> set:
    return {s[i:i + 2] for i in range(len(s) - 1) if _CJK_RE.search(s[i:i + 2])}


def _match_loose(rn: str, hn: str) -> bool:
    """宽松命中：严格命中，或中文双字组重叠率高（容忍改写措辞）"""
    if _match_strict(rn, hn):
        return True
    b1, b2 = _cjk_bigrams(rn), _cjk_bigrams(hn)
    if len(b1) < 3 or len(b2) < 3:
        return False
    inter = len(b1 & b2)
    return inter >= 3 and inter / min(len(b1), len(b2)) >= 0.5


def _classify(rn: str, hot: List[Dict]) -> Tuple[int, int]:
    """返回 (档位, 命中的最佳热榜名次)；档位 1=严格 2=宽松 3=未命中"""
    if not rn or not hot:
        return 3, 0
    best = None
    tier = 3
    for h in hot:
        if _match_strict(rn, h["norm"]):
            if best is None or h["rank"] < best:
                best = h["rank"]
            tier = 1
    if tier != 1:
        for h in hot:
            if _match_loose(rn, h["norm"]):
                if best is None or h["rank"] < best:
                    best = h["rank"]
                tier = 2
    return tier, (best if best is not None else 0)


def select_rss_groups(groups: List[Dict], select_cfg: Dict) -> List[Dict]:
    """按 rss.select 配置对 RSS 分组做每轮精选，返回新分组列表

    - 未启用 / 无数据时原样返回（仍同步 count 与 titles 一致）
    - 条数上限对热榜接口成败均生效
    """
    if not groups:
        return groups

    try:
        max_items = int(select_cfg.get("MAX_ITEMS", 25))
    except (ValueError, TypeError):
        print(f"[警告] rss.select.max_items 格式错误 ({select_cfg.get('MAX_ITEMS')})，使用默认值 25")
        max_items = 25
    if max_items <= 0:
        max_items = 25

    enabled = bool(select_cfg.get("ENABLED", False))
    total_before = sum(len(g.get("titles") or []) for g in groups)
    if not enabled:
        for g in groups:
            g["count"] = len(g.get("titles") or [])
        return groups
    if total_before <= max_items:
        for g in groups:
            g["count"] = len(g.get("titles") or [])
        print(f"[精选] 共 {total_before} 条未超上限（{max_items}），全部保留")
        return groups

    try:
        timeout = int(select_cfg.get("HOT_TIMEOUT", 10))
    except (ValueError, TypeError):
        timeout = 10

    hot = fetch_hot_titles(
        select_cfg.get("HOT_API_URL", ""),
        select_cfg.get("HOT_PLATFORMS", []) or [],
        timeout=max(3, timeout),
    )

    # 逐条分档：(档位, 热榜名次, 全局发布新旧序, 分组序, 条目序)
    entries: List[Tuple[int, int, int, int, int]] = []
    for gi, g in enumerate(groups):
        for ti, td in enumerate(g.get("titles") or []):
            tier, hrank = _classify(_normalize(td.get("title", "")), hot)
            ranks = td.get("ranks") or []
            try:
                rec = int(ranks[0])
            except (IndexError, TypeError, ValueError):
                rec = 999
            entries.append((tier, hrank, rec, gi, ti))

    # 档 1/2（热搜命中）优先入选，按热榜名次、发布时间排序
    hot_entries = sorted((e for e in entries if e[0] <= 2), key=lambda e: (e[0], e[1], e[2]))
    kept = {(e[3], e[4]) for e in hot_entries[:max_items]}

    # 补足：各分类按组轮转取最新（组内按发布时间从新到旧），保证分类不被挤没
    if len(kept) < max_items:
        rec_of = {(e[3], e[4]): e[2] for e in entries}
        order_by_group = {
            gi: sorted(
                (ti for (xx, ti) in rec_of if xx == gi),
                key=lambda ti: rec_of[(gi, ti)],
            )
            for gi in range(len(groups))
        }
        cursors = {gi: 0 for gi in range(len(groups))}
        progressed = True
        while len(kept) < max_items and progressed:
            progressed = False
            for gi in range(len(groups)):
                order = order_by_group.get(gi) or []
                while cursors[gi] < len(order) and (gi, order[cursors[gi]]) in kept:
                    cursors[gi] += 1
                if cursors[gi] < len(order):
                    kept.add((gi, order[cursors[gi]]))
                    cursors[gi] += 1
                    progressed = True
                    if len(kept) >= max_items:
                        break

    new_groups: List[Dict] = []
    for gi, g in enumerate(groups):
        titles = [td for ti, td in enumerate(g.get("titles") or []) if (gi, ti) in kept]
        if titles:
            ng = dict(g)
            ng["titles"] = titles
            ng["count"] = len(titles)
            new_groups.append(ng)

    total_after = sum(len(g.get("titles") or []) for g in new_groups)
    if hot:
        tier_of = {(e[3], e[4]): e[0] for e in entries}
        c1 = sum(1 for k in kept if tier_of.get(k) == 1)
        c2 = sum(1 for k in kept if tier_of.get(k) == 2)
        c3 = len(kept) - c1 - c2
        print(
            f"[精选] 热榜信号 {len(hot)} 条：热搜严格命中 {c1} + 宽松命中 {c2} + 分类最新补足 {c3}"
            f" = {total_after}/{total_before} 条（上限 {max_items}）"
        )
    else:
        print(f"[精选] 热榜不可用，按分类最新补足 {total_after}/{total_before} 条（上限 {max_items}）")

    return new_groups
