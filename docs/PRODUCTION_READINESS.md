# Production Readiness Sprint：第一阶段（P0）记录

2026-10-26，分支 `prod-readiness-p0`（从 `ui-polish` = `origin/main` = `ca863d1` 切出）。**没有部署，没有改线上环境，没有迁移用户数据。**

## 结论

**还不能称为 Production Ready。** 代码层面的 P0 修复和测试已完成，但 P0-1（实际部署与隐私承诺不一致）必须靠迁移服务器和选定语言模型供应商才能解决，这需要你决定；而且所有修复（包括 10-26 已合并的两个）都还没部署到线上。

## 线上实际状态（只读核实，2026-10-26 17:40 美东）

| 检查 | 结果 | 依据 |
|---|---|---|
| 线上主机 | 旧 onethingai GPU 机 | `GET cerebel.tech/playground/api/health` 返回 `data_dir: /root/CerebelServer/data` |
| GPU 位置 | 同一台机器的本机 worker，不是 Modal | `gpu.state = ready`（Modal 后端会是 `remote`） |
| 语言模型 | 无法从外部确定 | `backend: dashscope` 只是历史名称；代码默认端点是阿里云大陆 DashScope |
| 教练视频帧修复（`d270764`） | **未部署** | 无法从外部直接验证；运行手册记录 10-26 服务器上的文件仍是改动前版本 |
| 付费墙 "Unlimited"（`1acce6c`） | **未部署** | 线上 `/static/billing.js` 仍写 "Unlimited questions…"；`/api/billing` 没有 `pro_daily_questions` |
| 隐私政策 / 同意书 | 写的是 DigitalOcean、Modal、Together AI，全部在美国 | 线上 `/privacy` 第 6、7 节 |
| `playground.cerebel.tech` | 现在能访问（PRODUCT_STATUS.md 里说不可用，已过时） | 同一个 health 响应 |

## P0 问题与修复结果

### P0-1 部署、GPU、处理方与隐私政策不一致：已检测，未解决（需要你决定）

不一致：线上数据和 GPU 都在旧机（交接文档记录出口 IP 在上海），但政策写的是 DigitalOcean + Modal（美国）。如果服务器没有设置 `MOTION_AGENT_BASE_URL`，教练的问题会发到阿里云大陆。

代码修复：

- `motion_agent/app/privacy_guard.py`（新增）：逐项比对服务器配置（数据目录、`CEREBEL_HOST_PROVIDER`、GPU 后端、模型和 embedding 端点、SMTP）与 `consent.PROCESSORS`，列出所有不一致。
  - 启动时写 WARNING 日志；结果显示在运维端口的 `/api/ops`。
  - 命令行：`python3 -m motion_agent.app.privacy_guard`，只读，有不一致时退出码为 1。
  - `CEREBEL_PRIVACY_GATE=enforce`：有不一致就暂停新上传（503，不计次）；语言模型不一致时同时暂停教练。默认是 `warn`，所以部署这份代码本身不会让上传停掉。
- 去掉了阿里云大陆的默认端点（`qwen_client.py`、`knowledge/__init__.py`）。现在 `MOTION_AGENT_BASE_URL` 必须显式设置，不设置时教练直接报错，`/api/health` 返回 503（`credential: endpoint_missing`）。
- `consent.PROCESSORS` 改为唯一数据源：同意弹窗、同意书正文、隐私政策第 6 节表格都从它生成。
  - 渲染出的隐私页与修改前逐字节相同，同意书正文哈希不变，所以不需要用户重新同意。
  - 新增测试锁定同意书正文：以后改了正文却没有提升 `VIDEO_CONSENT_VERSION`，测试会失败。
- 部署模板：`env.product.example` 增加 `CEREBEL_HOST_PROVIDER` 和 `CEREBEL_PRIVACY_GATE`。DigitalOcean 脚本写入 `digitalocean`；Mac mini 脚本写入 `mac-mini-boston`，在政策列出这台机器之前会被标出。

用旧机的实际配置跑检查，得到 3 到 4 条不一致：`LEGACY_HOST`、`HOST_UNDECLARED`、`GPU_LOCAL`，以及 `LLM_UNSET` 或 `LLM_NOT_DISCLOSED`（取决于服务器的 `.env.product`）。

### P0-2 教练向外部语言模型发送视频帧：已修复（`d270764`），本轮加了两层纵深防御

- 已有修复：视频不共享时，不向模型提供 `get_rgb_frame` / `get_rgb_clip`，模型即使调用也会被拒绝，提示词里也不再引导它看视频。
- 新增第一层：即使 `CEREBEL_LLM_ATTACH_VIDEO=1`，也只有运维端口能附带视频，而且只限没有账户归属的内部测试挥杆和 demo。客户端口、单端口部署、任何属于账户的挥杆，一律不附带。
- 新增第二层：`MotionAgent` 和 `answer_question` 的 `attach_video` 默认值改为 `False`，以后新增的调用方忘了传参也不会泄露视频。
- 新增 7 个测试。

### P0-3 Pro "Unlimited Questions" 与真实限额不一致：已修复（`1acce6c`），未部署

- 仓库里已经找不到 "Unlimited"。付费墙显示 "Up to N questions a day"，N 来自 `CEREBEL_PRO_DAILY_QUESTIONS`。
- 本地冒烟测试中，`/api/billing` 返回 `pro_daily_questions: 100`。

### P0-4 账户、视频访问、删除、订阅安全：发现 6 个问题，全部修复

| # | 问题 | 后果 | 修复 |
|---|---|---|---|
| 1 | 处理中删除挥杆，GPU 流水线结束后会重新写回 3D 身体数据、发"分析完成"邮件；retention 不清理有主但已删除的目录 | 用户删除的生物特征数据会一直留着，违反"立即删除"的承诺 | 流水线结束后检查是否已删除，已删除就再清理一次，不写库、不发邮件；retention 新增 `deleted_capture_leftovers` 规则 |
| 2 | 订阅处于 `past_due` / `unpaid` 时可以删除账户 | Stripe 继续重试扣款，而用户已经没有 "Manage subscription" 按钮 | 新增 `billing.still_billing()`，删除账户和 retention 都改用它 |
| 3 | Stripe webhook 乱序时，迟到的 "active" 更新会让已取消的订阅重新生效 | 用户可能免费得到 Pro，状态也会回退 | 按事件 `created` 时间忽略过期事件；同一订阅一旦变成 canceled 不再恢复 |
| 4 | 已删除账户的 webhook 会重新生成计费记录 | 已删除账户的数据残留 | 记录已删除账户（只存随机 id），之后忽略它们的 webhook；创始会员计数照常增加 |
| 5 | 订阅 `past_due` 时还能再走一次结账 | 同时存在两个订阅，重复扣款 | 返回 409，引导去 Manage subscription |
| 6 | 登录没有限流；PBKDF2 在全局锁里计算；登录、注册、删除、教练接口不限制请求体大小 | 可以暴力破解密码；可以让所有人登录排队；大请求体会占住线程（修改前测试中一个超大请求要等 10 秒超时） | 每个邮箱 15 分钟内最多失败 10 次，每个 IP 最多 50 次；每个 IP 每小时最多注册 20 个账户；哈希移到锁外；请求体上限 4–256 KB，超出返回 413 |

视频访问本轮复查没有发现问题：只有本人和指定的 demo 可以访问；签名链接 6 小时过期，删除后立即失效；路径穿越有防护。

## 测试

命令：`python3.10 -m unittest discover -s motion_agent/tests -t .`

| | 修改前 | 修改后 |
|---|---|---|
| 测试总数 | 263 | 310（新增 47 个） |
| 失败 / 报错 | 15 | 15（同样的 15 个：14 个需要服务器上的样本数据，1 个是原有的 `test_golf_kb_scope_defaults`） |

- 新增测试：`test_account_security.py`（21）、`test_privacy_guard.py`（19）、`test_coach_video_privacy.py`（+7）。
- 变异检验：把业务代码临时恢复为修复前的版本，新增测试相应失败（教练 4 个，账户安全 33 处）。
- 本机真实进程冒烟测试通过：注册、同意书处理方、付费墙额度、第 11 次错误登录返回 429、超大请求体返回 413、隐私页表格、删除账户。
- **没有覆盖**：服务器样本数据上的那 14 个测试；真实 Stripe（webhook 用签名过的本地事件测试）；真实 GPU 流水线（用替身模拟）。

## 部署前必须做的事（都需要你授权）

1. 先确定教练的语言模型端点。如果服务器目前依赖代码里的默认值，部署这份代码后教练会停止工作，`/api/health` 返回 503，巡检会报警。这是有意的"失败即关闭"。
2. 在服务器上只读运行：
   ```bash
   python3 -m motion_agent.app.privacy_guard
   python3 -m motion_agent.app.retention --dry-run
   ```
   第二条看 `deleted_capture_leftovers`，确认有多少条已删除挥杆的数据残留。
3. 上传文件：本分支改动的所有文件，加上新文件 `rate_limit.py`、`privacy_guard.py`，以及 `d270764` 和 `1acce6c` 的 5 个文件。确认没有处理中的上传后再重启 `cerebel-product`。

## 需要你决定

1. **主机迁移**：DigitalOcean（与现有政策一致）还是 Mac mini（需要改 `PROCESSORS`、提升同意版本号、让所有用户重新同意）。迁移完成前，建议在旧机设置 `CEREBEL_PRIVACY_GATE=enforce`（暂停新上传），或只开放邀请制内测。
2. **语言模型供应商**：政策写的是 Together AI。

## 留到下一阶段的问题（P1）

- `tokens.json` 里的登录 token 是明文，应改为只存哈希，并兼容现有 token。
- 没有邮箱验证和找回密码。
- 按邮箱锁定可能被人用来把别人锁在外面 15 分钟。按 IP 计数信任 `CF-Connecting-IP`，所以源站必须只能经 Cloudflare / Vercel 访问。
- 条款写着"关节负荷只以相对自己以前视频的变化显示"，但教练可以说出绝对负荷数字。这一条放进第二阶段"区分测量、估计和推断"一起处理。
- Python 3.13 删除了 `cgi` 模块，升级 Python 前要先替换上传解析。
- iOS gRPC 61016 端口是明文、无认证，要确认没有暴露在公网。
