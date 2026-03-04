# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

AstrBot 本地图床插件。在 AstrBot 进程内启动独立 aiohttp HTTP 服务器，接收图片上传后保存到本地文件系统，并返回持久化访问链接。支持聊天指令上传和 HTTP API 上传，相同内容按 SHA256 去重。

## 架构

这是一个**单文件插件项目**，所有逻辑在 `main.py` 中，无独立依赖管理文件，依赖由 AstrBot 框架提供。

核心组成：

- `detect_image_type()` — 通过 magic number 识别图片格式（JPEG/PNG/GIF/WebP/BMP/TIFF）
- `UploadRateLimiter` — 内存级 per-IP 滑动窗口速率限制器，asyncio.Lock 保护
- `ImageStore` — 数据持久化层，SQLite 存元数据 + 文件系统存图片，asyncio.Lock 保护并发写入；提供 `save_image`/`get_image`/`delete_image`/`cleanup_older_than`
- `LocalImageBedPlugin(Star)` — 插件主类，继承 AstrBot 的 `Star` 基类，包含 HTTP 服务器、聊天指令处理、审计日志

插件通过 `@register()` 装饰器注册到 AstrBot 框架，生命周期由 `initialize()` / `terminate()` 管理。

### 数据流

```
HTTP 上传 → 速率限制 → Token 鉴权 → 读取 payload → detect_image_type → SHA256 去重
         → 写入文件系统 + INSERT SQLite → 审计日志 → 返回 {id, url}
聊天上传 → 提取 Image 组件或 URL → 同上流程
访问     → GET /i/{id} → SQLite 查询 → FileResponse + 缓存头
```

### 审计日志

所有上传操作（HTTP 和聊天指令）固定记录到 `{plugin_data}/audit_upload.jsonl`，JSONL 格式，asyncio.Lock 保护写入。审计日志不可关闭。

### 数据存储位置

- 图片文件：`{astrbot_data}/plugin_data/astrbot_plugin_local_image_bed/images/`
- 元数据库：`{astrbot_data}/plugin_data/astrbot_plugin_local_image_bed/images.db`
- 审计日志：`{astrbot_data}/plugin_data/astrbot_plugin_local_image_bed/audit_upload.jsonl`

### HTTP 路由

| 路由 | 方法 | 用途 |
|------|------|------|
| `/` | GET | 索引信息 |
| `/health` | GET | 健康检查 |
| `/i/{image_id}` | GET | 获取图片 |
| `/upload` | POST | 上传图片（multipart 或 JSON base64） |

### 聊天指令

- `/图床上传` + 图片 或 URL — 上传并返回链接
- `/图床状态` — 查看运行状态
- `/图床删除 <image_id>` — 管理员删除指定图片（需 ADMIN 权限）
- `/图床清理天数 <days> [limit]` — 管理员批量清理旧图（需 ADMIN 权限）
- `/图床帮助` — 显示使用说明

管理员指令通过 `@filter.permission_type(filter.PermissionType.ADMIN)` 装饰器限制。

## AstrBot 插件 API 使用

```python
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.utils.io import download_image_by_url
```

- 配置通过 `self.config.get(key, default)` 读取，对应 `_conf_schema.json` 中定义的 9 个配置项
- 命令处理使用 `@filter.command("指令名")` 装饰器，handler 是 async generator（用 `yield event.plain_result()`）
- 管理员指令需叠加 `@filter.permission_type(filter.PermissionType.ADMIN)` 装饰器
- 图片组件通过 `Image.convert_to_file_path()` 获取本地路径

## 配置项（_conf_schema.json）

`listen_host`、`listen_port`、`public_base_url`、`upload_token`、`enable_url_upload`（默认关闭，SSRF 风险）、`upload_rate_limit_count`、`upload_rate_limit_window_sec`、`max_upload_mb`、`enable_deduplicate`

## 验证方式

无自动化测试。手动验证：

```bash
# 健康检查
curl http://127.0.0.1:18345/health

# 文件上传
curl -X POST http://127.0.0.1:18345/upload \
  -H "X-ImageBed-Token: <token>" \
  -F "file=@./test.png"
```
