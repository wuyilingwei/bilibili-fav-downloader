# bilibili-fav-audio-downloader

Bilibili 收藏夹音频批量下载器，通过 **aria2 RPC** 远程推送下载任务。

## 特性

- 启动时自动测试 aria2 可达性，失败立即报错退出
- **逐页处理**：每获取一页收藏夹数据立即发送下载，不等全部加载完毕
- **本地缓存**（`cache/fav_<id>.json`）：记录已拉取的页和已发送的 BVID，支持断点续传
- 自动选取最高质量音频（FLAC > Dolby EAC3 > AAC）
- 支持多 P 视频，支持 Cookie 认证 & 访客模式
- 412 风控自动退避重试（15s → 30s）
- 仅依赖 Python 3.8+ 标准库，无需安装第三方包

---

## 快速开始

```bash
# 访客模式（公开收藏夹）
python3 bilibili_fav_audio.py \
  --fav 1566616540 \
  --aria2-host <your-aria2-host> \
  --aria2-secret <your-secret> \
  --dir /fav1

# Cookie 模式（推荐，支持私有收藏夹 / 高音质）
python3 bilibili_fav_audio.py \
  --fav "https://space.bilibili.com/535047440/favlist?fid=1566616540" \
  --cookie "SESSDATA=xxx; buvid3=yyy" \
  --aria2-host <your-aria2-host> \
  --aria2-secret <your-secret> \
  --dir /fav1

# 自定义延迟（降低风控概率）
python3 bilibili_fav_audio.py --fav 1566616540 \
  --video-delay 3 --page-delay 8 \
  --aria2-host <your-aria2-host> --aria2-secret <your-secret> --dir /fav1

# 清除缓存重新下载
python3 bilibili_fav_audio.py --fav 1566616540 --clear-cache ...

# 仅预览（不发送 aria2）
python3 bilibili_fav_audio.py --fav 1566616540 --dry-run
```

---

## 参数说明

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--fav` | 必填 | 收藏夹 URL 或纯 fid |
| `--cookie` | 空 | Bilibili Cookie（SESSDATA, buvid3 等） |
| `--aria2-host` | `<your-aria2-host>` | aria2 RPC 主机 |
| `--aria2-port` | `6800` | aria2 RPC 端口 |
| `--aria2-secret` | `<your-secret>` | aria2 RPC 密钥 |
| `--aria2-protocol` | `http` | `http` 或 `https` |
| `--aria2-path` | 空 | RPC 路径前缀（不含 `/jsonrpc`） |
| `--dir` | 空 | aria2 下载目录，空=aria2 全局默认 |
| `--video-delay` | `2.0` | 同页视频间延迟（秒） |
| `--page-delay` | `5.0` | 翻页间延迟（秒） |
| `--stream-delay` | `1.5` | 同视频多P间延迟（秒） |
| `--cache-dir` | `cache` | 缓存目录 |
| `--clear-cache` | false | 清除缓存后重新开始 |
| `--page-size` | `20` | 每页视频数（API 最大 20） |
| `--dry-run` | false | 仅列出，不推送 aria2 |
| `--limit` | 0 (全部) | 最多处理 N 个视频 |

---

## 运行流程

```
[1/4] 测试 aria2 连通性      ← 失败立即退出
[2/4] 初始化 Cookie
[3/4] 获取 WBI 签名 Key
[4/4] 加载缓存

── 第 1 页  [拉取中…] 20 个视频 ──
  [ 1/20] 视频标题 (BVxxxx)
         → mp4a.40.2  192 kbps  →  视频标题.m4a
         ✓ gid: abc123
  ...
  [翻页延迟 5s…]

── 第 2 页  [缓存]  20 个视频 ──
  ...

[完成]  ✓ 成功 40  ✗ 失败 0  → 跳过 5  共处理 45 条
```

---

## 如何获取 Cookie

**方式 A（推荐）：浏览器直接复制，无需登录**
1. 打开 bilibili.com
2. F12 → Application → Cookies → `bilibili.com`
3. 复制 `buvid3` 值
4. `--cookie "buvid3=xxx"`

**方式 B：登录账号获得高音质**
1. 登录 bilibili.com
2. 同上步骤，额外复制 `SESSDATA`
3. `--cookie "SESSDATA=aaa; buvid3=bbb"`

---

## 缓存文件格式

`cache/fav_1566616540.json`：

```json
{
  "media_id": 1566616540,
  "page_size": 20,
  "pages": {
    "1": { "fetched_at": "2026-05-26T12:00:00", "has_more": true, "medias": [...] }
  },
  "sent": ["BVxxxx", "BVyyyy"]
}
```

---

## 实现原理

参考 [Bilibili-Evolved](https://github.com/the1812/Bilibili-Evolved) 的 DASH 音频提取逻辑，
以及 [bilibili-API-collect](https://github.com/SocialSisterYi/bilibili-API-collect) 的 WBI 签名算法。
