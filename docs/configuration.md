# 配置参考

所有配置都来自环境变量。线上写在代码旁边的 `.env.product` 里（模板 [`deploy/env.product.example`](../deploy/env.product.example)），启动脚本用 bash `source` 读入，所以带空格或 `< >` 的值要加引号。**`.env.product` 不要提交，也不要贴到任何聊天里**；`.gitignore` 已经忽略了 `.env` 和 `.env.*`。

## 网页服务

| 变量 | 默认 | 说明 |
|---|---|---|
| `MOTION_AGENT_HOST` | `0.0.0.0` | 线上设 `127.0.0.1`，再通过 HTTPS 隧道对外 |
| `MOTION_AGENT_PORT` | `8092` | |
| `CEREBEL_OPERATOR_PORT` | 空 | 设了就在 127.0.0.1 上另开操作员端口，公开端口只服务消费者界面。推荐 `8093` |
| `CEREBEL_DATA_DIR` | 仓库下 `data/` | 会话和产品数据 |
| `CEREBEL_PUBLIC_URL` | — | 用户访问的 HTTPS 地址，Stripe 回跳和邮件链接用 |
| `CEREBEL_BASE_PATH` | 空 | 挂在路径前缀下时设，例如 `/playground` |
| `CEREBEL_UPLOAD_ORIGIN` | 空 | 有路径前缀且在 Vercel 后面时，上传直接发到这个源，绕开代理的请求体上限 |
| `CEREBEL_ALLOWED_ORIGINS` | 空 | 允许跨域的来源，逗号分隔。空 = 只允许同源 |
| `CEREBEL_TOKEN_TTL_DAYS` | `30` | 登录令牌有效期 |
| `CEREBEL_MEDIA_URL_SECRET` | 空 | 媒体链接签名密钥；空则自动生成到 `data/product/media_url.key` |
| `CEREBEL_MEDIA_URL_TTL_S` | `21600` | 媒体链接有效期（秒） |
| `CEREBEL_UPLOAD_MAX_BYTES` | `536870912`（模板里是 300 MB） | 单个上传上限 |
| `CEREBEL_DEMO_SESSION_ID` | 空 | 唯一允许匿名访问的会话；数据保留任务不会删它 |
| `CEREBEL_LOG_LEVEL` | `INFO` | |

## 套餐和限额

| 变量 | 默认 | 说明 |
|---|---|---|
| `CEREBEL_FREE_MONTHLY` | `3` | 免费用户每月分析次数 |
| `CEREBEL_PRO_MONTHLY` | `60` | Pro 每月合理使用上限 |
| `CEREBEL_FOUNDING_LIMIT` | `200` | 创始会员名额 |
| `CEREBEL_ANON_DAILY_QUESTIONS` | `3` | 匿名访客每天提问数 |
| `CEREBEL_ANON_DAILY_TOTAL` | `300` | 所有匿名访客每天合计（防止伪造 IP 头刷量） |
| `CEREBEL_FREE_DAILY_QUESTIONS` | `15` | |
| `CEREBEL_PRO_DAILY_QUESTIONS` | `100` | |

## Stripe

| 变量 | 说明 |
|---|---|
| `STRIPE_SECRET_KEY` | 只放在服务器上 |
| `STRIPE_WEBHOOK_SECRET` | webhook 签名密钥 |
| `STRIPE_PRICE_ANNUAL` | 年付价格 ID；不设就不显示付费方案 |
| `STRIPE_PRICE_MONTHLY` | 月付价格 ID；空则隐藏月付 |
| `STRIPE_FOUNDING_COUPON` | 让首年变成 $69 的优惠券 |

## AI 教练和语言模型

客户端是通用的 OpenAI 兼容接口，后端名 `dashscope` 是历史名字。

| 变量 | 默认 | 说明 |
|---|---|---|
| `MOTION_AGENT_BACKEND` | `dashscope` | `dashscope`（任何 OpenAI 兼容服务）或 `mock`（本地开发、测试） |
| `MOTION_AGENT_BASE_URL` | 阿里云 DashScope | **线上必须换成美国托管的服务**，例如 `https://api.together.xyz/v1` |
| `MOTION_AGENT_MODEL` | `qwen3.7-plus` | 服务商的模型 ID |
| `MOTION_AGENT_API_KEY` | — | 优先使用。没设时依次读 `~/Qwen_KEY.txt`（或 `QWEN_KEY_FILE`）、`DASHSCOPE_API_KEY`、`QWEN_API_KEY` |
| `MOTION_AGENT_ENABLE_THINKING` | `0` | 1 = 打开推理 token，更贵更慢 |
| `MOTION_AGENT_MAX_TOOL_CALLS` | 不限 | 单个问题的工具调用上限，线上建议 12 |
| `CEREBEL_LLM_ATTACH_VIDEO` | `0` | 1 = 允许把视频帧发给模型。默认关闭，隐私政策承诺不发 |
| `MOTION_AGENT_KB_CORPUS_DIR` | `motion_agent/knowledge/corpus` | 知识库文本 |
| `MOTION_AGENT_KB_INDEX_DIR` | `motion_agent/.cache/kb_index` | 向量索引缓存；换嵌入模型时换个目录 |
| `MOTION_AGENT_KB_EMBED_MODEL`、`MOTION_AGENT_KB_EMBED_BASE_URL`、`MOTION_AGENT_KB_EMBED_API_KEY` | — | 嵌入模型；地址和密钥空则用教练模型的 |
| `MOTION_AGENT_KB_TOP_K`、`MOTION_AGENT_KB_CHUNK_CHARS`、`MOTION_AGENT_KB_CHUNK_OVERLAP` | 见 `knowledge/__init__.py` | 检索条数和切块大小 |
| `MOTION_AGENT_PROMPT_PASSWORD` | 空 | 操作员功能的密码。不设则这些功能全部锁住 |

## GPU 流水线

| 变量 | 默认 | 说明 |
|---|---|---|
| `CEREBEL_GPU_BACKEND` | `local` | `local` = 本机 warm worker；`modal` = Modal 函数 |
| `CEREBEL_QUEUE_WORKERS` | 1（local）/ 10（modal） | 并行任务数 |
| `CEREBEL_AVG_JOB_SECONDS` | `150` | 用于估算排队时间 |
| `MODAL_TOKEN_ID`、`MODAL_TOKEN_SECRET` | — | Modal 凭据 |
| `CEREBEL_GEMX_ROOT` | `/root/GEM-X` | GEM-X 代码目录 |
| `CEREBEL_GEMX_PYTHON` | `$GEMX_ROOT/.venv/bin/python` | 能 import torch 的 Python |
| `CEREBEL_IK_PYTHON` | 仓库下 `.venv-ik/bin/python` | 能 import opensim 的 Python |
| `CEREBEL_GEMX_OUTPUT_ROOT` | `$GEMX_ROOT/outputs/cerebel` | GEM-X 输出 |
| `CEREBEL_GEMX_WORKER_HOST` / `_PORT` | `127.0.0.1` / `61017` | warm worker 地址 |
| `CEREBEL_GEMX_WORKER_TIMEOUT_S` | `3600` | 单个任务上限 |
| `CEREBEL_GEMX_WORKER_SUBMIT_WAIT_S` | `300` | worker 重启时任务最多等多久 |
| `CEREBEL_IK_ENABLED` | `1` | 关掉就只跑 GEM-X |
| `CEREBEL_SUBJECT_HEIGHT_CM` / `_MASS_KG` | `185` / `93` | IK 用的身高体重（目前所有人一样） |
| `CEREBEL_MOBL_ARMS` | — | MoBL-ARMS 手臂模型，仅限非商业用途，线上保持 `0` |
| `CEREBEL_FAKE_EGOCENTRIC` | `0` | 演示用：隐藏原视频，改显示重建的人体网格 |

## 数据保留

| 变量 | 默认 | 说明 |
|---|---|---|
| `CEREBEL_RAW_VIDEO_RETENTION_DAYS` | `30` | 处理完多少天后删原视频和帧 |
| `CEREBEL_INACTIVE_DELETE_DAYS` | `730` | 多久不活跃就删除账号和全部数据 |

隐私政策页会显示这两个数字，改了它们等于改了对用户的承诺。

## 邮件

| 变量 | 说明 |
|---|---|
| `CEREBEL_SMTP_HOST`、`CEREBEL_SMTP_PORT`（465）、`CEREBEL_SMTP_USER`、`CEREBEL_SMTP_PASSWORD` | 不设就不发"分析完成"邮件，界面也不会承诺发邮件 |
| `CEREBEL_MAIL_FROM` | 例如 `'CeRebel <garylin@cerebel.tech>'`，要加引号 |
| `CEREBEL_LEGAL_ENTITY`、`CEREBEL_SUPPORT_EMAIL` | 条款和隐私页里的公司名和联系邮箱 |

## SDK 和命令行

| 变量 | 说明 |
|---|---|
| `CEREBEL_BASE_URL` | `cerebel` 命令行的 API 地址，默认 `https://cerebel.tech/playground` |
| `CEREBEL_UPLOAD_BASE_URL` | 命令行上传用的地址（对应服务端的 `CEREBEL_UPLOAD_ORIGIN`） |
| `CEREBEL_TOKEN` | 命令行用的令牌（否则读 `cerebel login` 保存的） |
| `CEREBEL_CREDENTIALS` | 令牌保存位置，默认 `~/.config/cerebel/credentials.json`（权限 600） |
| `CEREBEL_TEST_BASE_URL` | 设了才运行 SDK 的真服务器测试 |
| `CEREBEL_TEST_UPLOAD` | 真服务器测试里要上传的视频（会占用一次分析，需要 GPU） |
