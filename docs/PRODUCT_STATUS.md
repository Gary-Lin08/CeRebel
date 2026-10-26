# CeRebel 产品现状全记录

截至 2026-10-26（美东）。基于 `ui-polish` 分支 `1acce6c`（与 `origin/main` 相同）的代码、提交记录和仓库里的运行手册整理。我没有登录线上服务器，所以「线上现在跑在哪台机器上」这类结论来自运行手册的记录，标了「待核实」的地方请以服务器实际状态为准。

目录

1. 一页摘要
2. 产品定义
3. 开发进度（时间线、模块完成度、进行中的工作、测试、部署）
4. 风险与发现的问题（按严重程度）
5. 完整功能清单
6. 用户使用的完整步骤
7. 附录：接口、阈值、埋点、配置、文件索引

---

## 1. 一页摘要

**产品**：CeRebel 是一个网页版高尔夫挥杆分析工具。用户用手机拍一段挥杆视频上传，系统把视频重建成 3D 人体（GEM-X），再驱动一个带肌肉的 OpenSim 肌骨模型算出关节角度和估算负荷，最后给出「最该先改的一件事」，并配一个练习动作。AI 教练可以回答关于这次挥杆的问题，它引用的每个数字都来自确定性的计算工具，不由语言模型生成。

**阶段**：消费者公开测试版（beta），只支持高尔夫，只有网页版。对外地址是 `https://cerebel.tech/playground`（经 Vercel 主站代理）和 `https://playground.cerebel.tech`。

**已经能用的完整闭环**：

- 落地页 → 注册（勾选同意）→ 首次上传前的视频处理同意 → 浏览器端检查并裁剪挥杆 → 上传 → 服务器质检 → 排队 → 3D 重建 → 测量 → 识别是否为高尔夫挥杆 → 报告（主修正、骨架叠加、练习、教练问答）
- 作品库、删除单个挥杆、删除账户
- 两次挥杆的进步对比
- 每月免费 3 次分析，Stripe 订阅升级（创始会员 / 年付 / 月付）
- 数据保留和隐私清理（原始视频 30 天删除，24 个月不活跃删除账户）

**最需要先处理的三件事**（详见第 4 节）：

1. **对外的隐私承诺与实际不一致**。同意书和隐私政策写的是「只在美国处理，由 DigitalOcean 和 Modal 处理」，但按 10-26 的运行记录，线上仍跑在旧的 onethingai GPU 服务器上（交接文档记其出口 IP 在上海），处理方名单也和 Mac mini 方案对不上。
2. **AI 教练会把视频帧发给语言模型**。`CEREBEL_LLM_ATTACH_VIDEO=0` 只关掉了开头附带的视频；教练调用 `get_rgb_frame` / `get_rgb_clip` 时，取到的帧仍会作为图片发给模型。同意书写的是教练「不接收我的视频」。**代码已修复（`d270764`），尚未部署到线上。**
3. **付费墙写着 "Unlimited questions to the coach"**，但 Pro 实际每天上限 100 个问题。**代码已修复（`1acce6c`），尚未部署到线上。**

---

## 2. 产品定义

| 项目 | 内容 |
|---|---|
| 公司 | Cerebel, Inc.（Delaware C-corp，团队在美国） |
| 一句话 | 一部手机、一次挥杆，得到 3D 身体重建、关节角度和一个最该改的点 |
| 核心承诺 | 报告和教练里的每个数字都来自测量，不来自语言模型 |
| 目标用户 | 海外（美国）业余高尔夫球手；之后通过教练码引入教练和学员 |
| 当前运动 | 高尔夫（live）。棒球有代码但未开放；网球、篮球、足球、跑步、举重、游泳、排球、拳击、瑜伽、单板滑雪显示为 coming soon |
| 平台 | 网页（手机和电脑浏览器）。iOS 采集端有 gRPC 协议但产品上往后放 |
| 定价 | 免费每月 3 次；Pro 年付 $99.99；创始会员首年 $69（限 200 名）；月付 $14.99（可选，未配置价格时隐藏）。Pro 每月 60 次（合理使用上限） |
| 域名 | 官网 cerebel.tech（Vercel）；产品 playground.cerebel.tech，也挂在 cerebel.tech/playground |
| 客服邮箱 | garylin@cerebel.tech（SpaceMail） |
| 设计方向 | 见 `DESIGN.md`：「高尔夫码数本」而不是科幻仪表盘。当前上线的是 10-24 重做的深色「黄昏球场」主题，带 WebGL 流体背景和滚动驱动的挥杆演示 |

### 技术链路（一段视频经过的路）

```
手机视频
  │  浏览器：MediaPipe 姿态模型（自托管）找人、找挥杆、建议 3–4 秒窗口
  ▼
POST /api/upload  ──  额度检查 → 同意检查 → ffprobe 质检 → ffmpeg 裁剪到挥杆窗口
  ▼
GPU 队列（upload_server.ensure_queue）
  ▼
GEM-X（NVIDIA 单目 3D 人体重建，输出 SOMA/SMPL 参数）
  │  后端二选一：本机常驻 worker（local）或 Modal 美国 GPU（modal）
  ▼
smpl_to_pose3d → SOMA 颈部关键点 → OpenSim IK（Rajagopal 2023 + 颈部模型）
  │  输出 ik/opensim-motion.json、pose3d.json、estimated-dynamics.json、session.mp4
  ▼
hpe_results.pt 脱敏（删 net_outputs，头部形状系数清零）
  ▼
运动识别（Golf Sport Pack：挥杆置信度 ≥ 0.45）→ ready / no_swing
  ▼
报告：挥杆分段 → 6 条测量曲线 → 4 条规则找问题 → 按严重度排序 → 主修正
  ▼
AI 教练：OpenAI 兼容接口的语言模型 + 18 个确定性工具 + 高尔夫知识库检索
```

---

## 3. 开发进度

### 3.1 时间线

| 日期 | 里程碑 |
|---|---|
| 10-19 | 初始提交：iOS gRPC 采集服务、GEM-X 流水线、OpenSim IK、Kinetic Lens 演示站、Motion Agent 教练原型；MoBL 手臂肌肉渲染 |
| 10-23 | 多运动 Sport Pack 知识库 v2、Playground 产品外壳、检测器与问题规则对接 |
| 10-24 | **消费者测试版成型的一天**：两个安全补丁；月度额度、Stripe 订阅、注册同意、条款隐私页、埋点；修复网页上传进不了 GPU 队列；上传质检；只公开指定 demo；语言模型改成供应商中立、教练每日提问上限、MoBL 默认关闭（非商用许可）；排队位置和预计时间、服务器端裁剪、分析完成邮件、浏览器端挥杆检测、`?session=` 深链接；Modal GPU 后端（美国）；DigitalOcean 部署脚本；视频处理同意和数据保留；补上颈部 IK 文件和脸型参数的隐私缺口；私有视频的签名链接、iOS Range 播放；`/static` 路径穿越漏洞修复；新落地页和真人挥杆演示；Mac mini 部署方案；`/playground` 路径前缀；匿名提问按转发 IP 限流；整体视觉重做 |
| 10-26 | 演示动画打磨；页面上隐藏内部工具名；开发者界面移到单独的本机端口；GPU worker 宕机时立即拒绝上传并标注失败环节；「Today's drill」练习闭环；等待页步骤条；只在能发邮件时才承诺邮件；旧 GPU 机上线每日 retention 并执行一次性隐私清理；登录后的应用布局整理；上传前检查修复（iPhone Safari 卡死、误报「全身不在画面」）；姿态模型自托管；跨进程文件锁；retention 补三条规则；匿名访客 IP 改存 HMAC；**进步对比**（API + 界面）；噪声测量脚本；**骨架叠加在视频上、问题部位标成琥珀色**；上传后直接显示等待页再变成报告；GitHub Actions 每 30 分钟巡检 |

10-26 当天旧 GPU 机关机数小时没人发现（见 `.github/workflows/uptime.yml` 注释），之后加了巡检。

### 3.2 模块完成度

状态说明：✅ 已完成并在主线上；🟡 能用但有已知缺口；🚧 进行中（分支未合并）；⬜ 未开始；🧊 遗留或内部工具。

| 模块 | 状态 | 说明 |
|---|---|---|
| 落地页（未登录） | ✅ | 标题、滚动挥杆演示、三个标签页示例、拍摄指引、隐私承诺、价格（从 `/api/billing` 读取）、页脚条款 |
| 注册 / 登录 / 登出 | ✅ | 邮箱 + 密码（≥ 8 位，PBKDF2-SHA256 12 万轮）；注册必须勾选同意；可选教练码；token 30 天过期 |
| 视频处理同意 | ✅ | 首次上传前弹窗，单独记录版本（`2026-10-24-us`）；文本或处理方变了会重新征求 |
| 浏览器端上传前检查 | ✅ | MediaPipe 姿态模型在用户设备上运行，自托管（无第三方请求）；找不到人则拦截；建议裁剪窗口 |
| 服务器端质检 | ✅ | 时长 1.5–30 秒、帧率 ≥ 24、短边 ≥ 320；不合格直接删除视频，不计次 |
| GPU 重建（GEM-X） | 🟡 | 本机 worker 和 Modal 两种后端都已实现。本机 worker 宕机时会立即拒绝上传；10-26 出过数小时停机 |
| OpenSim IK 与估算负荷 | 🟡 | 能跑通。所有用户都按 **185 cm / 93 kg** 缩放模型，用户不能填自己的身高体重 |
| 运动识别 | ✅ | 只开放高尔夫；识别不到挥杆标记为 `no_swing`，不计次 |
| 报告页：主修正卡 | ✅ | 标题、白话解释、测量值、要点、播放该时刻、Why、Today's drill、对比滑块、语音提问、即时回答按钮 |
| 骨架叠加 | ✅ | 10-26 新增。关节和骨骼按帧画在用户自己的视频上，问题部位标成琥珀色并写上测量值；开关按浏览器记住 |
| 测量面板 | ✅ | 6 条曲线、相位刻度、播放头、3D 骨骼 + 肌肉特写。「做得好的地方」列表只在开发者界面显示 |
| 练习闭环 | ✅ | 标记已练习（计数保存在挥杆记录上）、下一次挥杆的检查目标、「Film again to check」 |
| AI 教练 | 🟡 | 流式回答、工具调用、知识库检索、每日限额。视频帧外泄已在代码里修复，线上仍是旧代码（见 4.2） |
| 作品库 | ✅ | 列表、状态、排队信息、删除；处理中每 4 秒刷新 |
| 进步对比 | 🟡 | 10-26 新增。免费用户只能对比相邻两次，Pro 可任选两次。噪声阈值是暂定值，还没用真实数据校准 |
| 计费 | ✅ | Stripe 托管结账和订阅管理页；webhook 验签、去重；创始会员名额计数 |
| 分析完成邮件 | 🟡 | 代码完成；只有配置了 SMTP 才会发，配置与否待核实 |
| 数据保留 / 隐私清理 | ✅ | 每日 retention 七条规则；一次性 backfill 已在旧机执行（10-26） |
| 埋点 | ✅ | 服务端和前端事件写入 `data/product/events.jsonl` |
| 运维 | 🟡 | `/api/health`、`/api/ops`、每日备份 14 天、30 分钟巡检。没有告警以外的监控面板 |
| 开发者界面 | ✅ | `CEREBEL_OPERATOR_PORT` 设置后只在本机端口提供；Prompt 编辑需要密码 |
| Sport Pack 引擎重构 | 🚧 | 分支 `claude/sport-packs-engine`，2 个提交未合并（见 3.3） |
| 棒球 | ⬜ | 有规则和知识库，检测器未开放 |
| 其他运动 | ⬜ | 只有知识库和目录卡片 |
| 教练后台 | ⬜ | 只做了教练码写入 Stripe 元数据 |
| 7 天试用 | ⬜ | 决定上线时先不开 |
| iOS App | 🧊 | 采集协议在 `capture.proto`（gRPC 61016，明文、无认证），App 代码不在本仓库 |
| Kinetic Lens（8091） | 🧊 | 内部 React 演示站：视频 + OpenSim 模型 + 关节曲线三视图同步 |
| `web_api.py` + `webapp/`、`upload_ui/` | 🧊 | 第一版网页后端和上传页，已不使用；retention 会清理它留下的日志 |

### 3.3 进行中的工作

| 分支 | 内容 | 状态 |
|---|---|---|
| `claude/sport-packs-engine` | 每个运动变成 `motion_agent/sports/packs/<id>/` 下的一个文件夹（`pack.json` + 可选 `hooks.py`），引擎、教练、提示词、工具定义、知识库范围都从包里读，平台不再写死高尔夫。另有一个重构前的行为快照（70 个用例），重构后保持一致 | 2 个提交，未合并到 `ui-polish` |
| `claude/sweet-sinoussi-f47a6c` | 「自己的挥杆带登录头打开，别人的保持私有」测试 | 1 个提交，和主线上的 `33be532` 改动完全相同，分支可以清理 |
| `origin/live-hotfix` | 10-24 的签名媒体链接、落地页和配色提交的另一份拷贝 | 10 个提交在主线上都有同名提交（9 个改动完全相同），分支可以清理 |

本地 `main` 停在 `c8d943e`，比 `origin/main` 落后 59 个提交；`HANDOFF.md` 里「本地 main 多 3 个提交未推送」的说法已经过时。

### 3.4 测试现状

命令：`python3.10 -m unittest discover -s motion_agent/tests -t .`

本机 Python 3.10 下的结果（2026-10-26）：

| 项目 | 数量 |
|---|---|
| 测试总数 | 263 |
| 通过 | 248 |
| 跳过 | 7 |
| 失败 / 报错 | 15 |

15 个失败全部集中在 `test_agent`、`test_angle_unwrap`、`test_dynamics_tools`、`test_golf_phases`、`test_motion_tools`：

- 14 个需要服务器上的样本数据（`/root/CerebelServer/data/sessions/C991F337-…`），本地没有。
- 1 个是交接时就存在的失败：`test_golf_kb_scope_defaults`。

计费、账户、上传、质检、隐私、retention、文件锁、进步对比、骨架叠加、页面鉴权等产品侧测试全部通过。

注意：用 Python 3.13 跑会多出 34 个报错，因为 `cgi` 模块在 3.13 被删除了，而 `product_api.parse_upload` 和 `upload_server` 都用它解析上传。服务器固定用 Python 3.10，所以目前没问题；以后升级 Python 前要先替换 `cgi.FieldStorage`。

### 3.5 部署现状

仓库里有三套部署方案，外加 Modal 上的 GPU：

| 方案 | 文件 | 组成 | 状态 |
|---|---|---|---|
| 旧 GPU 服务器（onethingai） | `deploy/LAUNCH.md`、`deploy/supervisor/` | `/root/CerebelServer`，supervisord 托管 `cerebel-product`（8092）、`cerebel-gemx-worker`、`cerebel-retention`、`cerebel-cloudflared` | **已确认是当前线上主机**：10-26 `cerebel.tech/playground/api/health` 返回数据目录 `/root/CerebelServer/data`、GPU 状态 `ready`（本机 worker），四个 supervisor 程序都在运行。交接文档记其出口 IP 在上海 |
| 波士顿 Mac mini | `deploy/mac/DEPLOY_MAC.md` | 网页和 CPU 后处理在 Mac mini；GEM-X 在 Modal 美国机房；教练用 Together AI；Cloudflare Tunnel；launchd 托管网站、每日 retention（03:41）、每日备份（04:17，保留 14 天）；上传上限 95 MB | 脚本就绪，未见执行记录 |
| DigitalOcean 纽约 | `deploy/us/DEPLOY_US.md` | 4 核 8 GB Droplet，Caddy 自动 HTTPS，systemd 托管，防火墙只开 22/80/443，retention 07:41 UTC，备份 08:17 UTC | 脚本就绪，未见执行记录 |
| Modal GPU | `deploy/modal_gemx.py` | GEM-X 镜像，L40S，限定美国机房（基础价 ×1.15，约 $2.24/小时），按秒计费，最多 10 个容器 | Mac mini 手册写「已部署（`cerebel-gemx`）」 |

其他基础设施：

- `https://playground.cerebel.tech` 在 10-26 没有响应（连接失败），目前只有 `cerebel.tech/playground` 能打开。

- Stripe 正式模式：价格、创始会员优惠券、4 个事件的 webhook 都已建好（交接文档记录）。
- Cloudflare：隧道已建；名称服务器从 Spaceship 切到 Cloudflare 的工作在交接时还没完成，切完要重新开启 DNSSEC（待核实）。
- GitHub Actions：`deploy.yml` 只能手动运行；`modal-deploy.yml` 部署 Modal；`uptime.yml` 每 30 分钟检查页面和 GPU 状态，失败时 GitHub 发邮件。

### 3.6 接下来的待办（按建议顺序）

1. 让对外承诺与实际一致：决定线上主机（Mac mini 或 DigitalOcean）和语言模型供应商，迁移完成后再按实际情况更新 `consent.py` 的处理方名单、隐私政策，并提升同意版本号（见 4.1）。
2. ~~修复教练把视频帧发给语言模型的问题（见 4.2）~~：代码已合并（`d270764`），待部署。
3. ~~改掉付费墙的 "Unlimited questions" 文案（见 4.3）~~：代码已合并（`1acce6c`），待部署。
   部署：把 `motion_agent/llm/agent.py`、`prompts.py`、`schemas.py`、`motion_agent/app/billing.py`、`motion_agent/app/ui/billing.js` 传到旧 GPU 机，确认没有处理中的上传后重启 `cerebel-product`。10-26 已核对服务器上这 5 个文件与改动前版本逐字节相同。
4. 完成名称服务器切换并重新开启 DNSSEC；用新邮箱走一遍 `deploy/mac/DEPLOY_MAC.md` 第 7 步的自测清单。
5. 用同一人连续拍的挥杆跑 `motion_agent/scripts/compare_noise_floor.py`，校准进步对比的噪声阈值。
6. 合并或放弃 `claude/sport-packs-engine`，再开始做第二个运动。
7. 请律师审阅条款和隐私政策（含「首次付款 14 天内可退款」承诺）。
8. 之后：教练后台、7 天试用实验、滑雪/单板小范围内测、iOS。

---

## 4. 风险与发现的问题

按严重程度排序。前 3 条是这次通读代码时发现、之前的文档里没有记录的。

### 4.1 隐私承诺与实际部署不一致（严重）

- `motion_agent/app/consent.py` 的同意文本写的是「Processing and storage happen only in the United States, by DigitalOcean, Modal」，处理方名单列的是 DigitalOcean、Modal、Together AI、Stripe、Cloudflare、SpaceMail。
- 但按运行记录，10-26 线上服务和 GPU 重建都在旧 onethingai 服务器上；交接文档记录其出口 IP 在上海，并指出美国用户的挥杆视频属于生物特征数据（美国司法部数据安全规定、伊利诺伊州 BIPA）。
- 即使迁到 Mac mini，名单里写的也是 DigitalOcean 而不是自有的 Mac mini。
- 语言模型的实际供应商取决于服务器 `.env.product`，代码默认值仍是阿里云 DashScope 大陆接口。

建议：迁移完成前只开放邀请制内测，或暂停新用户上传；迁移后按实际主机和供应商改同意文本并提升 `VIDEO_CONSENT_VERSION`，让所有人重新同意。

### 4.2 AI 教练会把视频帧发给语言模型（严重）

- `CEREBEL_LLM_ATTACH_VIDEO=0` 只控制提问时是否预先附带视频帧（`agent._build_user_content`）。
- 教练可用的 18 个工具里包括 `get_rgb_frame` 和 `get_rgb_clip`（`llm/schemas.py` 的 `openai_tools()` 总是返回全部工具）。教练调用它们时，`agent.py` 第 825–870 行会把取到的图片作为 `image_url` 追加到发给模型的消息里。
- 高尔夫系统提示词还明确鼓励「visual appearance… use the RGB video or RGB frame/clip tools」。
- 同意文本写的是「The AI coach … receives my questions and the computed numbers, not my video」。

状态：已修复，合并为 `d270764`（视频附带关闭时不向模型提供这两个工具；模型即使调用也直接拒绝、不读视频；提示词里的看视频指引只在附带视频时加入；新增 9 个测试）。**线上尚未部署。**

### 4.3 付费墙文案与实际限额不一致（中）

`motion_agent/app/ui/billing.js` 第 140 行写 "Unlimited questions to the coach about your swings"，但 `CEREBEL_PRO_DAILY_QUESTIONS` 默认 100，到达后返回 "You've reached today's question limit"。状态：已修复，提交 `1acce6c`。`/api/billing` 现在返回 `pro_daily_questions`，付费墙显示 "Up to 100 questions a day to the coach about your swings"（数字随配置变化）。**线上尚未部署。**

### 4.4 所有用户都按同一个身高体重建模（中）

`ik_pipeline.py` 用 `CEREBEL_SUBJECT_HEIGHT_CM=185`、`CEREBEL_SUBJECT_MASS_KG=93` 缩放 OpenSim 模型，用户无法填写自己的数据。关节角度受影响较小，估算的力和力矩直接和体重相关。目前报告的主修正只用角度和位移，但教练可以调用动力学工具说出负荷数字。

### 4.5 进步对比的判断阈值未校准（中）

`swing_compare.py` 的 `NOISE_FLOOR_SOURCE = "provisional"`。「Fixed / Getting better / Slipped」的判定依赖这些阈值（角度 ±5°、位移 ±2 cm、手速 ±0.5 m/s、节奏 ±0.3）。测量脚本已写好，但还没有真实数据。

### 4.6 GPU 可用性（中）

本机 worker 宕机时上传会被立即拒绝（503，不计次），这避免了用户白等，但旧机 10-26 停机数小时。现在有 30 分钟巡检，但巡检只发 GitHub 邮件。迁到 Modal 后这一项基本消失（`gpu.state = remote`）。

### 4.7 其他

- 条款和隐私政策是初稿，未经律师审阅；承诺「首次 Pro 付款 14 天内可退款」，退款需手动在 Stripe 后台操作。
- 分析完成邮件里的 `?session=` 链接：如果用户登录已过期，页面只会显示落地页，不会提示「登录后查看这次挥杆」。
- 语音提问只支持 `en-US`，依赖浏览器的 SpeechRecognition（Chrome、Safari 有，Firefox 没有）。
- 上传上限因部署而异：代码默认 512 MB，配置模板 300 MB，Caddy 200 MB，Mac mini 方案 95 MB（Cloudflare 免费版限制）。
- `motion_agent/docs/PLAYGROUND.md`、`TOC_PRODUCT.md` 还写着「默认是 Playground、`?consumer=1` 才是消费者版」，与现状相反；`deploy/LAUNCH.md` 的自测清单写「首页点 Try the sample」，但未登录访客现在看到的是落地页，没有这个按钮。
- iOS 采集端口 61016 是明文 gRPC、无认证，要确认没有暴露在公网。
- root 密码登录：交接文档记录用户决定暂不改。

---

## 5. 完整功能清单

产品界面是英文，下面引号里的是页面上的原文。

### 5.1 落地页（未登录访客）

未登录时只显示落地页，看不到应用本体。开发者界面（`?dev=1` 或开发者端口）永远不显示落地页。

| 区块 | 内容 |
|---|---|
| 顶部导航 | Logo；"How it works"、"What you get"、"Pricing" 锚点；"Sign in" |
| 首屏 | 标题 "Your swing, taken apart."；说明「拍一次挥杆，重建成带肌肉的 3D 骨骼，估算每个关节角度，告诉你先改哪一件事」；按钮 "Upload your swing" 和 "Watch it work"；"Free for 3 swings a month. No credit card."（次数从服务器读取） |
| 挥杆演示（滚动驱动） | 一位真实球员的手机视频、GEM-X 关节、OpenSim 骨骼肌肉在同一时间轴上播放，随滚动推进三幕："One phone, one swing." → "CeRebel finds every joint." → "Then it rebuilds your body."；读数显示「肩膀超过髋部」的角度（示例 39°），可点击相位跳转 |
| What comes back | 三个标签页：**The fix**（39°、为什么重要、要保持什么、今日练习）；**Every joint**（6 条测量：髋肩分离、骨盆转动、骨盆漂移、前臂弯曲、后膝弯曲、后手速度）；**Ask the coach**（一段示例对话） |
| 拍摄指引 | "Film it in thirty seconds"：手机立在髋部高度、侧面、2.5–3 米；全身和球杆入镜，有 60 fps 就用；一段只拍一次挥杆 |
| 隐私承诺 | "Your video is used to analyse your swing. That's it."：原始视频处理后自动删除；随时删除挥杆或账户；视频只通过会过期的私有链接加载 |
| 价格 | 免费 $0（每月 3 次）；已配置的付费方案（创始会员剩余名额、年付、月付）从 `/api/billing` 读取 |
| 结尾 | "See your own swing." + "Upload your swing" |
| 页脚 | © Cerebel, Inc.；Terms；Privacy |
| 交互细节 | 磁吸按钮、文字逐词出现、WebGL 流体背景；把视频拖到页面上会提示先登录；`prefers-reduced-motion` 时减少动画 |

### 5.2 账户

| 功能 | 规则 |
|---|---|
| 注册 | 邮箱、密码（至少 8 位）、可选教练码（字母数字和 `-_`，最多 32 位）、必须勾选："I'm 18+ (or have a parent's OK), I agree to the Terms and Privacy Policy, and I consent to CeRebel processing my video…"。邮箱已存在会报错 |
| 登录 | 邮箱 + 密码；错误统一提示 "Email or password is incorrect." |
| 会话 | token 存在浏览器 `localStorage`，30 天过期，每次请求更新「最后活跃时间」（用于 24 个月不活跃删除） |
| 登出 | 服务端作废 token |
| 删除账户 | "Delete account and videos"：删除账户、token、所有挥杆的文件和 GPU 结果、作品库记录、本地订阅记录、相关埋点。**有生效中的 Pro 订阅且未设为期末取消时会拒绝**，提示先去 Manage subscription 取消。Stripe 自己的付款记录保留 |
| 教练码 | 保存在账户上，结账时写进 Stripe 的 metadata，方便以后给教练分成 |

### 5.3 上传前

1. **拍摄指引**：落地页、登录后首页右侧（"How to film it" 图示和三步）、上传面板里的 "How to film for the best analysis" 折叠说明（侧面或正面、每次同一角度；全身和球杆入镜；手机固定在髋部高度、2.5–3 米；有慢动作用 120/240 fps；一段一次挥杆、30 秒内、画面里只有你）。
2. **视频处理同意（首次上传前）**：弹窗标题 "Before your first upload"，列出同意文本、每个处理方的名称/用途/国家、原始视频保留天数、不活跃删除期限；勾选 "I have read this and agree…" 后保存。同意版本变了会再次询问。不同意则上传取消，什么都不会写到服务器。
3. **浏览器端检查与裁剪**（`swing_trim.js`）：在用户自己的设备上运行 MediaPipe 姿态模型（文件从本站加载，不经第三方）。
   - 一个人都找不到：拦截，提示 "We couldn't find a person in this clip…"，不计次。
   - 大部分时间全身不在画面里，或画面里多于一个人：弹出警告，用户可以选 "Upload anyway?"。
   - 找到手部移动最快的时刻（击球），建议保留它之前 2.2 秒、之后 1.3 秒。
   - 模型加载失败或超过 25 秒：不拦截，直接上传整段。
4. **服务器端检查**（按顺序）：
   - 未登录 → 401 "Sign in to upload"
   - GPU 不可用 → 503 "Swing analysis is offline for a few minutes. Nothing was uploaded and it didn't use one of your analyses."
   - 本月额度用完 → 402，免费用户弹出升级窗口，Pro 提示合理使用上限 1 号重置
   - 未同意视频处理 → 428，重新弹出同意窗口
   - 文件格式不支持（只收 mp4、mov、m4v、webm、avi、mkv、mpeg、mpg）→ 400
   - 文件过大 → 413
   - ffprobe 质检：没有视频轨、短于 1.5 秒、长于 30 秒、帧率低于 24、短边小于 320 → 422，视频立即删除，"This didn't use one of your analyses."
   - 质检提示（不拦截）：低于 60 fps 提示击球瞬间精度会下降；短边小于 480 提示部分角度精度会下降
   - 裁剪：服务器校验浏览器建议的窗口（1.5–10 秒，且确实比原片短），用 ffmpeg 精确裁剪并只保留这段，原片删除

### 5.4 分析流水线与等待页

| 阶段（stage） | 页面上的步骤 | 进度条 | 说明 |
|---|---|---|---|
| `uploading` | Upload received | 5–8% | 接收文件 |
| `extracting` | Preparing frames | 12–15% | 抽帧（和 GPU 排队并行） |
| `queued` | Waiting in line | 22% | 只有真的在排队时才显示这一步；显示 "N swings ahead of you — about M min." 或 "You're next — about M min." |
| `gemx` | Rebuilding your body | 45→74% | GEM-X 3D 重建，进度按时间平滑爬升 |
| `ik` | Measuring angles | 78→96% | OpenSim IK、估算动力学 |
| `ready` / `no_swing` / `error` | — | 100% | 结束 |

- 上传成功后页面直接切到等待页（"Working on your swing"），分析完成后自动变成报告。
- 能发邮件时副标题写 "You can close this page; we'll email you when it's ready."，否则写 "This page updates on its own."
- 预计时间按 `CEREBEL_AVG_JOB_SECONDS`（默认 150 秒）和并行 worker 数估算。
- 超过 45 分钟未完成按失败处理，不计次。
- GPU worker 连接被拒时，任务会等它最多 5 分钟（重启约 2 分钟）再判定为 `gpu_unavailable`。
- 失败原因记录在 `failed_at`（input / gpu_unavailable / gemx / ik / start / timeout），用户只看到通用说明，不看到原始报错。
- 完成后自动识别运动：高尔夫挥杆置信度 ≥ 0.45 为 `ready`；否则为 `no_swing`，提示 "We rebuilt the body but couldn't confirm a golf swing. Try a side-on clip that shows a full swing."，不计次。
- 配置了 SMTP 时发邮件："Your swing analysis is ready"（附 `?session=` 链接）或 "We couldn't analyse your last clip"（附拍摄建议，说明不计次）。

### 5.5 报告页

登录后打开一个已完成的挥杆，从上到下是：

**主修正卡（"Biggest thing to fix"）**

- 标题（例如 "Your back knee locks too early"）、白话解释、测量值和说明、动作要点（cue）。
- 多个问题时有标签页切换，按严重度排序，最多 4 个。
- "Play this moment"：视频跳到问题发生的时间窗口播放（这次挥杆没有视频时隐藏，例如原始视频已按 30 天规则删除）。
- "Why?"：右侧抽屉解释为什么重要，可继续 "Ask coach more"。
- "Today's drill"：练习内容、"Next swing" 检查目标（例如「上杆顶点后膝保持在 35° 左右，而不是掉到 20°」）、"Mark as practiced"（记录次数，显示 "Practiced N times"）、练过之后出现 "Film again to check"。
- "Compare"：同一段挥杆的两个时刻（准备姿势 vs 击球）左右滑动对比。
- "Hold to talk"：语音提问（英文）。
- 即时回答按钮：常见问题（"What's the biggest thing I should fix in this swing?"、"Walk me through my downswing." 等）直接用测量结果拼出答案，不调用语言模型，零成本、零等待。
- 相位条：Address、Takeaway、Backswing、Transition、Downswing、Impact、Follow-through，可点击。

**骨架叠加（10-26 新增）**

- 在用户自己的视频上按帧画出关节和骨骼（适配黑边），可开关，选择记在浏览器里。
- 当前问题的时间窗口内，对应身体部位变成琥珀色，并画出要改什么：膝或肘的角度和测量值、"keep this bend" / "keep the arm long" 箭头、肩线和髋线及其夹角、击球时的髋线和髋部移动路径。

**四条问题规则**（右手球员：前侧 = 左，后侧 = 右；左手球员反过来）

| 问题 id | 标题 | 触发条件 | 练习 |
|---|---|---|---|
| `trail-knee` | Your back knee locks too early | 后膝弯曲从准备到顶点减少 ≥ 8° | 双脚并拢慢速上杆 10 次，再完整挥一次 |
| `lead-arm` | Your front arm collapses at the top | 顶点前臂弯曲比准备时多 ≥ 15°，或顶点 ≥ 45° | Pump drill：顶点停住、前臂伸直，下压到一半两次再挥，8 次 |
| `separation` | Hips and chest are turning as one block | 顶点髋肩分离 < 20° | 髋部先动的半挥杆：骨盆先转，肩膀停一拍，10 次 |
| `early-extension` | You're standing up through the ball | 击球后 0.28 秒内骨盆向球移动 ≥ 3.5 cm | 椅子练习：椅子放在臀部后方，挥 10 次不碰到 |

触发少于 2 条时，用测量曲线补充说明性读数，保证报告至少有内容可看。没触发的规则会生成「做得好的地方」（例如 "Lead arm stays long: elbow flex is 28° at the top."），教练回答时可以引用；页面上的 "What you're doing well" 列表目前只在开发者界面显示，消费者界面隐藏了。

**测量面板**（在主修正卡下方）

- 3D 特写 "Bones + muscles" 占满宽度：OpenSim 骨骼加相关肌群（例如后侧梨状肌和臀中肌），问题部位高亮；右上角是各个问题的切换按钮。
- 6 条曲线，每条带实时读数、问题窗口高亮和目标线：后膝弯曲、前臂弯曲、髋肩分离、骨盆转动、骨盆漂移、后手速度。
- 播放按钮、时间轴拖动、相位刻度；播放到问题窗口时自动放慢到 0.4 倍速。
- 说明：数字是单部手机视频的估算值（beta）。

**页面下方**

- "Choose your sport" 运动目录：高尔夫可用，其他运动显示为锁定的 coming soon。
- 点 "Ask coach" 后，原始视频播放器和对话框在页面下方展开（平时隐藏）。

**其他**

- "Does this match what you feel, or what your coach says?" 是 / 否，用来收集信任度反馈。
- 示例挥杆：登录后首页 "See a sample swing" 打开服务器指定的 demo（`CEREBEL_DEMO_SESSION_ID`），页面标注 "Sample swing"。
- 只在开发者界面出现：挥杆相位卡片和 3D 相位预览、提示词模式切换、系统提示词编辑。

### 5.6 AI 教练

- 入口：右下角 "Ask coach"（有挥杆时才出现，点开后视频播放器和对话框在页面下方展开）、"Why?" 抽屉里的 "Ask coach more"、即时回答按钮。
- 回答是流式的（SSE），过程中显示正在做什么，然后给出 Markdown 回答；回答里的时间戳可点击跳转视频。
- 语言模型通过 OpenAI 兼容接口调用（`MOTION_AGENT_BASE_URL` / `MOTION_AGENT_MODEL`），计划用 Together AI 上的 Qwen；可关闭「思考」以省钱提速；每个问题最多 12 次工具调用。
- 服务器会先替模型跑好挥杆分段和测量面板，模型只能引用这些数字。
- 18 个工具：姿态状态、运动事件、挥杆分段、测量面板、关节轨迹、重心轨迹、足部接触、动力学摘要、关节动力学、估算地面反作用力、关节功率、取视频帧、取视频片段、动作对比、动作搜索、受力示意图、知识库检索、知识库来源列表。
- 知识库：高尔夫语料 36 个文件（规则、差点、器材、生物力学、分类、本体、常见问题、练习等），只把检索到的片段给模型，不上传整份文件。
- 提问的运动模式由服务器按这次挥杆识别出的运动决定，用户不能切换；客户端不能覆盖系统提示词或切换后端（只有开发者端口可以）。
- 每日限额（UTC 零点重置）：未登录 3 次（另有全体匿名访客合计 300 次/天）、免费 15 次、Pro 100 次。匿名访客按 IP 计数，但磁盘上只存每日更换密钥的 HMAC。

### 5.7 作品库（"Your swings"）

- 从导航 "Library" 或账户按钮打开，作为抽屉覆盖在页面上；Esc 或点外面关闭。
- 顶部是套餐栏：Free / Pro 标签、"2 of 3 analyses left this month"、取消后显示 "· ends at period end"、有订阅时的 "Manage subscription"、免费用户的 "Upgrade" 按钮。
- 每条显示名称（"Swing · Oct 26, 3:12 PM"）、状态（Receiving / Preparing / Waiting / Rebuilding body / Measuring / Ready / No clear swing / Needs another clip）和排队信息。
- 点 Ready 的挥杆打开报告；点处理中的显示当前进度。
- "Delete"：确认后删除视频和分析，立即生效；打开着的报告会跳到其他挥杆。
- 有处理中的挥杆时每 4 秒自动刷新。
- 上传入口 "Upload a clip"，也可以把视频拖到页面任意位置。

### 5.8 进步对比（10-26 新增）

- 有两次可测量的挥杆后，主修正卡下方出现卡片 "See what changed since your last swing."，点开是对比面板。
- 默认对比最近两次。**免费用户只能对比相邻的两次**；任选两次是 Pro 功能（402 时弹出升级窗口 "Compare any two swings"，可点 "Back to your last two"）。
- 面板内容：
  - 「Last time's main fix」：上一次的主修正这次怎么样了，状态为 Fixed / Close / Getting better / No clear change / Slipped / Can't compare，附前后数值和目标线。
  - 6 项指标：肩膀超过髋部（顶点）、后膝弯曲损失、前臂弯曲（顶点）、击球后髋部漂移、后手峰值速度、节奏（上杆 : 下杆）；每项给出前后值、变化、误差范围（±）和 Better / Worse / No clear change。后手速度和节奏只展示不评判。
  - 按相位对齐的曲线叠加（Address、Top、Impact、Finish 落在同一位置）。
  - 两段视频并排，可 "Play both, half speed"，视频上同样叠加骨架并标出上次的主修正。
  - 下一次的重点（next issue）。
- 可比性检查：两次拍摄角度相差超过 20°，转体和髋部漂移不评判；帧率相差超过 1.5 倍，速度和节奏不评判；任一段找不到完整挥杆则不对比。
- 原始视频已被 30 天规则删除时显示 "Video removed after 30 days for privacy. The measurements are kept."
- 底部说明：单部手机的估算值，小于误差范围的变化可能是噪声。

### 5.9 计费

| 项目 | 规则 |
|---|---|
| 计次 | 按自然月（UTC）统计本月创建的挥杆。失败、识别不到挥杆、被删除、质检拒绝的都不算 |
| 免费 | 每月 3 次 |
| Pro | 每月 60 次（合理使用上限）；任选两次对比；每天 100 个教练问题 |
| 方案 | 创始会员 $69 首年（之后 $99.99/年，显示剩余名额，用完自动隐藏）；Pro 年付 $99.99（约 $8.33/月）；Pro 月付 $14.99（配置了才显示）。非创始会员方案允许输入促销码 |
| 升级入口 | 作品库套餐栏的 "Upgrade" 按钮（随时可点）；额度用完时自动弹出；对比非相邻挥杆时弹出；付费墙列出权益和 "Secure checkout by Stripe" |
| 结账 | 跳到 Stripe 托管页面，回来后提示 "Welcome to Pro! It can take a few seconds to show up." 或 "Checkout cancelled. You're still on the free plan." |
| 管理订阅 | "Manage subscription" 打开 Stripe Customer Portal（取消、换卡） |
| webhook | 校验签名（5 分钟时间窗）、按事件 id 去重；处理 `checkout.session.completed` 和订阅创建/更新/删除 |
| 宽限 | 订阅到期后多给 1 天，防止续费 webhook 延迟导致误锁 |
| 未开通时 | 没配置 Stripe 时付费墙显示 "Pro is opening soon. We'll email you when upgrades are live." |

### 5.10 隐私与数据

| 项目 | 做法 |
|---|---|
| 可见范围 | 只能看到自己的挥杆和服务器指定的那一个 demo；别人的挥杆返回 403；删除过的返回 410 |
| 视频链接 | `<video>` 不能带登录头，所以媒体地址带签名，6 小时过期；挥杆被删除后签名立即失效；`Referrer-Policy: no-referrer` |
| 原始视频 | 处理后 30 天删除原片、裁剪片段、抽帧、`session.mp4`、封面图和 GEM-X 的其他输出；3D 参数、角度、负荷保留，并留下 `retention.json` 标记 |
| 不活跃账户 | 24 个月无活动整体删除（有续费中订阅的跳过并列出） |
| 其他保留规则 | 无主的会话目录、孤儿 GEM-X 目录、旧版颈部 IK 残留、GEM-X 日志、旧版埋点文件、前一天的提问计数，都按规则删除 |
| 脱敏 | `hpe_results.pt` 处理完立即删除 `net_outputs`，头部形状系数清零（重建出来是平均头型）；GEM-X 不预测表情 |
| 文件锁 | `events.jsonl`、`library.json`、`accounts.json`、`tokens.json`、`billing.json`、`question_counts.json` 跨进程加锁（retention 和清理脚本是另外的进程） |
| 安全 | 只放行白名单跨域；不向客户端返回报错堆栈；`/static` 路径穿越已修；内部查看器只监听本机；操作员密码只从环境变量读取并只接受请求头 |
| 不做的事 | 不用视频识别身份、不建人脸模板、不卖数据、不做医疗诊断（写在同意文本里） |
| 法律页 | `/terms`（11 节）、`/privacy`（18 节，含 BIPA、德州 CUBI、华盛顿 MHMDA、科罗拉多、加州 CCPA）；公司名、邮箱、保留天数从环境变量填入 |

### 5.11 运维与开发者工具

- `GET /api/health`：进程、磁盘、模型凭证、GPU 状态（ready / loading / remote / down / unknown）；不可用时返回 503。
- `GET /api/ops`：各类埋点计数和上传失败率（需要操作员密码，只在开发者界面可用）。
- 开发者界面：设置 `CEREBEL_OPERATOR_PORT`（如 8093）后只在服务器本机端口提供，通过 SSH 隧道访问；有提示词模式切换（通用、单板、高尔夫、棒球）、系统提示词查看和编辑（需密码）、原始会话信息、自我视角网格演示。
- 每日备份账户和订阅数据，保留 14 天。
- `.github/workflows/uptime.yml`：每 30 分钟检查页面 200 和 GPU 状态为 ready 或 remote。
- 埋点写在 `data/product/events.jsonl`，事件列表见附录。

### 5.12 运动包（Sport Packs）

| 运动 | 状态 | 说明 |
|---|---|---|
| 高尔夫 | live | 检测器、分段、测量面板、四条规则、即时回答、知识库 |
| 通用动作（general） | live（兜底） | 识别不到具体运动时使用；产品里会标为 no_swing |
| 棒球 | coming soon | 有投球分段、测量面板和规则，检测器未开 |
| 网球、篮球、足球、跑步、举重、排球、拳击、瑜伽、单板滑雪 | coming soon | 只有知识库和目录卡片 |
| 游泳 | beta 标签 | 同上 |

知识库在 `motion_agent/knowledge/corpus/<运动>/`，每个运动按 Biomechanics、Coaching、Findings、Phases、Quality、Schema、Taxonomy、Training 等分类组织。

### 5.13 内部和遗留组件

| 组件 | 端口 | 用途 |
|---|---|---|
| iOS 采集服务（`server.py` + `capture.proto`） | 61016 | iOS App 流式上传 640×480 JPEG 帧 |
| 3D 网格查看器（`mesh_viewer_service.py`） | 8090，仅本机 | GEM-X 结果查看 |
| Kinetic Lens（`kinetic-lens/`、`kinetic_lens_viewer.py`） | 8091，仅本机 | OpenSim Motion Lab 演示：视频、肌骨模型、关节曲线三视图同步，估算 GRF 和关节反力 |
| `upload_server.py` | 8093（独立运行时） | 旧上传页；现在由产品服务导入，只用它的 GPU 队列 |
| `web_api.py` + `webapp/` | 8093 | 第一版 React 网页后端，已停用 |

---

## 6. 用户使用的完整步骤

以一位第一次来的美国业余球手为例，从打开网站到取消订阅、删除账户。

### 第 1 步：打开网站

1. 访问 `https://cerebel.tech/playground` 或 `https://playground.cerebel.tech`。
2. 未登录，看到落地页（见 5.1）。可以往下滚动看挥杆演示、"What comes back" 的三个标签、拍摄指引、隐私说明和价格。
3. 页面记录 `landing_view` 事件。

### 第 2 步：创建账户

1. 点 "Upload your swing"（首屏或页尾）。账户面板以对话框形式打开，并且已切到 "Create account"。（点右上 "Sign in" 则是登录模式。）
2. 填写邮箱、密码（至少 8 位）；如果是教练推荐来的，填教练码。
3. 勾选同意条款、隐私政策和视频处理。不勾选会提示 "Please tick the box to accept the Terms and Privacy Policy."
4. 点 "Create account"。成功后落地页消失，进入应用。
5. 新账户还没有挥杆，看到首页：标题 "See your swing as a full 3D body. Fix the one thing that matters."、"Upload a swing"、"See a sample swing"，右侧是 "How to film it" 图示和三步。

### 第 3 步（可选）：先看示例

1. 点 "See a sample swing"，页面加载服务器指定的示例挥杆并滚动到主修正卡。
2. 页面标注 "Sample swing"。可以体验报告页的所有阅读功能，但不能标记练习。

### 第 4 步：拍摄

1. 手机竖着或横着都行，固定在髋部高度，离人 2.5–3 米，从侧面（down the line）或正面拍。
2. 全身和球杆从准备到收杆都在画面里，画面里只有自己。
3. 有慢动作就用 120 或 240 fps，至少 60 fps 最好；光线充足。
4. 一段视频只拍一次挥杆，30 秒以内。

### 第 5 步：上传

1. 点 "Upload a swing" 打开作品库抽屉，点 "Upload a clip" 选择视频；或者直接把视频拖到页面上。
2. **第一次上传**会弹出 "Before your first upload"：阅读同意文本和处理方名单，勾选后保存。不同意则上传取消。
3. 浏览器开始检查视频，状态栏显示进度：
   - 找不到人：显示 "We couldn't find a person in this clip…" 并停止，不计次。
   - 全身大部分时间不在画面里或有多个人：弹窗警告，用户可选择继续上传。
   - 正常：自动选出击球前后约 3.5 秒的挥杆窗口。
4. 显示 "Uploading…"，视频（连同建议的裁剪窗口）上传到服务器。
5. 服务器检查（见 5.3 第 4 条）。不合格的会立即说明原因，并写明 "This didn't use one of your analyses."；有质量提示的（如低于 60 fps）会附在成功消息后面。
6. 成功后抽屉关闭，页面回到顶部，显示等待页。

### 第 6 步：等待分析

1. 等待页 "Working on your swing" 显示步骤：Upload received → Preparing frames →（排队时）Waiting in line → Rebuilding your body → Measuring angles，以及进度条和排队信息（"2 swings ahead of you — about 4 min."）。
2. 通常几分钟。页面自己刷新；如果服务器能发邮件，可以关掉页面，等 "Your swing analysis is ready" 邮件，点里面的链接回来（需要仍处于登录状态）。
3. 作品库里这条记录同步显示状态。

### 第 7 步：阅读报告

1. 分析完成后等待页自动变成报告。如果识别不到高尔夫挥杆，会提示 "We couldn't confirm a golf swing in this clip. Try a side-on take that shows a full swing."，不计次，回到第 4 步重拍。
2. 先看主修正卡 "Biggest thing to fix"：标题、白话解释、测量值、动作要点。
3. 视频上叠加着骨架，问题部位在问题发生时变成琥珀色并标出数值。点 "Play this moment" 只看那一段。
4. 有多个问题时，点上方标签切换。
5. 点 "Why?" 看为什么重要。
6. 点 "Compare" 拖动滑块对比准备姿势和击球瞬间。
7. 往下看测量面板：3D 骨骼肌肉特写，点右上角切换问题；拖动时间轴或点播放，看 6 条曲线随挥杆变化，到问题窗口时自动放慢。
8. 回答 "Does this match what you feel, or what your coach says?"（是 / 否）。

### 第 8 步：练习

1. 点 "Today's drill"，看练习方法和 "Next swing" 检查目标。
2. 去练习场练完后点 "Mark as practiced"，显示 "Practiced 1 time"，提示 "Marked as practiced. Film again when you're ready to check it."
3. 出现 "Film again to check"，点它回到上传。

### 第 9 步：问教练

1. 点即时回答按钮（如 "What's the biggest thing I should fix in this swing?"），立刻得到基于测量的回答，不消耗语言模型。
2. 点 "Ask coach"（或 "Why?" 抽屉里的 "Ask coach more"），输入问题，例如 "Is my lead arm collapsing before impact?"；也可以按住 "Hold to talk" 用英文说。
3. 回答逐步出现，里面的时间戳可以点击跳到视频对应位置。
4. 免费账户每天 15 个问题，到达后提示 "You've reached today's question limit. It resets at midnight UTC."

### 第 10 步：再拍一次，看进步

1. 重复第 4–6 步上传新的挥杆。
2. 新报告的主修正卡下方出现 "See what changed since your last swing."，点开对比面板。
3. 先看 "Last time's main fix"：上次的问题现在是 Fixed、Getting better 还是 No clear change。
4. 看 6 项指标的前后变化和误差范围、按相位对齐的曲线，点 "Play both, half speed" 并排看两段视频。
5. 如果两次拍摄角度或帧率差别太大，面板会说明哪些数字没有比较；以后尽量在同一位置、同一模式拍摄。
6. 免费用户想对比更早的一次时，会弹出升级窗口。

### 第 11 步：额度用完，升级

1. 当月第 4 次上传时，服务器返回额度用完，弹出付费墙 "You're out of free analyses"，列出 Pro 权益和可选方案。
2. 选创始会员、年付或月付，点 "Choose"，跳到 Stripe 结账页付款。
3. 付款成功后回到网站，提示 "Welcome to Pro! It can take a few seconds to show up."，额度变成每月 60 次。
4. 取消付款则提示 "Checkout cancelled. You're still on the free plan."
5. 不升级的话，下个月 1 号（UTC）免费额度自动恢复。
6. 也可以不等额度用完，随时在作品库套餐栏点 "Upgrade"。

### 第 12 步：管理订阅

1. 打开作品库抽屉，在顶部套餐栏点 "Manage subscription"，打开 Stripe 的订阅管理页面，可以取消订阅或换卡。
2. 取消后 Pro 用到当期结束，套餐栏显示 "· ends at period end"。
3. 首次 Pro 付款 14 天内不满意可以发邮件申请退款（条款承诺，人工处理）。

### 第 13 步：删除数据

1. 删除单个挥杆：作品库里点 "Delete" 并确认，视频和分析立即从服务器删除。
2. 删除账户：点 "Delete account and videos" 并确认。如果有生效中的 Pro 订阅，会提示先取消订阅。删除后回到落地页。
3. 不主动删除时：原始视频在处理 30 天后自动删除（测量结果保留，报告里视频位置会显示已删除）；24 个月没有任何活动，账户和所有数据自动删除。

### 异常情况速查

| 情况 | 用户看到 | 是否计次 |
|---|---|---|
| 视频里找不到人（浏览器检查） | "We couldn't find a person in this clip…" | 否（没有上传） |
| 视频太短、太长、帧率太低、分辨率太低、没有视频轨 | 具体原因 + "This didn't use one of your analyses." | 否（视频立即删除） |
| GPU 离线 | "Swing analysis is offline for a few minutes…" | 否（没有上传） |
| 未同意视频处理 | 同意弹窗 | 否 |
| 额度用完 | 付费墙 | — |
| 不是高尔夫挥杆 | "We couldn't confirm a golf swing…" | 否 |
| 分析失败 | "Something went wrong on our side while analyzing this video. Please try again." | 否 |
| 处理超过 45 分钟 | "Processing took too long…" | 否 |
| 打开别人的挥杆链接 | "That capture belongs to another account" | — |
| 打开自己删掉的挥杆 | "You deleted this swing." | — |
| 教练提问到上限 | "You've reached today's question limit…" | — |

---

## 7. 附录

### 7.1 接口一览（`motion_agent/app/chat_server.py`）

| 方法 | 路径 | 说明 | 鉴权 |
|---|---|---|---|
| GET | `/` | 页面（落地页 / 应用 / 开发者界面） | — |
| GET | `/terms`、`/privacy` | 法律页 | — |
| GET | `/static/*` | 前端资源 | — |
| GET | `/media/<session>/<file>` | 视频等媒体 | 签名或本人 / demo |
| GET | `/api/billing` | 方案、额度、订阅 | 可选 |
| GET | `/api/health` | 健康检查 | — |
| GET | `/api/ops` | 埋点计数 | 开发者端口 + 密码 |
| GET | `/api/me`、`/api/library` | 账户和作品库 | 登录 |
| GET | `/api/session` | 当前要显示的挥杆（自己的、demo、等待中、空） | 可选 |
| GET | `/api/progress` | 某个挥杆的处理进度 | 本人 |
| GET | `/api/compare` | 两次挥杆对比 | 登录 |
| GET | `/api/sports` | 运动目录 | — |
| GET | `/api/golf_phases` | 挥杆分段 | 本人 / demo |
| GET | `/api/sport_analysis` | 测量面板和问题 | 本人 / demo |
| GET | `/api/swing_keypoints` | 骨架叠加用的投影关节点 | 本人 / demo |
| GET | `/api/coach_briefs` | 即时回答 | 本人 / demo |
| GET | `/api/smplx_mesh_status`、`/api/smplx_mesh_clip` | 3D 骨骼肌肉动画片段 | 本人 / demo |
| GET | `/api/system_prompt` | 系统提示词 | 开发者端口 + 密码 |
| POST | `/api/auth/register`、`/login`、`/logout` | 账户 | — |
| POST | `/api/account/video-consent` | 记录视频处理同意 | 登录 |
| POST | `/api/account/delete` | 删除账户 | 登录 |
| POST | `/api/upload` | 上传视频（multipart，可带 `trim_start` / `trim_end`） | 登录 |
| POST | `/api/library/delete` | 删除挥杆 | 登录 |
| POST | `/api/library/practice` | 标记练习 | 登录 |
| POST | `/api/billing/checkout`、`/api/billing/portal` | Stripe 结账 / 订阅管理 | 登录 |
| POST | `/api/stripe/webhook` | Stripe 回调 | 签名 |
| POST | `/api/events` | 前端埋点（白名单） | 可选 |
| POST | `/api/chat`、`/api/chat/stream` | 教练问答（流式为 SSE） | 可选（受每日限额） |

### 7.2 关键数字

| 项目 | 值 | 位置 |
|---|---|---|
| 免费 / Pro 每月分析 | 3 / 60 | `CEREBEL_FREE_MONTHLY`、`CEREBEL_PRO_MONTHLY` |
| 创始会员名额 | 200 | `CEREBEL_FOUNDING_LIMIT` |
| 教练每日问题 | 匿名 3、免费 15、Pro 100、匿名合计 300 | `CEREBEL_*_DAILY_QUESTIONS` |
| 每题最多工具调用 | 12 | `MOTION_AGENT_MAX_TOOL_CALLS` |
| 视频时长 | 1.5–30 秒 | `product_api.py` |
| 最低帧率 / 提示阈值 | 24 / 60 fps | 同上 |
| 最小短边 / 提示阈值 | 320 / 480 px | 同上 |
| 浏览器建议窗口 | 击球前 2.2 秒、后 1.3 秒 | `swing_trim.js` |
| 服务器接受的裁剪长度 | 1.5–10 秒 | `product_api._parse_trim` |
| 高尔夫识别阈值 | 挥杆置信度 ≥ 0.45 | `sports/packs/golf.py` |
| 处理超时 | 45 分钟 | `PIPELINE_TIMEOUT_S` |
| 平均处理时间（估算用） | 150 秒 | `CEREBEL_AVG_JOB_SECONDS` |
| 登录有效期 | 30 天 | `CEREBEL_TOKEN_TTL_DAYS` |
| 媒体签名有效期 | 6 小时 | `CEREBEL_MEDIA_URL_TTL_S` |
| 原始视频保留 | 30 天 | `CEREBEL_RAW_VIDEO_RETENTION_DAYS` |
| 不活跃删除 | 730 天 | `CEREBEL_INACTIVE_DELETE_DAYS` |
| 备份保留 | 14 天 | 部署脚本 |
| 建模身高 / 体重 | 185 cm / 93 kg（所有用户） | `ik_pipeline.py` |
| 对比：角度容差 / 帧率比 | 20° / 1.5 倍 | `swing_compare.py` |
| Modal GPU | L40S，美国，最多 10 个容器 | `deploy/modal_gemx.py` |

### 7.3 埋点事件

服务端：`signup`、`video_consent`、`upload_needs_consent`、`paywall_hit`、`upload_rejected`、`upload_trimmed`、`upload_saved`、`upload_unavailable`、`capture_finalized`、`email_sent`、`pipeline_error`、`capture_deleted`、`practice_logged`、`account_deleted`（不含 user_id）、`chat_question`、`chat_error`、`checkout_created`、`billing_<Stripe 事件类型>`、`compare_view`。

前端（加 `client_` 前缀记录）：`landing_view`、`sample_report_view`、`signup_view`、`upload_click`、`report_view`、`fix_expand`、`trust_yes`、`trust_no`、`chat_open`、`paywall_view`、`checkout_click`、`share_click`、`progress_card_view`、`progress_open`。

### 7.4 主要配置项

完整列表见 `deploy/env.product.example`。上线必须填：`CEREBEL_PUBLIC_URL`、4 个 Stripe 变量、`CEREBEL_LEGAL_ENTITY`、`CEREBEL_SUPPORT_EMAIL`、`MOTION_AGENT_PROMPT_PASSWORD`、`MOTION_AGENT_BASE_URL` / `MOTION_AGENT_MODEL` / `MOTION_AGENT_API_KEY`、`CEREBEL_GPU_BACKEND`（及 Modal token）、`CEREBEL_DEMO_SESSION_ID`。可选：`CEREBEL_SMTP_*`（邮件）、`CEREBEL_BASE_PATH` / `CEREBEL_UPLOAD_ORIGIN`（挂在主站路径下时）、`CEREBEL_OPERATOR_PORT`（开发者端口）。

### 7.5 关键文件

| 文件 | 作用 |
|---|---|
| `motion_agent/app/chat_server.py` | 对外 HTTP 服务和全部接口 |
| `motion_agent/app/product_api.py` | 上传、质检、额度检查、进度、删除、埋点 |
| `motion_agent/app/accounts.py` / `consent.py` | 账户、同意文本和版本 |
| `motion_agent/app/billing.py` | 方案、额度、Stripe、提问限额 |
| `motion_agent/app/library.py` | 作品库和练习记录 |
| `motion_agent/app/compare.py` + `motion/swing_compare.py` | 进步对比 |
| `motion_agent/motion/swing_overlay.py` + `ui/skeleton-overlay.js` | 骨架叠加 |
| `motion_agent/motion/golf_phases.py` | 挥杆分段（匈牙利匹配，一次挥杆只对应一个击球峰） |
| `motion_agent/motion/sport_analysis.py` | 测量曲线和问题规则 |
| `motion_agent/llm/agent.py` / `prompts.py` / `schemas.py` | 教练的工具循环、提示词、工具定义 |
| `motion_agent/app/retention.py` / `retention_schedule.py` / `privacy_backfill.py` | 数据保留和隐私清理 |
| `motion_agent/app/ui/index.html` | 前端主页面（约 6,400 行） |
| `motion_agent/app/ui/billing.js` / `compare.js` / `swing_trim.js` | 付费和同意弹窗、对比面板、上传前检查 |
| `upload_server.py` / `gemx_pipeline.py` / `ik_pipeline.py` | GPU 队列、GEM-X、OpenSim IK |
| `hpe_privacy.py` | GEM-X 结果脱敏 |
| `deploy/` | 三套部署方案、Modal、supervisor、运行手册 |
| `HANDOFF.md` / `DESIGN.md` | 交接文档（部分过时）、设计规范草稿 |
