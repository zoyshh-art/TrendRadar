# coding=utf-8
"""生成图库存目检工具（人工目检是当前唯一的 NSFW 防线）

用法：
  python scripts/pic_vet.py                      # 拼带编号网格供人工目检（默认 output/pet_grid.jpg）
  python scripts/pic_vet.py --vetted 3,7,9       # 将指定编号标记为"已目检"（放行进定时推送池）
  python scripts/pic_vet.py --unvetted 12        # 撤销指定编号的目检标记
  python scripts/pic_vet.py --status             # 只看库存状态（已目检/未目检/已推过）

目检标准：
- 合入：穿着完整（内衣/比基尼/吊带/礼服/衬衫等），无裸露敏感部位
- 不合：全裸、乳头/生殖器可见、裸背擦边、透视衣物到露点
"""
import io
import json
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
BANK = ROOT / "output" / "gen_bank.json"
HISTORY = ROOT / "output" / "pic_history.json"
GRID_OUT = ROOT / "output" / "vet_grid.jpg"

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}


def load() -> tuple[list[dict], set[str]]:
    try:
        items = json.loads(BANK.read_text(encoding="utf-8")).get("items", [])
    except (OSError, ValueError):
        items = []
    try:
        hist = set(json.loads(HISTORY.read_text(encoding="utf-8")).get("urls", []))
    except (OSError, ValueError):
        hist = set()
    return items, hist


def save(items: list[dict]) -> None:
    BANK.write_text(json.dumps({"items": items, "updated": "manual-vet"},
                               ensure_ascii=False, indent=1), encoding="utf-8")


def parse_idxs(arg: str) -> list[int]:
    return [int(x) for x in arg.replace("，", ",").split(",") if x.strip()]


def main() -> int:
    items, hist = load()
    args = sys.argv[1:]

    if "--vetted" in args or "--unvetted" in args:
        flag = "--vetted" in args
        arg = args[args.index("--vetted")] if flag else args[args.index("--unvetted")]
        idxs = parse_idxs(args[args.index(arg) + 1])
        for i in idxs:
            if 0 <= i < len(items):
                items[i]["vetted"] = flag
                print(f"{'标记已目检' if flag else '撤销目检'} #{i} {items[i].get('fromPageTitleEnc')}")
        save(items)
        print("库存已更新")
        return 0

    if "--status" in args:
        v = sum(1 for it in items if it.get("vetted"))
        fresh = sum(1 for it in items if it.get("vetted") and it["thumbURL"] not in hist)
        print(f"库存 {len(items)} 张：已目检 {v}，未目检 {len(items) - v}；已目检且未推过 {fresh}")
        for i, it in enumerate(items):
            mark = "V" if it.get("vetted") else " "
            used = "已推" if it["thumbURL"] in hist else "未推"
            print(f"  {mark}#{i:02d} {used} {it.get('fromPageTitleEnc')}")
        return 0

    # 默认：拼编号网格（含肤色占比参考；注意肤色比无法判定裸体，仅辅助）
    from PIL import Image, ImageDraw
    import daily_pic as dp

    W, H = 180, 225
    cols = 6
    rows = (len(items) + cols - 1) // cols or 1
    grid = Image.new("RGB", (cols * W + 8, rows * H + 8), "white")
    draw = ImageDraw.Draw(grid)
    for i, it in enumerate(items):
        try:
            r = requests.get(it["thumbURL"], headers=UA, timeout=60)
            im = Image.open(io.BytesIO(r.content)).convert("RGB").resize((W - 4, H - 20))
            skin = dp.skin_ratio(r.content) or 0
        except Exception as e:
            print(f"#{i} 下载失败 {type(e).__name__}: {e}")
            continue
        x, y = (i % cols) * W + 4, (i // cols) * H + 16
        grid.paste(im, (x, y))
        tag = "V" if it.get("vetted") else " "
        draw.text((x + 2, y - 14), f"{tag}#{i:02d} s={skin:.2f}", fill="black")
    GRID_OUT.parent.mkdir(parents=True, exist_ok=True)
    grid.save(GRID_OUT, quality=85)
    print(f"网格已保存: {GRID_OUT}（{len(items)} 张，V=已目检）")
    return 0


if __name__ == "__main__":
    import io  # noqa: E402  (供 grid 分支 PIL 用)
    sys.exit(main())