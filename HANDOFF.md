# CeRebel 交接文档（给下一个 AI 助手或开发者）

更新于 2026-10-24。这份文档不包含任何密码、密钥或 token。凭证都只存在服务器的 `.env.product` 和各服务商后台里，请不要写进仓库或聊天。

## 1. 产品是什么

CeRebel（Cerebel, Inc.，Delaware C-corp，团队在美国）：用户用手机拍一段动作视频，系统重建 3D 肌骨模型（OpenSim），算出关节角度和估算负荷，再由 AI 教练解释。教练回答里的每个数字都来自确定性工具，不由语言模型生成。

- 第一个运动是高尔夫，之后做滑雪/单板、网球、跑步、力量训练。
- 面向海外消费者，先做网页版，iOS 往后放。
- 定价：免费每月 3 次分析；Pro $99.99/年；创始会员首年 $69，限 200 名；月付 $14.99 可选。
- 官网 cerebel.tech（托管在 Vercel）和产品站 playground.cerebel.tech 先分开。这一阶段先迭代 Playground，转化路径稳定后再考虑合并。
- 客服邮箱：garylin@cerebel.tech（SpaceMail）。

## 2. 代码在哪

- 本地仓库：`~/Developer/CeRebel`，远程是 `github.com/Gary-Lin08/CeRebel`（私有，分支 main）。
- 本地 main 比 origin/main 多 3 个提交，还没推送：
  - 上传质检
  - 失败上传后清空文件框
  - 只公开指定的 demo session
- 推送命令：`git push origin main`。
- 服务器上的代码目录：`/root/CerebelServer`（GPU 云，onethingai，经代理 SSH 访问）。

### 关键文件

| 文件 | 作用 |
|---|---|
| `motion_agent/app/chat_server.py` | 对外的网页应用和 API（8092 端口）：登录、上传、报告、教练、计费、webhook、条款 |
| `motion_agent/app/product_api.py` | 账户、作品库、上传质检、月度额度、埋点、隐私范围 |
| `motion_agent/app/billing.py` | 套餐、额度、Stripe Checkout/Portal、webhook 签名校验（只用标准库） |
| `motion_agent/app/accounts.py` | 注册（必须勾选同意，可填教练码）、登录、token 过期 |
| `motion_agent/app/ui/index.html` | 前端主页面（约 5,800 行）。默认是消费者界面，`?dev=1` 打开操作员界面 |
| `motion_agent/app/ui/billing.js` / `billing.css` | 升级弹窗、套餐卡片、首页文案、拍摄指引、"这和你的感觉一致吗"、埋点 |
| `motion_agent/app/ui/legal/terms.html` / `privacy.html` | 条款和隐私页，公司名和邮箱从环境变量读取 |
| `upload_server.py` | GPU 队列（`ensure_queue()` 懒加载） |
| `deploy/run-product.sh` | 启动/停止/重启网页应用：`start` / `stop` / `restart` / `status` |
| `deploy/env.product.example` | 服务器 `.env.product` 的模板，列出所有环境变量 |
| `deploy/LAUNCH.md` | 上线步骤和自测清单 |
| `.github/workflows/deploy.yml` | 推送 main 后自动 rsync 到服务器并重启（需要先配好 Secrets） |
| `motion_agent/tests/test_billing.py` | 计费、额度、质检、同意条款、埋点相关测试 |

测试命令：`python3 -m unittest discover -s motion_agent/tests -t .`

- 本地有 1 个原有失败：`test_golf_kb_scope_defaults`。
- 另有 20 个测试依赖服务器上 `/root/CerebelServer/data` 里的数据，本地跑不了。
- 新增的计费和账户相关测试全部通过（42 个）。

## 3. 已完成（截至这份文档）

- 两个安全补丁：
  - 操作员密码只从环境变量读取；
  - 登录 token 30 天过期；
  - 不再向客户端返回报错堆栈；
  - 跨域请求只放行白名单。
- 修复：网页上传的视频之前进不了 GPU 队列，现在能正常排队，处理完成后会做运动识别。
- 每月免费额度：上传失败、被质检拒绝、识别不到挥杆的都不计次。
- 上传质检：时长 1.5–30 秒、帧率 ≥ 24、短边 ≥ 320，不合格的直接拒绝并删除视频；低于 60 fps 的给提示。
- Stripe 订阅、升级弹窗、订阅管理页、webhook。
- 注册时必须勾选同意，可填教练码，教练码会写进 Stripe 元数据。
- 条款和隐私页。
- 服务端和前端埋点，写在 `data/product/events.jsonl`。
- 默认不把视频帧发给语言模型（`CEREBEL_LLM_ATTACH_VIDEO=0`）。
- 内部查看器（8090/8091）和 `web_api.py` 只绑定 127.0.0.1。
- 只有 `CEREBEL_DEMO_SESSION_ID` 指定的 session 能被匿名访问。之前的逻辑会把没有归属的旧 session 当成公开示例。
- 用 Playwright 在电脑和手机两种尺寸上走过界面流程：注册同意、额度显示、升级弹窗、上传被拒。

### 另一个助手（Codex）在线上环境完成的（根据它的操作记录）

- **Stripe 正式模式**：
  - 建好了正式价格和创始会员优惠券（限 200 次，仅适用 CeRebel Pro）；
  - 建好了 webhook，监听 4 个事件：checkout.session.completed 和 customer.subscription.created / updated / deleted；
  - 正式密钥和 webhook 签名密钥已写入服务器的受限配置文件。
- **Cloudflare**：
  - 建了 "CeRebel Playground" 隧道并已连上服务器；
  - 公开路由 playground.cerebel.tech 指向服务器本机服务，CNAME 已在 Cloudflare 里创建；
  - 补了 SpaceMail 的 DKIM 记录；
  - 主站和 www 记录设为仅 DNS，继续直连 Vercel。
- **域名**：
  - 已在 Spaceship 关闭 DNSSEC，正在等旧 DS 记录过期；
  - **还没切换名称服务器到 Cloudflare**；
  - 切换后要在 Cloudflare 重新开启 DNSSEC。
- **服务器**：
  - 产品版代码已同步，在独立测试端口跑通了页面、条款和注册接口；
  - 正式 8092 服务还没启动；
  - Codex 计划先让 playground.cerebel.tech 显示一个"开发中"页面，用来验证 HTTPS。

## 4. 上线前的阻断问题

1. **GPU 服务器的出口 IP 在上海**（36.212.225.64）。美国用户的挥杆视频属于生物特征数据，存放在中国大陆的机器上有合规风险（美国司法部的数据安全规定、伊利诺伊州 BIPA 等）。建议迁到美国的 GPU 云后再开放上传。
2. **语言模型接口**：`MOTION_AGENT_BASE_URL` 目前默认是阿里云大陆的 DashScope，需要换成美国托管、兼容 OpenAI 接口的模型。
3. **条款**：是初稿，需要有人审阅。里面承诺了"首次付款 14 天内可退款"。
4. **root 密码**：用户决定不改。服务器目前用密码登录，建议之后改成只允许密钥登录。

## 5. 待办（按顺序）

1. 把本地 3 个提交推到 GitHub。
2. 完成名称服务器切换，并在 Cloudflare 重新开启 DNSSEC。
3. 决定服务器位置和语言模型供应商。这两件事解决之前，只开放"开发中"页面或仅限邀请的内测。
4. 在服务器上启动 `bash deploy/run-product.sh start`，设置 `CEREBEL_DEMO_SESSION_ID`，然后用新邮箱走一遍 `deploy/LAUNCH.md` 里的自测清单。
5. **用户最新需求**：重新设计 Playground 的 UI/UX，并调研可用的设计类技能（UI/UX、three.js 3D 界面等）。
6. 后续：教练后台、分析完成邮件通知、7 天试用实验、滑雪/单板内测（这个雪季，规模要小）。

## 6. 相关文档（claude.ai 上的 Claude Docs）

- 路线图：https://claude.ai/artifact/8nRCvofzKBm1VXmRESuANj
- 架构说明：https://claude.ai/artifact/DQGGHsHtt5Jh3CM4UvpqoE
- 上线计划：https://claude.ai/artifact/Qk7s6x6dPEhcXo2mH7BTdv
- YC 和 speedrun 申请草稿：https://claude.ai/artifact/V9TeTskxzZyumQGrHYyCSD
- 市场验证报告：`~/Developer/Claude outputs/CeRebel 市场验证.md`
