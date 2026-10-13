# coding=utf-8
"""选题库：关键词矩阵 → 每日选题

定位：通用效率 / AI 工具，走量漏斗（不讲服务，只讲干货和实测）。
所有选题走「搜索长尾」逻辑——用户是搜出来的，不是刷出来的，
所以每个选题都必须对应一个真实会被搜到的长尾词。

安全红线（硬性）：
- 不做破解/盗版/去水印/绕过付费/薅羊毛类选题（灰色，且平台会限流）
- 不做医疗/金融荐股/法律意见类实操建议（合规风险）
- 不做"最强/第一/必看"式标题党（降权且拉低信任）
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Topic:
    """一个选题 = 长尾词 + 标题骨架 + 内容要点"""

    keyword: str          # 目标长尾词（决定写给谁、被谁搜到）
    title: str            # 标题
    angle: str            # 内容角度
    pattern: str = ""     # 标题骨架（用于去重，避免多篇同一套路）
    points: tuple[str, ...] = field(default_factory=tuple)  # 覆盖的要点


# ──────────────────── 关键词矩阵 ────────────────────
# 场景 × 人群，交叉出长尾词。这些都是真实有人搜的词。
SCENARIOS: dict[str, tuple[str, ...]] = {
    "办公文档": (
        "周报自动生成", "会议纪要整理", "ppt 大纲", "邮件回复模板",
        "excel 公式", "合同条款梳理", "长文档总结",
    ),
    "信息处理": (
        "信息整理", "网页内容提取", "多平台内容改写", "翻译润色",
        "语音转文字", "字幕生成", "文章摘要",
    ),
    "图像视频": (
        "商品图生成", "视频剪辑自动化", "图片批量处理", "宣传片制作",
        "老照片修复", "头像设计", "logo 设计",
    ),
    "数据整理": (
        "数据清洗", "报表自动化", "数据可视化", "竞品监控",
        "舆情监控", "库存监控",
    ),
    "学习研究": (
        "文献综述", "论文资料整理", "英语学习工具", "编程助手",
        "正则表达式", "sql 写法",
    ),
    "日常提效": (
        "待办清单", "自动化工具", "手机提效", "浏览器插件",
    ),
}

AUDIENCES: tuple[str, ...] = (
    "打工人", "设计师", "程序员", "运营", "销售", "行政", "老师", "学生",
    "自媒体", "电商卖家", "小公司老板", "会计", "律师", "摄影师",
)

# 标题骨架：{kw} 是长尾词。刻意避开"最强/第一/必看"这类标题党。
TITLE_PATTERNS: tuple[str, ...] = (
    "{kw}：我用过的 3 个免费方案",
    "{kw}，到底该用哪个工具",
    "{kw}的完整流程（附实测踩坑）",
    "{kw}：从 2 小时到 10 分钟",
    "{kw}，不用花一分钱",
    "{kw}：一份可以直接抄的清单",
    "{kw}这件事，90% 的人第一步就做错了",
    "{kw}：我做了 5 个工具的横向对比",
    "关于 {kw}，说几句实在话",
    "{kw}：先别急着付费，这几个方案够用",
)

# 每个场景下的内容要点（决定文章有没有干货）
POINT_BANK: dict[str, tuple[str, ...]] = {
    "办公文档": (
        "手工做和工具做的差距在哪（用具体耗时对比）",
        "免费方案能做到什么程度，哪些必须付费",
        "常见坑：格式乱、幻觉内容、需要人工复核的地方",
        "什么场景根本不值得自动化",
    ),
    "信息处理": (
        "工具的能力边界在哪（什么情况会出错）",
        "省下来的时间具体值多少钱",
        "手工做的部分怎么保留质量",
        "一条流程从输入到成品的完整走法",
    ),
    "图像视频": (
        "出图效果和真实需求的差距",
        "哪些环节 AI 做不了还得靠手工",
        "成本对比：AI 生成 vs 外包 vs 自己拍",
        "商用时的版权和合规注意事项",
    ),
    "数据整理": (
        "小数据量根本不需要上工具",
        "搭一条流程要花多久",
        "数据源不稳定时的应对",
        "哪些指标值得自动化监控，哪些是浪费",
    ),
    "学习研究": (
        "AI 能帮你做哪一步，不能做哪一步",
        "查证和交叉验证的必要性",
        "省时间但增加什么成本",
        "一份可复用的检查清单",
    ),
    "日常提效": (
        "设置一次能省多久的账",
        "工具之间能不能叠着用",
        "什么该自动化、什么纯属折腾",
        "关掉比开启更省事的情况",
    ),
}

# 每篇固定的收尾模块（专业感 + 自然引流，不硬广）
ENDING_POOL: tuple[str, ...] = (
    "如果你有具体场景想让我实测，可以在评论区说一声，我挑高频的写。",
    "以上都是我自己用过的，工具链接放在评论区，有失效的再补。",
    "工具会变，思路不会。这套流程的关键是先想清楚哪一步真的值得自动化。",
    "踩过的坑我都写出来了，省得你再走一遍。",
)


def build_topic_pool() -> list[Topic]:
    """把关键词矩阵展开成候选选题池"""
    pool: list[Topic] = []
    for scenario, kws in SCENARIOS.items():
        for kw in kws:
            for tpl in TITLE_PATTERNS:
                pool.append(
                    Topic(
                        keyword=kw,
                        title=tpl.format(kw=kw),
                        angle=scenario,
                        pattern=tpl,
                        points=POINT_BANK[scenario],
                    )
                )
    return pool


def pick_topics(n: int = 3, seed: str | None = None,
                exclude: set[str] | None = None) -> list[Topic]:
    """随机抽 n 个选题。三重去重：长尾词不重复、标题骨架不重复、场景尽量不重复。"""
    pool = build_topic_pool()
    exclude = exclude or set()
    rng = random.Random(seed)

    candidates = [t for t in pool if t.title not in exclude]
    rng.shuffle(candidates)

    chosen: list[Topic] = []
    used_kw: set[str] = set()
    used_pattern: set[str] = set()
    used_scenario: set[str] = set()

    # 三轮贪心：每轮分别放宽「场景重复」→「骨架重复」→「长尾词重复」
    for require_pattern in (True, False):
        for t in candidates:
            if len(chosen) >= n:
                break
            if t.title in {c.title for c in chosen}:
                continue
            if t.keyword in used_kw:
                continue
            if require_pattern and t.pattern in used_pattern:
                continue
            if t.angle in used_scenario and len(used_scenario) < len(SCENARIOS) and len(chosen) < n:
                # 还没铺满所有场景就别重复场景
                if any(c.angle not in used_scenario for c in candidates):
                    continue
            chosen.append(t)
            used_kw.add(t.keyword)
            used_pattern.add(t.pattern)
            used_scenario.add(t.angle)
        if len(chosen) >= n:
            break

    # 兜底：放宽长尾词限制也要凑够 n
    for t in candidates:
        if len(chosen) >= n:
            break
        if t.title not in {c.title for c in chosen}:
            chosen.append(t)
            used_scenario.add(t.angle)

    return chosen[:n]


def get_ending(seed: str | None = None) -> str:
    return random.Random(seed).choice(ENDING_POOL)


if __name__ == "__main__":
    for t in pick_topics(3, seed="demo"):
        print(f"[{t.angle}] {t.title}")
        print(f"  长尾词：{t.keyword}")
        print(f"  要点：{len(t.points)} 条")