# coding=utf-8
"""一稿四改：同一篇文章 → 公众号 / 知乎 / 百家号 / 小红书 四种成品

每家的读者习惯、排版规则、标题偏好都不同：
- 公众号：长文、段落短、小标题、可引用块——读得完才有转发
- 知乎：回答体、逻辑链完整、结论明确——要经得起专业读者挑刺
- 百家号：更直白、段落更短、要点前置——搜索流量为主
- 小红书：短标题、emoji 分点、口语化——C 端刷feed，靠标签长尾

输出统一带 HTML 转义（LLM 内容不可信，绝不把原始 HTML 直接塞进公众号）
"""

from __future__ import annotations

import html
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import topics as T
from writer import Article, Section

# 各平台标题长度上限（超出会被截断或影响分发）
LIMIT_TITLE = {"wechat": 64, "zhihu": 100, "baijia": 30, "xhs": 20}


def _esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def _clean_inline(s: str) -> str:
    """去掉 LLM 可能带出的 markdown 记号，避免转义后出现 **** 这类垃圾"""
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s or "")
    s = re.sub(r"`(.+?)`", r"\1", s)
    return s.strip()


def _cut_title(title: str, platform: str) -> str:
    lim = LIMIT_TITLE[platform]
    t = re.sub(r"\s+", " ", _clean_inline(title)).strip()
    if len(t) <= lim:
        return t
    cut = t[:lim]
    for sep in ("：", "，", "、", " ", "的"):
        p = cut.rfind(sep)
        if p > lim * 0.5:
            return cut[:p]
    return cut


# ──────────────────── 公众号（HTML，草稿箱直投） ────────────────────
def render_wechat(art: Article) -> dict:
    """微信草稿箱 article.content 要求 HTML"""
    parts: list[str] = []

    # 开头（加浅色块，公众号里视觉分隔明显）
    parts.append(f'<section style="font-size:15px;line-height:1.8;color:#3f3f3f;">'
                 f'{_esc(_clean_inline(art.hook))}</section>')
    parts.append("<p><br/></p>")

    for s in art.sections:
        parts.append(
            f'<p style="font-size:16px;font-weight:600;color:#1a1a1a;margin:26px 0 12px;">'
            f'<span style="border-left:4px solid #07c160;padding-left:10px;">'
            f'{_esc(_clean_inline(s.heading))}</span></p>'
        )
        if s.body:
            for para in [p for p in re.split(r"\n{1,}", s.body.strip()) if p.strip()]:
                parts.append(
                    f'<p style="font-size:15px;line-height:1.9;color:#3f3f3f;margin:12px 0;">'
                    f'{_esc(_clean_inline(para))}</p>'
                )
        if s.bullets:
            items = "".join(
                f'<li style="font-size:15px;line-height:1.9;color:#3f3f3f;margin:6px 0;">'
                f'{_esc(_clean_inline(b))}</li>' for b in s.bullets
            )
            parts.append(
                f'<ul style="padding-left:22px;margin:12px 0;'
                f'border-left:3px solid #e8e8e8;padding-top:8px;padding-bottom:8px;">{items}</ul>'
            )
        parts.append("<p><br/></p>")

    concl = _clean_inline(art.conclusion)
    parts.append(
        f'<section style="font-size:15px;line-height:1.9;color:#3f3f3f;'
        f'background:#f7f7f7;padding:16px;border-radius:6px;margin-top:8px;">'
        f'<strong>最后一句</strong><br/>{_esc(concl)}</section>'
    )

    if art.tags:
        tag_html = "".join(
            f'<a style="display:inline-block;font-size:13px;color:#576b95;'
            f'margin:18px 8px 0 0;">#{_esc(t)}</a>' for t in art.tags
        )
        parts.append(f"<p style=\"margin-top:8px;\">{tag_html}</p>")

    return {
        "platform": "wechat",
        "title": _cut_title(art.title, "wechat"),
        "digest": _clean_inline(art.hook)[:120],
        "content_html": "".join(parts),
        "tags": art.tags,
    }


# ──────────────────── 知乎（Markdown） ────────────────────
def render_zhihu(art: Article) -> dict:
    lines: list[str] = [f"# {art.title}", ""]
    # 知乎读者要"针对问题作答"的语感
    lines += [f"> **问题**：{art.keyword}到底该怎么处理？"
              f"这篇按实操顺序拆一遍，包含工具做不到的部分。", ""]
    lines += [art.hook, ""]
    lines += ["---", ""]
    for i, s in enumerate(art.sections, 1):
        lines += [f"## {i}. {s.heading}", ""]
        for para in [p for p in re.split(r"\n{1,}", s.body.strip()) if p.strip()]:
            lines += [para, ""]
        for b in s.bullets:
            lines.append(f"- {b}")
        if s.bullets:
            lines.append("")
    lines += ["---", ""]
    lines += ["## 结论", "", art.conclusion, ""]
    if art.tags:
        lines += ["**标签**：" + " ".join(f"#{t}" for t in art.tags), ""]
    return {
        "platform": "zhihu",
        "title": _cut_title(art.title, "zhihu"),
        "markdown": "\n".join(lines),
        "tags": art.tags,
    }


# ──────────────────── 百家号（Markdown，短平快） ────────────────────
def render_baijia(art: Article) -> dict:
    lines: list[str] = [f"# {_cut_title(art.title, 'baijia')}", ""]
    lines += [art.hook, ""]
    for s in art.sections:
        lines += [f"### {s.heading}", "", s.body.strip(), ""]
        # 百家号读者吃"清单"，要点直接提到正文里
        for b in s.bullets:
            lines.append(f"- {b}")
        if s.bullets:
            lines.append("")
    lines += ["**最后一句**：" + art.conclusion.strip(), ""]
    return {
        "platform": "baijia",
        "title": _cut_title(art.title, "baijia"),
        "markdown": "\n".join(lines),
        "tags": art.tags,
    }


# ──────────────────── 小红书（Markdown + emoji，短） ────────────────────
_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣"]


def _emoji_idx(i: int) -> str:
    return _EMOJI[i] if i < len(_EMOJI) else f"{i+1}."


def render_xhs(art: Article) -> dict:
    """小红书：短标题、emoji 分点、口语化、每段只留前两句"""
    lines: list[str] = ["做这件事之前，先看这几点👇", ""]
    for i, s in enumerate(art.sections):
        lines.append(f"{_emoji_idx(i)} {s.heading}")
        body = _clean_inline(s.body)
        short = "。".join([p for p in re.split(r"[。！？]", body) if p.strip()][:2])
        if short and not short.endswith("。"):
            short += "。"
        if short:
            lines += [short, ""]
        for b in s.bullets:
            lines.append(f"· {b}")
        if s.bullets:
            lines.append("")
    first = art.conclusion.strip().split("\n")[0]
    lines += ["———", first if first else "有问题评论区问我，看到就回～", ""]
    lines += ["#" + " #".join(art.tags[:5])]
    return {
        "platform": "xhs",
        "title": _cut_title(art.title, "xhs"),
        "markdown": "\n".join(lines),
        "tags": art.tags,
    }


RENDERERS = {
    "wechat": render_wechat,
    "zhihu": render_zhihu,
    "baijia": render_baijia,
    "xhs": render_xhs,
}

PLATFORM_LABEL = {
    "wechat": "微信公众号", "zhihu": "知乎", "baijia": "百家号", "xhs": "小红书",
}


def render_all(art: Article) -> dict[str, dict]:
    """一次撰写，四种成品"""
    return {name: fn(art) for name, fn in RENDERERS.items()}


if __name__ == "__main__":
    from writer import write_article
    t = T.pick_topics(1, seed="demo")[0]
    art = write_article(t)
    print(f"《{art.title}》 {art.word_count} 字")
    outs = render_all(art)
    for k, v in outs.items():
        body = v.get("markdown") or v.get("content_html", "")
        print(f"\n{'=' * 20} {PLATFORM_LABEL[k]} · 标题({len(v['title'])}字)：{v['title']}")
        print(body[:500] + ("..." if len(body) > 500 else ""))