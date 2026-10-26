# API 指南

完整的接口规范是 [`sdk/openapi.yaml`](../sdk/openapi.yaml)（OpenAPI 3.1，可以直接导入 Postman，或者生成 Swift / Kotlin 客户端）。这篇讲怎么用，以及容易踩的坑。

- 线上地址：`https://cerebel.tech/playground`（主站 Vercel 代理转发到应用服务器，所以带路径前缀 `/playground`）
- 本地开发：`http://127.0.0.1:8092`（见[快速上手](getting-started.md)）
- 离线模拟：`cerebel mock api`（见 [Python SDK](../sdk/python/README.md)）

如果服务挂在路径前缀下（`CEREBEL_BASE_PATH`，例如 `https://cerebel.tech/playground`），所有路径前面都要加这个前缀；接口返回的 `/media/...` 链接已经带好前缀，拼到域名后面即可。

## 典型流程

```text
注册 / 登录 ─► 读 /api/me，看 video_consent.required
            ─► 给用户看同意文本，同意后 POST /api/account/video-consent
            ─► POST /api/upload（multipart，字段名 file）           → session_id
            ─► 每 5 秒 GET /api/progress?session_id=…              → 直到最终状态
            ─► GET /api/sport_analysis?session_id=…                → findings[0] 就是主推的修正
            ─► 可选：POST /api/chat/stream 追问教练
```

用 SDK 一行：

```python
result = client.analyze("swing.mp4")           # Python
```

```ts
const result = await client.analyze(file);      // TypeScript
```

## 认证

- `POST /api/auth/register` 或 `/api/auth/login` 返回 `token`。之后每个请求带 `Authorization: Bearer <token>`。
- 令牌 30 天过期（`CEREBEL_TOKEN_TTL_DAYS`），过期后接口返回 401，重新登录即可。
- 注册必须传 `"accept_terms": true`，表示用户读过并同意 `/terms` 和 `/privacy`。不要替用户默认勾选。
- 匿名调用只能看到演示会话（`CEREBEL_DEMO_SESSION_ID`），看别人的会话一律 403。

## 视频处理同意

这是一项独立于注册条款的同意，美国的生物特征数据法规（例如伊利诺伊州 BIPA）要求单独取得。

1. `GET /api/me` → `video_consent`：`required`、`version`、`text`（完整文本）。
2. 把 `text` 原样展示给用户，用户勾选后：`POST /api/account/video-consent {"accept": true, "version": "<看到的版本>"}`。
3. 如果在这期间文本改了版本，返回 409，重新展示新文本。
4. 没同意就上传，返回 **428**，响应里带着 `video_consent`，可以直接拿来展示。

## 上传

`POST /api/upload`，`multipart/form-data`：

| 字段 | 必填 | 说明 |
|---|---|---|
| `file` | 是 | `.mp4 .mov .m4v .webm .avi .mkv .mpeg .mpg` |
| `trim_start`、`trim_end` | 否 | 秒。只保留这一段（建议前端让用户框选一次挥杆） |

质检规则：时长 1.5–30 秒、帧率 ≥ 24、短边 ≥ 320 像素，大小不超过 `CEREBEL_UPLOAD_MAX_BYTES`（默认 300 MB；Mac mini + Cloudflare 免费版的线上环境实际上限约 95 MB）。低于 60 fps 或短边低于 480 会通过，但会在 `warnings` 里提示。

| 返回 | 含义 | 是否占用每月次数 |
|---|---|---|
| 200 | 已保存，开始处理。`session_id`、`warnings` | 是（失败或没识别到挥杆的会退回） |
| 401 | 没登录 | — |
| 402 | 本月次数用完（`quota` 里有详情，免费用户 `paywall: true`） | 否 |
| 413 | 文件太大 | 否 |
| 422 | 质检不合格（`rejected: true`），视频已被删除 | 否 |
| 428 | 没同意视频处理 | 否 |
| 503 | GPU 离线（`unavailable: true`），什么都没保存 | 否 |

线上入口在 Vercel 代理后面，代理只能转发约 4.5 MB 的请求体，所以**上传要直接发到应用服务器自己的地址**（服务端配置 `CEREBEL_UPLOAD_ORIGIN`，网页会自动用它）。用 SDK 时，向运维要这个地址，传给 `upload_base_url`（Python）/ `uploadBaseUrl`（TypeScript）/ `--upload-base-url`（命令行）。不设的话，大于约 4.5 MB 的视频会被代理拒绝。

## 进度

`GET /api/progress?session_id=…`，建议每 5 秒一次。一般几分钟完成，超过 45 分钟服务端会判超时。

| `stage` | 显示建议 |
|---|---|
| `uploading`、`extracting` | 准备视频 |
| `queued` | 排队中；`queue_message` 可以直接显示（"1 swing ahead of you — about 3 min."） |
| `gemx` | 正在重建身体 |
| `ik` | 正在计算关节角度 |
| `ready` **且有 `sport`** | 完成，去取报告 |
| `ready` 但没有 `sport` | 还没完：运动识别马上会写入，可能变成 `no_swing`。继续轮询（最多等 90 秒） |
| `no_swing` | 没识别到挥杆，提示从侧面重拍，不计次 |
| `error` | 失败，`message` 可以直接展示，不计次 |

`GET /api/session?session_id=…` 在处理中也会返回 `processing: true` 和同样的进度字段，适合直接渲染等待页。

## 报告

| 接口 | 内容 |
|---|---|
| `GET /api/sport_analysis` | 主报告：阶段、测量曲线、关键点、`findings`（已按严重程度排序） |
| `GET /api/golf_phases` | 视频里有几次挥杆，每次的七个阶段和时间 |
| `GET /api/coach_briefs` | 常见问题的即时回答，不调用语言模型，免费 |
| `GET /api/compare?before=…&after=…` | 同一账号两次挥杆逐阶段对比，每个指标给出"变好 / 变差 / 没有明显变化" |
| `GET /api/session` | 会话概览：识别出的运动、视频地址、示例问题 |
| `GET /media/<id>/session.mp4?mt=…` | 视频文件。只用接口给出的链接，`mt` 是 6 小时有效的签名 |

**主推修正的选法**：`findings` 里第一条 `strength` 不为 true 的。SDK 的 `main_fix()` / `mainFix()` 就是这个逻辑。

## AI 教练

- `POST /api/chat {"question": "…", "video_id": "<session_id>"}` 一次性返回 `answer` 和工具调用记录 `trace`。
- `POST /api/chat/stream` 同样的请求，返回 Server-Sent Events，每帧 `data: <json>`。事件 `type`：

| type | 含义 |
|---|---|
| `status` | 进度文字（"Running tool …"） |
| `thinking` | 模型的中间想法（可以不展示） |
| `tool` | 一次工具调用和结果摘要 |
| `assistant` | 模型的中间回答 |
| `final` | 最终回答 |
| `error` | 出错，`message` 可展示 |
| `done` | **一定是最后一个**，`answer` 是完整回答 |

每天的提问上限：匿名 3 次、免费 15 次、Pro 100 次，超出返回 429。

## 错误

所有错误都是 JSON：

```json
{"error": true, "message": "That capture belongs to another account"}
```

- `message` 是写给最终用户看的英文，可以直接展示。
- 500 会带 `error_id`，和服务器日志里的记录对应，报 bug 时附上它。服务端不会把异常堆栈返回给客户端。
- SDK 把状态码映射成异常类：401 `AuthError`、402 `PaymentRequired`、403 `Forbidden`、404 `NotFound`、410 `Gone`、422 `UploadRejected`、428 `ConsentRequired`、429 `RateLimited`、503 `ServiceUnavailable`，分析失败是 `AnalysisFailed`。

## 跨域

浏览器跨域调用只放行 `CEREBEL_ALLOWED_ORIGINS` 里列出的来源，默认为空（只允许同源）。做独立前端时，让运维把你的域名加进去；手机 App 和服务端调用不受影响。

## 不对外的接口

`/api/ops`、`/api/system_prompt`、请求里的 `system_prompt` / `backend` 字段只在操作员端口（`CEREBEL_OPERATOR_PORT`，只绑 127.0.0.1）可用，并且需要 `X-Prompt-Password`。`/api/stripe/webhook` 只给 Stripe 调用，会校验签名。
