# coding=utf-8
"""AI 撰写：选题 → 结构化正文

输出统一用 JSON（title / hook / sections / conclusion / tags），
下游各平台 adapter 再按自己的排版规则重组——这就是"一稿四改"的实现方式。

主通道：LLM（OpenAI 兼容协议，DeepSeek / OpenAI / Moonshot / Qwen 通用）
无 key 时：走模板降级骨架（结构完整但内容浅，只能保底，不能当正式内容用）
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


LLM_BASE = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "120"))

# ──────────────────── 文章结构 ────────────────────
@dataclass
class Section:
    heading: str
    body: str
    bullets: list[str] = field(default_factory=list)


@dataclass
class Article:
    title: str
    keyword: str
    angle: str
    hook: str
    sections: list[Section]
    conclusion: str
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @property
    def word_count(self) -> int:
        return len(re.sub(r"\s", "", self.hook)
                   + self.conclusion
                   + "".join(s.heading + s.body + "".join(s.bullets) for s in self.sections))

    @property
    def outline(self) -> list[str]:
        return [s.heading for s in self.sections]


# ──────────────────── Prompt ────────────────────
WRITER_SYSTEM = (
    "你是一个写实操类内容的中文技术作者，专长是把工具方法写成读者愿意读完、干完的文章。\n"
    "你的写作准则：\n"
    "1. 只写你能被追问细节的东西，不写空话套话\n"
    "2. 不吹不黑：工具的缺点、适用边界、什么情况不该用，必须写出来\n"
    "3. 不编造：不要虚构具体数字、版本号、亲测经历、用户评价\n"
    "4. 不做标题党：标题里不要出现'最强''第一''必看''震惊''99%的人不知道'\n"
    "5. 用平实的口语，像同事之间分享经验，不像广告稿\n"
    "6. 涉及刷单、破解、盗版、绕过付费、去水印、薅羊毛、医疗诊断、投资荐股、法律结论——一律不写"
)

WRITER_PROMPT = """写一篇中文实操文章，严格按下面的 JSON 格式输出，不要有任何 JSON 之外的文字。

【选题】{title}
【核心长尾词】{keyword}
【所属方向】{angle}
【必须覆盖的要点】
{points}

【硬性要求】
1. 总长度 1200-1800 个汉字（不含 JSON 符号），这是硬要求
2. hook（开头）：100-150 字，直接戳中读者在这个场景下的真实痛点，不要铺垫
3. sections：4-6 个小节，每节一个明确的小标题
   - 每节 body 150-300 字，讲清楚"怎么做"和"为什么这么做"
   - bullets：每节可有 0-4 条要点，每条一句话，是可立刻照做的步骤或清单
4. 必须有且只有一处讨论"什么情况下不该用这个方法"（可以单独成一节）
5. conclusion：80-150 字，收尾，不要喊口号
6. tags：4-6 个标签词，用于文章底部

【JSON 格式】
{{
  "title": "文章标题（可与选题标题不同，但要含核心长尾词，20字以内）",
  "hook": "开头段落",
  "sections": [
    {{"heading": "小节标题", "body": "小节正文", "bullets": ["要点1", "要点2"]}}
  ],
  "conclusion": "收尾段落",
  "tags": ["标签1", "标签2"]
}}"""


# ──────────────────── LLM 调用 ────────────────────
def _strip_fence(text: str) -> str:
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_json(text: str) -> dict | None:
    """从 LLM 输出里抠出 JSON（容忍前后废话和代码块）"""
    text = _strip_fence(text)
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start > -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            return None
    return None


def call_llm(prompt: str, system: str = WRITER_SYSTEM,
             max_tokens: int = 3000, temperature: float = 0.7) -> str | None:
    """统一的 LLM 调用入口。无 key / 失败返回 None（由上层降级）"""
    key = os.environ.get("LLM_API_KEY", "").strip()
    if not key or requests is None:
        return None
    try:
        r = requests.post(
            f"{LLM_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": LLM_MODEL,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=LLM_TIMEOUT,
        )
        r.raise_for_status()
        return (((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    except Exception as e:
        print(f"  [LLM 调用失败] {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
        return None


def write_article(topic, ending: str = "") -> Article:
    """选题 → Article。无 LLM 时走模板降级（结构完整，内容浅）"""
    points = "\n".join(f"- {p}" for p in topic.points)
    raw = call_llm(WRITER_PROMPT.format(
        title=topic.title, keyword=topic.keyword,
        angle=topic.angle, points=points))

    if raw:
        data = _extract_json(raw)
        if data and data.get("sections"):
            sections = [
                Section(
                    heading=str(s.get("heading", "")).strip(),
                    body=str(s.get("body", "")).strip(),
                    bullets=[str(b).strip() for b in (s.get("bullets") or [])],
                )
                for s in data["sections"] if s.get("heading") or s.get("body")
            ]
            if sections:
                return Article(
                    title=str(data.get("title") or topic.title).strip(),
                    keyword=topic.keyword,
                    angle=topic.angle,
                    hook=str(data.get("hook", "")).strip(),
                    sections=sections,
                    conclusion=(str(data.get("conclusion", "")).strip() + ("\n\n" + ending if ending else "")),
                    tags=[str(t).strip() for t in (data.get("tags") or [])][:6],
                )

    return _fallback_article(topic, ending)


def _fallback_article(topic, ending: str = "") -> Article:
    """无 LLM 时的骨架保底：结构完整，可检测到，但内容不足以发布正式文章"""
    secs = [
        Section("先说清楚什么时候值得做",
                f"做「{topic.keyword}」之前，先判断这件事是不是真的值得花时间。"
                f"如果一年做不满几次，手工做通常更快也更稳；"
                f"如果每周都要做、且流程固定，那才值得工具化。"
                f"这一步判断错了，后面所有投入都是浪费。",
                ["先记录一周自己的实际耗时", "算出每次省下的时间值不值这个成本",
                 "流程不固定的场景先别自动化"]),
        Section("常见的几种做法",
                f"目前处理「{topic.keyword}」主要有几条路：纯手工、用通用工具、"
                f"搭一条固定流程、或者直接找现成的服务。"
                f"纯手工适合低频场景；固定流程适合高频且规则清晰的；"
                f"找服务适合一次性需求、自己没时间搭的。",
                ["按频次选路线：偶尔一次就别搭", "高频且规则固定才值得固化流程"]),
        Section("工具做不到的部分",
                "任何工具都有边界。把这一节单独拎出来说，是为了避免踩坑："
                "格式复杂的排版、需要主观判断的内容、涉及责任认定的结论，"
                "这些地方工具只能给草稿，最终必须人工复核。",
                ["保留人工复核环节，不要追求全自动", "复核成本要算进总成本"]),
        Section("落地前先跑通一个小样本",
                "不要一上来就搭完整流程。先拿十条真实数据跑一遍，"
                "看看结果能不能直接用，再决定要不要扩大。",
                ["先用 10 条样本验证", "记录每一步耗时和返工次数"]),
    ]
    return Article(
        title=topic.title,
        keyword=topic.keyword,
        angle=topic.angle,
        hook=(f"「{topic.keyword}」这件事，大部分人卡住不是因为工具难找，"
              f"而是没有先想清楚哪一步真的值得自动化。"
              f"这篇按实操顺序拆一遍，包括哪些环节工具做不了、哪些情况根本不该做。"),
        sections=secs,
        conclusion=(ending or "先跑通一个小样本，再决定要不要投入更多。"
                    "工具会变，判断流程的能力不会。"),
        tags=["效率工具", "AI 工具", "实操", topic.angle],
    )


if __name__ == "__main__":
    import topics as T
    for t in T.pick_topics(1, seed="demo"):
        art = write_article(t)
        print(f"《{art.title}》 {art.word_count} 字  {len(art.sections)} 节  [LLM={'有' if os.environ.get('LLM_API_KEY') else '降级'}]")
        for s in art.outline:
            print("  -", s)