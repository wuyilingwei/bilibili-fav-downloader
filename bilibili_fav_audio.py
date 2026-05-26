#!/usr/bin/env python3
"""
bilibili_fav_audio.py
~~~~~~~~~~~~~~~~~~~~~
Bilibili 收藏夹音频批量下载器（aria2 RPC 模式）

流程：
  1. 测试 aria2 RPC 可达性，失败立即退出
  2. 初始化 buvid3 cookie（本地生成，不发额外请求）
  3. 获取 WBI 签名 Key
  4. 加载本地缓存（cache/fav_{id}.json）
  5. 逐页处理：
       a. 若该页已缓存则直接读取，否则从 API 拉取并写入缓存
       b. 对该页每个视频逐一处理（跳过已发送的）
       c. 获取分 P → 获取音频流 → 发送 aria2
       d. 更新缓存中的已发送列表
       e. 页内视频间延迟 / 页间延迟
  6. 汇总输出

缓存文件格式（cache/fav_<media_id>.json）:
  {
    "media_id": int,
    "page_size": int,
    "pages": {
      "1": { "fetched_at": "...", "has_more": bool, "medias": [...] },
      ...
    },
    "sent": ["BVxxxx", ...]     ← 已成功发送到 aria2 的 BVID
  }

依赖: Python 3.8+ 标准库（无需安装第三方包）

参考实现:
  - Bilibili-Evolved DASH 音频提取: https://github.com/the1812/Bilibili-Evolved
  - bilibili-API-collect WBI 签名:   https://github.com/SocialSisterYi/bilibili-API-collect
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from functools import reduce

# ---------------------------------------------------------------------------
# WBI 签名
# ---------------------------------------------------------------------------

_MIXIN_KEY_ENC_TAB = [
    46, 47, 18,  2, 53,  8, 23, 32, 15, 50, 10, 31, 58,  3, 45, 35,
    27, 43,  5, 49, 33,  9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48,  7, 16, 24, 55, 40, 61, 26, 17,  0,  1, 60, 51, 30,  4,
    22, 25, 54, 21, 56, 59,  6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]


def _get_mixin_key(raw: str) -> str:
    return reduce(lambda s, i: s + raw[i], _MIXIN_KEY_ENC_TAB, "")[:32]


def _wbi_sign(params: dict, img_key: str, sub_key: str) -> dict:
    mixin_key = _get_mixin_key(img_key + sub_key)
    params = dict(params)
    params["wts"] = int(time.time())
    params = dict(sorted(params.items()))
    qs = urllib.parse.urlencode(
        {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in params.items()}
    )
    params["w_rid"] = hashlib.md5((qs + mixin_key).encode()).hexdigest()
    return params


def fetch_wbi_keys(cookies: str = "") -> tuple[str, str]:
    data = _api_get("https://api.bilibili.com/x/web-interface/nav", cookies=cookies)
    img_url: str = data["data"]["wbi_img"]["img_url"]
    sub_url: str = data["data"]["wbi_img"]["sub_url"]
    img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
    sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
    return img_key, sub_key


# ---------------------------------------------------------------------------
# 公共 HTTP（含 412 退避重试）
# ---------------------------------------------------------------------------

# Edge 131.0.2903.86 on Windows 10 —— 真实发布版本，含具体 build 号
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 "
    "Edg/131.0.2903.86"
)

# 通用请求头（WBI / pagelist / playurl 等）
_BASE_HEADERS = {
    "User-Agent":      _UA,
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer":         "https://www.bilibili.com/",
}

# 收藏夹专用（Origin/Referer 必须是 space.bilibili.com）
_FAV_HEADERS = {
    "User-Agent":      _UA,
    "Accept":          "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Origin":          "https://space.bilibili.com",
    "Referer":         "https://space.bilibili.com/",
    "Sec-Fetch-Site":  "same-site",
    "Sec-Fetch-Mode":  "cors",
    "Sec-Fetch-Dest":  "empty",
}


def _api_get(
    url: str,
    cookies: str = "",
    timeout: int = 15,
    extra_headers: dict | None = None,
) -> dict:
    """
    发起 GET 请求，412 风控时指数退避重试最多 3 次。
    extra_headers 可覆盖 _BASE_HEADERS（收藏夹请求传 _FAV_HEADERS）。
    """
    headers = {**_BASE_HEADERS, **(extra_headers or {})}
    max_retry = 3
    for attempt in range(max_retry):
        req = urllib.request.Request(url)
        for k, v in headers.items():
            req.add_header(k, v)
        if cookies:
            req.add_header("Cookie", cookies)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 412 and attempt < max_retry - 1:
                wait = 15 * (2 ** attempt)  # 15s → 30s → 放弃
                print(
                    f"  [风控] 触发 412，等待 {wait}s 后重试 "
                    f"({attempt + 1}/{max_retry - 1})…"
                )
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("多次重试后仍触发风控，请添加 --cookie 或稍后再试")


def init_buvid(existing: str = "") -> str:
    """本地生成 buvid3（不发网络请求），如已存在则直接返回。"""
    if "buvid3=" in existing:
        return existing
    buvid3 = str(uuid.uuid4()).upper() + "infoc"
    extra = f"buvid3={buvid3}"
    return f"{existing}; {extra}" if existing else extra


# ---------------------------------------------------------------------------
# aria2 RPC
# ---------------------------------------------------------------------------

def _aria2_rpc(
    method: str,
    params: list,
    host: str,
    port: int,
    secret: str,
    protocol: str,
    path: str,
    _max_retry: int = 3,
) -> object:
    """
    通用 aria2 JSON-RPC 调用，返回 result 字段。
    网络超时 / 临时断连时自动重试（最多 _max_retry 次）。
    """
    import socket as _socket

    rpc_url = f"{protocol}://{host}:{port}{path.rstrip('/')}/jsonrpc"
    payload = {
        "jsonrpc": "2.0",
        "id": f"bili_{int(time.time() * 1000)}",
        "method": method,
        "params": [f"token:{secret}"] + params,
    }
    body = json.dumps(payload).encode()

    last_err: Exception = RuntimeError("未知错误")
    for attempt in range(_max_retry):
        req = urllib.request.Request(
            rpc_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())
            if "error" in result:
                raise RuntimeError(f"aria2 错误: {result['error']}")
            return result["result"]
        except (urllib.error.URLError, _socket.timeout, OSError) as e:
            last_err = e
            if attempt < _max_retry - 1:
                wait = 3 * (attempt + 1)   # 3s → 6s → 放弃
                print(f"  [aria2] 超时/断连，{wait}s 后重试 ({attempt + 1}/{_max_retry - 1})…")
                time.sleep(wait)
            continue

    raise RuntimeError(f"aria2 多次重试失败 ({rpc_url}): {last_err}")


def aria2_ping(host: str, port: int, secret: str, protocol: str, path: str) -> str:
    """测试连通性，返回 aria2 版本号，失败抛出 RuntimeError。"""
    result = _aria2_rpc("aria2.getVersion", [], host, port, secret, protocol, path)
    return result.get("version", "unknown")  # type: ignore[union-attr]


def aria2_add_uri(
    uri: str,
    filename: str,
    referer: str,
    host: str,
    port: int,
    secret: str,
    protocol: str = "http",
    path: str = "",
    cookies: str = "",
    dir: str = "",
) -> str:
    """推送下载任务，返回 gid。"""
    headers = [f"Referer: {referer}"]
    if cookies:
        headers.append(f"Cookie: {cookies}")
    headers.append(f"User-Agent: {_BASE_HEADERS['User-Agent']}")

    options: dict = {
        "out": filename,
        "header": headers,
        "split": "16",
        "max-connection-per-server": "16",
        "allow-overwrite": "true",
    }
    if dir:
        options["dir"] = dir

    gid = _aria2_rpc(
        "aria2.addUri",
        [[uri], options],
        host, port, secret, protocol, path,
    )
    return str(gid)


# ---------------------------------------------------------------------------
# Bilibili API
# ---------------------------------------------------------------------------

def fetch_fav_page(
    media_id: int,
    page: int,
    page_size: int,
    cookies: str,
    uid: int = 0,
) -> tuple[list[dict], bool]:
    """
    拉取收藏夹第 page 页，返回 (medias, has_more)。
    uid 用于构造更精确的 Referer（可选）。
    """
    params = urllib.parse.urlencode({
        "media_id":    media_id,
        "pn":          page,
        "ps":          page_size,
        "keyword":     "",
        "order":       "mtime",
        "type":        0,
        "tid":         0,
        "platform":    "web",
        "web_location": "0.0",
    })
    url = f"https://api.bilibili.com/x/v3/fav/resource/list?{params}"

    # Referer 精确到收藏夹页面
    if uid:
        referer = (
            f"https://space.bilibili.com/{uid}/favlist"
            f"?fid={media_id}&ftype=create"
        )
    else:
        referer = f"https://space.bilibili.com/"

    fav_headers = {**_FAV_HEADERS, "Referer": referer}

    resp = _api_get(url, cookies, extra_headers=fav_headers)
    if resp["code"] != 0:
        raise RuntimeError(f"收藏夹 API 错误 {resp['code']}: {resp['message']}")
    data = resp["data"]
    medias: list[dict] = data.get("medias") or []
    has_more: bool = bool(data.get("has_more", False))
    return medias, has_more


def get_video_pages(bvid: str, cookies: str = "") -> list[dict]:
    data = _api_get(
        f"https://api.bilibili.com/x/player/pagelist?bvid={bvid}", cookies
    )
    if data["code"] != 0:
        raise RuntimeError(f"pagelist 错误 {data['code']}: {data['message']}")
    return data["data"]


def get_best_audio_stream(
    bvid: str,
    cid: int,
    cookies: str = "",
    img_key: str = "",
    sub_key: str = "",
) -> dict | None:
    """返回最高质量音频流 dict，或 None。"""
    params: dict = {
        "bvid": bvid,
        "cid": cid,
        "fnval": 16,
        "fnver": 0,
        "fourk": 1,
        "platform": "pc",
        "high_quality": 1,
    }
    if img_key and sub_key:
        params = _wbi_sign(params, img_key, sub_key)

    qs = urllib.parse.urlencode(params)
    data = _api_get(
        f"https://api.bilibili.com/x/player/wbi/playurl?{qs}", cookies
    )
    if data["code"] != 0:
        raise RuntimeError(f"playurl 错误 {data['code']}: {data['message']}")

    dash = (data.get("data") or {}).get("dash")
    if not dash:
        return None

    candidates: list[dict] = []

    for a in dash.get("audio") or []:
        candidates.append(_map_audio(a, "aac"))

    for a in (dash.get("dolby") or {}).get("audio") or []:
        candidates.append(_map_audio(a, "eac3"))

    flac = (dash.get("flac") or {}).get("audio")
    if flac:
        item = _map_audio(flac, "flac")
        item["bandwidth"] += 10_000_000
        candidates.append(item)

    if not candidates:
        return None
    candidates.sort(key=lambda x: x["bandwidth"], reverse=True)
    return candidates[0]


def _map_audio(raw: dict, default_codec: str) -> dict:
    url = raw.get("baseUrl") or raw.get("base_url") or ""
    backup = raw.get("backupUrl") or raw.get("backup_url") or []
    if url.startswith("http://"):
        url = "https://" + url[7:]
    return {
        "url": url,
        "backup_urls": [
            u if not u.startswith("http://") else "https://" + u[7:]
            for u in backup
        ],
        "bandwidth": raw.get("bandwidth", 0),
        "codecs": raw.get("codecs") or default_codec,
        "mime_type": raw.get("mimeType") or raw.get("mime_type") or "audio/mp4",
    }


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------

def _cache_path(cache_dir: str, media_id: int) -> str:
    return os.path.join(cache_dir, f"fav_{media_id}.json")


def load_cache(cache_dir: str, media_id: int, page_size: int) -> dict:
    path = _cache_path(cache_dir, media_id)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            # 兼容旧格式
            data.setdefault("pages", {})
            data.setdefault("sent", [])
            return data
        except Exception as e:
            print(f"[警告] 缓存读取失败，将重新创建: {e}")
    return {
        "media_id": media_id,
        "page_size": page_size,
        "pages": {},
        "sent": [],
    }


def save_cache(cache: dict, cache_dir: str, media_id: int) -> None:
    os.makedirs(cache_dir, exist_ok=True)
    path = _cache_path(cache_dir, media_id)
    cache["last_saved"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)  # 原子替换，防止写入中断导致缓存损坏


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def parse_fid(raw: str) -> int:
    m = re.search(r"fid=(\d+)", raw)
    if m:
        return int(m.group(1))
    m = re.search(r"/(\d+)(?:[/?#]|$)", raw)
    if m:
        return int(m.group(1))
    if raw.strip().isdigit():
        return int(raw.strip())
    raise ValueError(f"无法从以下内容解析 fid：{raw!r}")


def parse_uid(raw: str) -> int:
    """从收藏夹 URL 提取 UID（space.bilibili.com/{uid}/...），失败返回 0。"""
    m = re.search(r"space\.bilibili\.com/(\d+)", raw)
    return int(m.group(1)) if m else 0


def safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip("._")


def audio_ext(codecs: str, mime: str) -> str:
    c = codecs.lower()
    if "flac" in c:
        return "flac"
    if "eac3" in c or "ec-3" in c:
        return "eac3"
    if "opus" in c:
        return "opus"
    return "m4a"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bilibili_fav_audio",
        description="Bilibili 收藏夹音频批量下载（aria2 RPC）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 访客模式（公开收藏夹）
  python3 bilibili_fav_audio.py --fav 1566616540 \\
      --aria2-host 107.189.10.208 --aria2-secret Vs2Tn6eZx5SgZv4n --dir /fav1

  # Cookie 模式（私有收藏夹 / 高音质）
  python3 bilibili_fav_audio.py \\
      --fav "https://space.bilibili.com/535047440/favlist?fid=1566616540" \\
      --cookie "SESSDATA=xxx; buvid3=yyy" \\
      --aria2-host 107.189.10.208 --aria2-secret Vs2Tn6eZx5SgZv4n --dir /fav1

  # 仅预览（不发送 aria2）
  python3 bilibili_fav_audio.py --fav 1566616540 --dry-run

  # 清除缓存并重新下载
  python3 bilibili_fav_audio.py --fav 1566616540 --clear-cache ...
""",
    )
    p.add_argument("--fav", required=True,
                   help="收藏夹 URL 或纯 fid")
    p.add_argument("--cookie", default="",
                   help="Bilibili Cookie（SESSDATA=xxx; buvid3=yyy 等）")
    # aria2
    p.add_argument("--aria2-host",     default="127.0.0.1",
                   help="aria2 RPC 主机 (默认: 127.0.0.1)")
    p.add_argument("--aria2-port",     type=int, default=6800,
                   help="aria2 RPC 端口 (默认: 6800)")
    p.add_argument("--aria2-secret",   default="",
                   help="aria2 RPC 密钥")
    p.add_argument("--aria2-protocol", default="http", choices=["http", "https"])
    p.add_argument("--aria2-path",     default="",
                   help="RPC 路径前缀，不含 /jsonrpc（默认: 空）")
    p.add_argument("--dir",            default="",
                   help="aria2 下载目录（默认: aria2 全局配置目录）")
    # 延迟
    p.add_argument("--video-delay",    type=float, default=2.0,
                   help="同页视频间延迟秒数（默认: 2.0）")
    p.add_argument("--page-delay",     type=float, default=5.0,
                   help="翻页间延迟秒数（默认: 5.0）")
    p.add_argument("--stream-delay",   type=float, default=1.5,
                   help="同视频多 P 间延迟秒数（默认: 1.5）")
    # 缓存
    p.add_argument("--cache-dir",      default="cache",
                   help="缓存目录（默认: ./cache）")
    p.add_argument("--clear-cache",    action="store_true",
                   help="清除该收藏夹缓存后重新开始")
    # 其他
    p.add_argument("--page-size",      type=int, default=40,
                   help="每页视频数（默认: 40，B站实测支持）")
    p.add_argument("--dry-run",        action="store_true",
                   help="仅列出信息，不推送 aria2")
    p.add_argument("--limit",          type=int, default=0,
                   help="最多处理 N 个视频（0=全部）")
    return p


# ---------------------------------------------------------------------------
# 主程序
# ---------------------------------------------------------------------------

def main() -> None:
    args = build_arg_parser().parse_args()

    # ── 解析收藏夹 ID 和用户 UID ──────────────────────────────────────────────
    try:
        media_id = parse_fid(args.fav)
    except ValueError as e:
        print(f"[错误] {e}", file=sys.stderr)
        sys.exit(1)
    uid = parse_uid(args.fav)  # 用于构造精确 Referer，0 表示未知

    cookies = args.cookie.strip()

    # ── 1. 测试 aria2 可达性 ──────────────────────────────────────────────────
    if not args.dry_run:
        print("[1/4] 测试 aria2 连通性…", end=" ", flush=True)
        try:
            ver = aria2_ping(
                args.aria2_host, args.aria2_port,
                args.aria2_secret, args.aria2_protocol, args.aria2_path,
            )
            print(f"OK  (aria2 {ver})")
        except RuntimeError as e:
            print(f"失败\n[错误] {e}", file=sys.stderr)
            sys.exit(1)

    # ── 2. 初始化 Cookie（buvid3） ────────────────────────────────────────────
    print("[2/4] 初始化 Cookie…", end=" ", flush=True)
    cookies = init_buvid(cookies)
    print(f"{'Cookie 模式' if args.cookie.strip() else '访客模式'}  buvid3 已就绪")

    # ── 3. 获取 WBI Keys ──────────────────────────────────────────────────────
    print("[3/4] 获取 WBI 签名 Key…", end=" ", flush=True)
    img_key, sub_key = "", ""
    try:
        img_key, sub_key = fetch_wbi_keys(cookies)
        print("OK")
    except Exception as e:
        print(f"失败（{e}）\n       playurl 将不携带签名，部分内容可能受限")

    # ── 4. 加载缓存 ───────────────────────────────────────────────────────────
    print("[4/4] 加载缓存…", end=" ", flush=True)
    if args.clear_cache:
        path = _cache_path(args.cache_dir, media_id)
        if os.path.exists(path):
            os.remove(path)
            print("已清除旧缓存，重新开始")
        else:
            print("无缓存文件")
    cache = load_cache(args.cache_dir, media_id, args.page_size)
    sent_set: set[str] = set(cache.get("sent", []))
    cached_pages: dict = cache.get("pages", {})
    print(
        f"已缓存 {len(cached_pages)} 页  |  "
        f"已发送 {len(sent_set)} 个 BVID"
    )

    # ── 信息摘要 ──────────────────────────────────────────────────────────────
    print()
    print(f"  收藏夹 ID : {media_id}")
    print(f"  aria2 RPC : {args.aria2_protocol}://{args.aria2_host}:{args.aria2_port}"
          f"{args.aria2_path}/jsonrpc")
    print(f"  下载目录  : {args.dir or '(aria2 默认目录)'}")
    print(f"  延迟设置  : 视频间 {args.video_delay}s | 翻页 {args.page_delay}s | 多P间 {args.stream_delay}s")
    print(f"  缓存目录  : {os.path.abspath(args.cache_dir)}")
    print()

    # ── 逐页处理 ──────────────────────────────────────────────────────────────
    ok = fail = skip = total_processed = 0
    page_num = 1
    limit_hit = False
    failed_list: list[dict] = []   # [{bvid, title, reason}]

    while True:
        page_key = str(page_num)

        # ── 获取页数据（优先读缓存） ──────────────────────────────────────────
        if page_key in cached_pages:
            page_data = cached_pages[page_key]
            medias: list[dict] = page_data["medias"]
            has_more: bool = page_data["has_more"]
            print(f"── 第 {page_num} 页  [缓存]  {len(medias)} 个视频 ──")
        else:
            print(f"── 第 {page_num} 页  [拉取中…] ", end="", flush=True)
            try:
                medias, has_more = fetch_fav_page(
                    media_id, page_num, args.page_size, cookies, uid
                )
            except RuntimeError as e:
                print(f"失败\n[错误] {e}", file=sys.stderr)
                if "412" in str(e) or "风控" in str(e):
                    print("提示: 请添加 --cookie \"buvid3=xxx; SESSDATA=yyy\" 后重试",
                          file=sys.stderr)
                break

            if not medias:
                print("空页，结束")
                break

            print(f"{len(medias)} 个视频{'  (最后一页)' if not has_more else ''}")

            # 写入缓存
            cached_pages[page_key] = {
                "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "has_more": has_more,
                "medias": medias,
            }
            cache["pages"] = cached_pages
            save_cache(cache, args.cache_dir, media_id)

        # ── 处理该页每个视频 ──────────────────────────────────────────────────
        for vid_idx, video in enumerate(medias, 1):
            bvid: str   = video.get("bvid", "")
            title: str  = video.get("title") or "未知标题"
            fav_type: int = video.get("type", 2)

            total_processed += 1
            if args.limit > 0 and total_processed > args.limit:
                limit_hit = True
                break

            prefix = f"  [{vid_idx:>2}/{len(medias)}]"

            # 跳过非视频类型
            if not bvid or fav_type != 2:
                print(f"{prefix} 跳过（非视频）: {title[:40]}")
                skip += 1
                continue

            # 跳过已发送
            if bvid in sent_set:
                print(f"{prefix} 跳过（已发送）: {title[:40]}")
                skip += 1
                continue

            print(f"{prefix} {title[:50]}  ({bvid})")

            if args.dry_run:
                print(f"         [DRY-RUN]")
                continue

            # 获取分 P
            try:
                pages = get_video_pages(bvid, cookies)
            except Exception as e:
                reason = f"获取分P失败: {e}"
                print(f"         ✗ {reason}")
                fail += 1
                failed_list.append({"bvid": bvid, "title": title, "reason": reason})
                time.sleep(args.video_delay)
                continue

            video_ok = True
            for p_idx, page in enumerate(pages):
                cid: int  = page["cid"]
                part: str = page.get("part") or title
                label     = part if len(pages) > 1 else title

                # 获取最佳音频流
                try:
                    stream = get_best_audio_stream(
                        bvid, cid, cookies, img_key, sub_key
                    )
                except Exception as e:
                    print(f"         ✗ 音频流失败 (P{p_idx + 1}): {e}")
                    video_ok = False
                    time.sleep(args.stream_delay)
                    continue

                if stream is None:
                    print(f"         ✗ 无可用音频流 (P{p_idx + 1})")
                    video_ok = False
                    continue

                ext   = audio_ext(stream["codecs"], stream["mime_type"])
                fname = safe_filename(label) + f".{ext}"
                bw    = stream["bandwidth"] // 1000
                print(f"         → {stream['codecs']}  {bw} kbps  →  {fname}")

                # 发送 aria2
                try:
                    gid = aria2_add_uri(
                        uri=stream["url"],
                        filename=fname,
                        referer=f"https://www.bilibili.com/video/{bvid}",
                        host=args.aria2_host,
                        port=args.aria2_port,
                        secret=args.aria2_secret,
                        protocol=args.aria2_protocol,
                        path=args.aria2_path,
                        cookies=cookies,
                        dir=args.dir,
                    )
                    print(f"         ✓ gid: {gid}")
                except Exception as e:
                    print(f"         ✗ aria2 推送失败: {e}")
                    video_ok = False

                if p_idx < len(pages) - 1:
                    time.sleep(args.stream_delay)

            # 记录成功/失败，并更新缓存
            if video_ok:
                ok += 1
                sent_set.add(bvid)
                cache["sent"] = list(sent_set)
                # 从失败列表中移除（若之前因某P失败记录过）
                cache.pop("failed", None)
            else:
                fail += 1
                failed_list.append({"bvid": bvid, "title": title, "reason": "部分分P失败"})

            # 每次都写缓存（同时持久化失败列表，供下次断点参考）
            cache["failed"] = failed_list
            save_cache(cache, args.cache_dir, media_id)

            # 视频间延迟
            if vid_idx < len(medias):
                time.sleep(args.video_delay)

        if limit_hit:
            print(f"\n[*] 已达到 --limit {args.limit} 限制，停止")
            break

        if not has_more:
            break

        # 翻页延迟
        print(f"\n  [翻页延迟 {args.page_delay}s…]\n")
        time.sleep(args.page_delay)
        page_num += 1

    # ── 汇总 ─────────────────────────────────────────────────────────────────
    print("\n" + "─" * 55)
    if args.dry_run:
        print(f"[完成] DRY-RUN  处理了 {total_processed} 条记录（跳过 {skip} 个）")
    else:
        print(
            f"[完成]  ✓ 成功 {ok}  ✗ 失败 {fail}  → 跳过 {skip}  "
            f"共处理 {total_processed} 条"
        )
        if failed_list:
            print(f"\n  失败视频（共 {len(failed_list)} 个）：")
            for item in failed_list:
                print(f"    ✗ {item['bvid']}  {item['title'][:45]}  [{item['reason']}]")
        print(f"\n  缓存: {_cache_path(args.cache_dir, media_id)}")


if __name__ == "__main__":
    main()
