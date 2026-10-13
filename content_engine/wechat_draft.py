# coding=utf-8
"""微信公众号草稿箱写入（draft/add）

只写草稿，不发布。
- draft/add：订阅号和服务号都可用，个人号即可（无需微信认证）
- freepublish/submit（真正发布）：仅认证账号，且需 IP 白名单 —— 本模块刻意不做

环境变量：
  WECHAT_APPID / WECHAT_APPSECRET  （必填，在公众平台后台「设置与开发→基本配置」取）
  WECHAT_IP_ALLOWLIST_OK=1         （可选，标记你已配好白名单；GitHub runner 出口 IP 会变）

白名单说明：调 draft/add 也会校验 IP 白名单。GitHub Actions runner 的出口 IP 不固定，
所以要么用固定出口（自建 runner / 固定 VPS 跑本模块），要么把 GitHub 的 IP 段加进白名单
（GitHub 官方 meta API：https://api.github.com/meta → actions.runner_ips）。
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

API_BASE = "https://api.weixin.qq.com/cgi-bin"
TOKEN_URL = f"{API_BASE}/token"
DRAFT_ADD = f"{API_BASE}/draft/add"
MATERIAL_UPLOAD = f"{API_BASE}/material/add_material"   # 永久素材（封面 thumb_media_id）
UPLOAD_IMG = f"{API_BASE}/media/uploadimg"             # 正文插图（图文专用）

WECHAT_ERR = {
    40001: "AppSecret 错误或 AppID 不匹配",
    40164: "调用来源 IP 不在白名单里（公众平台后台 → 基本配置 → IP 白名单）",
    45009: "接口调用超过限制（同一 IP 每分钟有上限，加 sleep 重试）",
    48001: "api unauthorized（该接口对当前账号类型不可用）",
    40007: "素材 media_id 无效或已过期",
    41005: "缺少 media_id 或文件参数",
    53503: "草稿未通过发布检查（内容含违规词或格式问题）",
}


class WeChatError(RuntimeError):
    pass


def _cfg() -> tuple[str, str]:
    appid = os.environ.get("WECHAT_APPID", "").strip()
    secret = os.environ.get("WECHAT_APPSECRET", "").strip()
    if not appid or not secret:
        raise WeChatError("缺少 WECHAT_APPID / WECHAT_APPSECRET 环境变量")
    return appid, secret


def get_access_token() -> str:
    """带本地缓存的 access_token 获取（微信有 2 小时有效期，别每次都换）"""
    appid, secret = _cfg()
    cache_path = os.environ.get(
        "WECHAT_TOKEN_CACHE",
        os.path.join(os.path.dirname(__file__), "..", "output", "_wx_token.json"),
    )
    try:
        import json
        with open(cache_path, encoding="utf-8") as f:
            c = json.load(f)
        if c.get("appid") == appid and c.get("expire_at", 0) > time.time() + 120:
            return c["token"]
    except (OSError, ValueError, KeyError):
        pass

    r = requests.get(TOKEN_URL, params={
        "grant_type": "client_credential", "appid": appid, "secret": secret,
    }, timeout=20)
    j = r.json()
    if "access_token" not in j:
        code = j.get("errcode")
        raise WeChatError(f"取 token 失败 errcode={code}: {WECHAT_ERR.get(code, j.get('errmsg'))}")
    token = j["access_token"]
    try:
        import json
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({"appid": appid, "token": token,
                       "expire_at": time.time() + j.get("expires_in", 7200) - 300}, f)
    except OSError:
        pass
    return token


def _call(url: str, payload: Any, retries: int = 3) -> dict:
    token = get_access_token()
    last: dict = {}
    for attempt in range(retries):
        r = requests.post(url, params={"access_token": token}, json=payload, timeout=30)
        j = r.json()
        if "errcode" not in j or j.get("errcode") == 0:
            return j
        last = j
        code = j.get("errcode")
        if code in (40001, 40164, 48001):
            break                                   # 配置类错误，重试无意义
        if code == 45009 and attempt < retries - 1:
            time.sleep(5 * (attempt + 1))
            continue
        break
    code = last.get("errcode")
    raise WeChatError(f"{code}: {WECHAT_ERR.get(code, last.get('errmsg'))}")


# ──────────────────── 素材上传 ────────────────────
def upload_thumb(path: str) -> str:
    """永久素材（封面图）→ 返回 media_id（thumb_media_id 必填）"""
    token = get_access_token()
    with open(path, "rb") as f:
        files = {"media": ("thumb.jpg", f, "image/jpeg")}
        r = requests.post(MATERIAL_UPLOAD, params={"access_token": token},
                          files=files, timeout=60)
    j = r.json()
    if "media_id" not in j:
        code = j.get("errcode")
        raise WeChatError(f"封面上传失败 {code}: {WECHAT_ERR.get(code, j.get('errmsg'))}")
    return j["media_id"]


def upload_content_img(path: str) -> str:
    """正文插图 → 返回可直接放进 HTML 的 img src（需先传永久素材换取 media_id）"""
    with open(path, "rb") as f:
        files = {"media": ("img.jpg", f, "image/jpeg")}
        r = requests.post(UPLOAD_IMG, params={"access_token": get_access_token()},
                          files=files, timeout=60)
    j = r.json()
    if "url" not in j:
        code = j.get("errcode")
        raise WeChatError(f"正文图上传失败 {code}: {WECHAT_ERR.get(code, j.get('errmsg'))}")
    return j["url"]


# ──────────────────── 草稿写入 ────────────────────
def create_draft(title: str, content_html: str, digest: str,
                 thumb_media_id: str | None = None,
                 author: str = "", content_source_url: str = "") -> str:
    """写入草稿箱，返回 media_id。

    微信草稿 articles 至少要有 title / content / thumb_media_id 之一满足；
    未认证号没有永久素材配额时允许 thumb_media_id 为空，但可能显示无封面。
    """
    article = {
        "title": title,
        "author": author,
        "digest": digest,
        "content": content_html,
        "content_source_url": content_source_url,
        "thumb_media_id": thumb_media_id or "",
        "need_open_comment": 0,
        "only_fans_can_comment": 0,
    }
    j = _call(DRAFT_ADD, {"articles": [article]})
    media_id = ((j.get("media_id") or "") or "")
    if not media_id:
        raise WeChatError(f"草稿创建返回异常: {j}")
    return media_id


def is_configured() -> bool:
    return bool(os.environ.get("WECHAT_APPID", "").strip()
                and os.environ.get("WECHAT_APPSECRET", "").strip())


if __name__ == "__main__":
    print("公众号配置：", "已配置" if is_configured() else "未配置（缺 WECHAT_APPID/WECHAT_APPSECRET）")
    if is_configured():
        try:
            print("access_token 获取成功，前缀：", get_access_token()[:12], "...")
        except Exception as e:
            print("取 token 失败：", e)