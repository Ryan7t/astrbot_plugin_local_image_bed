# astrbot_plugin_local_image_bed

一个 AstrBot 本地图床插件。把图片保存到你自己的服务器上，返回一个可以长期打开的图片链接。

> **部署提示：** 本插件涉及 Docker 端口映射、Nginx 反向代理等服务器运维操作。有代码和运维基础的同学上手会比较简单；如果你对某些步骤不太理解，可以把本文档和报错信息直接丢给 GPT / Claude 等 AI 大模型，让它一步步教你操作。

**适合场景：**

- 不想依赖第三方图床，图片存在自己服务器上更放心
- 希望图片链接长期有效、可追溯
- 希望减少聊天中图片反复下载/上传的流量消耗

---

## 工作原理

插件启动后，会在 AstrBot 进程里额外开一个小型 HTTP 服务（默认端口 `18345`），流程很简单：

1. 你上传一张图片（通过聊天指令或 HTTP 接口）
2. 插件把图片保存到服务器本地目录
3. 返回一个永久链接，比如 `https://你的域名/imgbed/i/xxxx`

因为这个 HTTP 服务和 AstrBot 自带的 WebUI 不在同一个端口上，所以一般需要通过 Nginx 等工具做一个"反向代理"，把外部请求转发过来。下面会详细说明。

---

## 功能一览

- **图片持久化** — 图片存在你自己的服务器上，不会像 QQ 临时链接那样过期失效
- **HTTP 上传接口** — 支持文件上传和 base64 上传，方便程序调用
- **聊天指令上传** — 在聊天里直接发图就能拿到链接
- **内容去重** — 上传相同图片时自动复用已有链接，节省存储空间
- **上传鉴权** — 可选的 Token 验证，防止接口被他人滥用

---

## 数据存放位置

| 内容 | 路径 |
|------|------|
| 插件代码 | `data/plugins/astrbot_plugin_local_image_bed/` |
| 图片文件 | `data/plugin_data/astrbot_plugin_local_image_bed/images/` |
| 元数据库 | `data/plugin_data/astrbot_plugin_local_image_bed/images.db` |

---

## 聊天指令

在聊天窗口中直接发送即可：

| 指令 | 说明 |
|------|------|
| `/图床上传` + 一张图片 | 上传消息中的第一张图片，返回链接 |
| `/图床上传 <图片URL>` | 下载该 URL 的图片后上传，返回链接 |
| `/图床状态` | 查看插件当前运行状态（监听地址、Token 等） |
| `/图床帮助` | 显示指令帮助 |

---

## 配置项说明

安装插件后，在 AstrBot 管理面板的插件配置页面填写以下内容：

| 配置项 | 含义 | 推荐值 |
|------|------|------|
| `listen_host` | 插件 HTTP 服务监听的地址 | Docker 部署填 `0.0.0.0`；本机直接运行填 `127.0.0.1` |
| `listen_port` | 插件 HTTP 服务监听的端口 | `18345`（可自行更换未占用的端口） |
| `public_base_url` | 最终返回给用户的链接前缀 | `https://你的域名/imgbed` |
| `upload_token` | 上传接口的密码（Token） | 建议填一个随机字符串，如 `imgbed_abc123` |
| `max_upload_mb` | 单张图片大小上限（MB） | `10`（可按需调整，范围 1-100） |
| `enable_deduplicate` | 是否开启内容去重 | 开启 |

**简单理解：**

- `listen_host` + `listen_port` → 决定"服务在服务器的哪个地址和端口上运行"
- `public_base_url` → 决定"返回给用户的链接长什么样"
- `upload_token` → 决定"是否需要密码才能上传"

---

## 部署教程（Docker + Nginx）

> 以下是最常见的部署方式：AstrBot 跑在 Docker 里，前面有一个 Nginx 做反向代理。

### 第 1 步：Docker 暴露插件端口

编辑你的 `docker-compose.yml`，在 AstrBot 的 `ports` 部分增加图床端口映射：

```yaml
ports:
  - "6185:6185"                   # AstrBot WebUI（原有的）
  - "127.0.0.1:18345:18345"      # 图床插件
```

> `127.0.0.1:18345:18345` 表示这个端口只对本机开放（给 Nginx 转发用），外部无法直接访问，更安全。

修改后重建容器：

```bash
docker compose up -d --force-recreate astrbot
```

### 第 2 步：在插件面板填写配置

在 AstrBot 管理面板中填入：

| 配置项 | 填写内容 |
|------|------|
| `listen_host` | `0.0.0.0` |
| `listen_port` | `18345` |
| `public_base_url` | `https://你的域名/imgbed` |
| `upload_token` | 自己定一个随机字符串 |
| `max_upload_mb` | `10` |
| `enable_deduplicate` | 开启 |

填好后点保存，然后重载插件。

### 第 3 步：配置 Nginx 反向代理

在你网站的 Nginx 配置文件的 `server` 块中，添加以下内容：

```nginx
# 访问 /imgbed 时自动跳转到 /imgbed/
location = /imgbed {
    return 301 /imgbed/;
}

# 把 /imgbed/ 下的所有请求转发给图床插件
location ^~ /imgbed/ {
    proxy_pass http://127.0.0.1:18345/;
    proxy_set_header Host $http_host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Real-Port $remote_port;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Forwarded-Port $server_port;
    proxy_set_header REMOTE-HOST $remote_addr;
    proxy_connect_timeout 60s;
    proxy_send_timeout 600s;
    proxy_read_timeout 600s;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
}
```

**注意（这里容易踩坑）：**

- `location ^~ /imgbed/` 末尾的 `/` 不能省
- `proxy_pass http://127.0.0.1:18345/` 末尾的 `/` 也不能省
- 两个斜杠都写上，Nginx 才能正确地把 `/imgbed/health` 转发为后端的 `/health`

### 第 4 步：验证是否部署成功

按以下顺序逐步验证：

**1) 在 Docker 容器内测试插件是否正常启动**

```bash
docker exec astrbot sh -lc "curl -i http://127.0.0.1:18345/health"
```

期望看到：HTTP 200 + `{"ok": true, ...}`

**2) 通过域名测试 Nginx 转发是否正常**

```bash
curl -i https://你的域名/imgbed/health
```

期望看到：HTTP 200 + `{"ok": true, ...}`

**3) 在聊天中测试上传**

发送 `/图床上传` 并附带一张图片，机器人应返回包含 URL 的消息。

**4) 打开返回的链接**

在浏览器中访问返回的链接（类似 `https://你的域名/imgbed/i/xxxx`），应能直接看到图片。

---

## HTTP API 文档

如果你需要通过程序调用图床接口，可以参考以下说明。

### 健康检查

```
GET /health
```

返回 `{"ok": true, "status": "running", "time": "..."}`

### 上传图片

```
POST /upload
```

支持两种上传方式：

- **文件上传**（`multipart/form-data`）— 字段名用 `file` 或 `image`
- **Base64 上传**（`application/json`）— 字段名用 `image_base64` 或 `base64`

如果配置了 `upload_token`，请求时需要带上 Token：

- 方式一：请求头 `X-ImageBed-Token: 你的token`
- 方式二：URL 参数 `?token=你的token`

**返回示例：**

```json
{
  "ok": true,
  "id": "6d84a5f5e77a4f71",
  "url": "https://你的域名/imgbed/i/6d84a5f5e77a4f71",
  "mime": "image/png",
  "size": 123456,
  "created_at": "2026-03-04T12:00:00+00:00",
  "deduplicated": false
}
```

### 访问图片

```
GET /i/{image_id}
```

直接返回图片内容，带有长期缓存头。

### cURL 示例

**文件上传：**

```bash
curl -X POST "http://127.0.0.1:18345/upload" \
  -H "X-ImageBed-Token: 你的token" \
  -F "file=@./test.png"
```

**Base64 上传：**

```bash
curl -X POST "http://127.0.0.1:18345/upload" \
  -H "Content-Type: application/json" \
  -H "X-ImageBed-Token: 你的token" \
  -d '{"image_base64":"<base64编码内容>"}'
```

---

## 常见问题

**Q：能不能让图床和 AstrBot WebUI 共用 6185 端口？**

不行。6185 端口已经被 AstrBot WebUI 占用了，插件需要用一个单独的端口（默认 18345），然后通过 Nginx 把两个服务统一到同一个域名下。

**Q：访问域名返回 404？**

大概率是 Nginx 反向代理配置有误。重点检查两个地方：

- `location ^~ /imgbed/` — 末尾有 `/`
- `proxy_pass http://127.0.0.1:18345/` — 末尾有 `/`

**Q：健康检查通了，但返回的图片链接打不开？**

通常是 `public_base_url` 填得和实际的反代路径对不上。比如你 Nginx 配的路径是 `/imgbed`，那 `public_base_url` 就应该填 `https://你的域名/imgbed`。

**Q：上传时返回 401 错误？**

说明你配置了 `upload_token`，但请求时没带 Token 或者 Token 填错了。检查请求头 `X-ImageBed-Token` 的值是否和配置一致。

**Q：上传时提示"图片大小超出限制"？**

图片超过了 `max_upload_mb` 配置的上限，调大该配置或者压缩图片后再上传。
