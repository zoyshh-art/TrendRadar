# coding=utf-8
"""内容引擎主流程：选题 → AI 撰写 → 一稿四改 → 公众号草稿箱 + 其他平台 .md

每日产出：
  output/content/YYYY-MM-DD/
    meta.json            今日选题与统计
    wechat.html          公众号成品（可直接看排版效果）
    zhihu.md             知乎成品
    baijia.md            百家号成品
    xhs.md               小红书成品
    index.md             人工发布清单（今天要做什么、复制什么、去哪发）

发布动作保留人工：
  - 公众号：草稿箱已自动写入，你只需检查并点发布
  - 其他平台：复制对应 .md 粘贴到各自编辑器（无官方发布 API，逆向有封号风险）

用法：
  python content_engine/pipeline.py --count 3       # 生成 3 篇
  python content_engine/pipeline.py --push          # 同时写入公众号草稿箱
  python content_engine/pipeline.py --dry-run       # 不落盘不推送，只预览
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import adapters as A          # noqa: E402
import topics as T            # noqa: E402
from writer import write_article  # noqa: E402

CN_TZ = timezone(timedelta(hours=8))
OUT_ROOT = Path(os.environ.get("CONTENT_OUT", HERE.parent / "output" / "content"))
HISTORY = OUT_ROOT / "history.json"
DEFAULT_COUNT = int(os.environ.get("CONTENT_DAILY_COUNT", "3"))
CONCURRENCY = int(os.environ.get("CONTENT_CONCURRENCY", "3"))
MIN_WORDS = int(os.environ.get("CONTENT_MIN_WORDS", "900"))   # 低于此字数视为降级，不推送
MAX_LLM_FAILS = int(os.environ.get("CONTENT_MAX_LLM_FAILS", "1"))


def log(msg: str) -> None:
    print(f"[{datetime.now(CN_TZ).strftime('%m-%d %H:%M:%S')}] {msg}", flush=True)


def load_history() -> dict:
    try:
        return json.loads(HISTORY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"published": {}, "keywords": []}


def save_history(h: dict) -> None:
    try:
        HISTORY.parent.mkdir(parents=True, exist_ok=True)
        HISTORY.write_text(json.dumps(h, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as e:
        log(f"历史写入失败（不阻断）: {e}")


def has_llm() -> bool:
    return bool(os.environ.get("LLM_API_KEY", "").strip())


# ──────────────────── 单篇处理 ────────────────────
def make_one(topic, ending: str) -> tuple[object, dict, bool]:
    """选题 → 文章 → 四平台成品。返回 (article, renders, is_real)"""
    art = write_article(topic, ending)
    renders = A.render_all(art)
    # 「降级骨架」字数明显不足，视为不可发布
    is_real = art.word_count >= MIN_WORDS
    return art, renders, is_real


# ──────────────────── 今日产出清单 ────────────────────
def build_index(date_str: str, items: list[dict]) -> str:
    lines = [f"# 内容发布清单 · {date_str}", "",
             f"今日 {len(items)} 篇。每篇已生成四个平台版本，复制对应文件粘贴到各自编辑器即可。",
             ""]
    for i, it in enumerate(items, 1):
        lines += [
            f"## {i}. {it['title']}",
            "",
            f"- 长尾词：`{it['keyword']}` | 方向：{it['angle']} | 字数：{it['word_count']}",
            f"- 公众号：已写入草稿箱（media_id `{it.get('wechat_media_id', '未写入')}`）",
            "",
            "| 平台 | 文件 | 标题（已按平台长度裁好） |",
            "|---|---|---|",
        ]
        for pkey, label in A.PLATFORM_LABEL.items():
            fn = {"wechat": "wechat.html", "zhihu": "zhihu.md",
                  "baijia": "baijia.md", "xhs": "xhs.md"}[pkey]
            lines.append(f"| {label} | `{fn}` | {it['titles'].get(pkey, '')} |")
        lines += ["", "**检查清单**：标题有没有长尾词 / 有没有绝对化表述 / 数据是否编造 / 结尾是否自然", ""]
    return "\n".join(lines)


# ──────────────────── 主流程 ────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=DEFAULT_COUNT)
    ap.add_argument("--push", action="store_true", help="写入公众号草稿箱")
    ap.add_argument("--dry-run", action="store_true", help="只预览不落盘")
    ap.add_argument("--seed", default=None)
    args = ap.parse_args()

    log(f"选题引擎启动｜今日计划 {args.count} 篇｜LLM={'已配置 ' + os.environ.get('LLM_MODEL', '?') if has_llm() else '未配置(将走降级骨架)'}")

    history = load_history()
    exclude = set(history.get("published", {}).keys())

    seed = args.seed or datetime.now(CN_TZ).strftime("%Y%m%d")
    chosen = T.pick_topics(args.count, seed=seed, exclude=exclude)
    if not chosen:
        log("没有可用选题（历史已覆盖），请扩充 topics.SCENARIOS")
        return 0
    log(f"选中 {len(chosen)} 个选题：")
    for t in chosen:
        log(f"  · [{t.angle}] {t.title}")

    # ── 并发撰写 ──
    results: list[tuple] = []
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = [pool.submit(make_one, t, T.get_ending(seed + t.keyword)) for t in chosen]
        for f in futures:
            try:
                results.append(f.result())
            except Exception as e:
                log(f"生成失败 {type(e).__name__}: {str(e)[:120]}")

    good = [r for r in results if r[2]]
    bad = len(results) - len(good)
    log(f"撰写完成：合格 {len(good)} 篇，降级/过短 {bad} 篇")

    if not results:
        log("没有产出任何文章，退出")
        return 1
    if not has_llm():
        log("⚠ 未配置 LLM_API_KEY，以下为模板降级骨架，字数与干货不足，**不建议发布**。")
        log("  配 LLM_API_KEY（DeepSeek 即可）后重跑即可产出可发布内容。")
    if bad > MAX_LLM_FAILS:
        log("过多生成失败，终止（避免把降级内容当成品推出去）")
        return 1

    date_str = datetime.now(CN_TZ).strftime("%Y-%m-%d")
    day_dir = OUT_ROOT / date_str
    day_dir.mkdir(parents=True, exist_ok=True)

    items: list[dict] = []
    for art, renders, is_real in results:
        titles = {k: v["title"] for k, v in renders.items()}
        (day_dir / "wechat.html").write_text(
            f"<!-- 标题：{titles['wechat']} -->\n" + renders["wechat"]["content_html"],
            encoding="utf-8")
        (day_dir / "zhihu.md").write_text(renders["zhihu"]["markdown"], encoding="utf-8")
        (day_dir / "baijia.md").write_text(renders["baijia"]["markdown"], encoding="utf-8")
        (day_dir / "xhs.md").write_text(renders["xhs"]["markdown"], encoding="utf-8")
        items.append({
            "title": art.title, "keyword": art.keyword, "angle": art.angle,
            "word_count": art.word_count, "publishable": is_real,
            "outline": art.outline, "tags": art.tags, "titles": titles,
        })

    log(f"已写入 {day_dir}")

    # ── 公众号草稿箱 ──
    if args.push and not args.dry_run:
        try:
            import wechat_draft as WX
            if not WX.is_configured():
                log("公众号未配置 WECHAT_APPID/WECHAT_APPSECRET，跳过草稿箱写入")
            else:
                for i, (art, renders, is_real) in enumerate(results, 1):
                    try:
                        mid = WX.create_draft(
                            title=renders["wechat"]["title"],
                            content_html=renders["wechat"]["content_html"],
                            digest=renders["wechat"]["digest"],
                        )
                        items[i - 1]["wechat_media_id"] = mid
                        log(f"  草稿{i} 已入草稿箱 media_id={mid}")
                    except Exception as e:
                        log(f"  草稿{i} 写入失败: {e}")
                        items[i - 1]["wechat_media_id"] = f"失败:{str(e)[:60]}"
        except Exception as e:
            log(f"草稿箱模块不可用: {e}")

    # ── 落盘清单 ──
    (day_dir / "index.md").write_text(build_index(date_str, items), encoding="utf-8")
    (day_dir / "meta.json").write_text(
        json.dumps({"date": date_str, "llm": has_llm(), "items": items},
                   ensure_ascii=False, indent=1), encoding="utf-8")

    # ── 更新历史（供选题去重） ──
    if not args.dry_run:
        pub = history.setdefault("published", {})
        for it in items:
            if it["publishable"]:
                pub[it["title"]] = datetime.now(CN_TZ).isoformat()
        hist = history.setdefault("keywords", [])
        for it in items:
            if it["keyword"] not in hist:
                hist.append(it["keyword"])
        # 只留最近 120 篇 + 400 个关键词，避免无限增长
        if len(pub) > 120:
            for k in sorted(pub, key=lambda x: pub[x])[:-120]:
                pub.pop(k, None)
        history["keywords"] = hist[-400:]
        save_history(history)
        log(f"历史已更新：已发 {len(pub)} 篇 / 关键词 {len(history['keywords'])} 个")

    log("完成 ✅ 公众号草稿箱自动写入，其他平台复制文件发布")
    return 0


if __name__ == "__main__":
    sys.exit(main())