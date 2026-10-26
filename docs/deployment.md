# 部署

详细的一步步手册已经有了，这篇只讲全貌、怎么选、上线后怎么维护。

| 手册 | 环境 | 状态 |
|---|---|---|
| [`deploy/mac/DEPLOY_MAC.md`](../deploy/mac/DEPLOY_MAC.md) | 波士顿 Mac mini + Cloudflare Tunnel + Modal GPU + Together AI | 手册里写的"当前线上"方案 |
| [`deploy/us/DEPLOY_US.md`](../deploy/us/DEPLOY_US.md) | DigitalOcean 纽约服务器 + Caddy + Modal + Together AI | 用户多了以后迁移的目标 |
| [`deploy/LAUNCH.md`](../deploy/LAUNCH.md) | 旧 GPU 云服务器（`/root/CerebelServer`） | 早期公开测试版。服务器不在美国，不应再存放美国用户的视频 |

## 线上现状（2026-10-26）

- 对外入口是 `https://cerebel.tech/playground`：主站（Vercel）把 `/playground` 代理到应用服务器，所以服务端设了 `CEREBEL_BASE_PATH=/playground`，上传走 `CEREBEL_UPLOAD_ORIGIN`。`playground.cerebel.tech` 这个域名目前连不上。
- 上面三份手册写的环境不完全一致。**接手前先和负责人确认线上实际运行在哪台机器上、按哪份手册部署的**，再动服务器。
- GitHub Actions 的 `uptime.yml` 每 30 分钟检查一次页面和 GPU 状态，失败会发邮件。

## 组成和选择

| 部分 | 选项 | 说明 |
|---|---|---|
| 网页和 API | Mac mini / DigitalOcean / 任意 Linux | 只需要 CPU。4 核 8 GB 够用。只监听 127.0.0.1:8092，通过 HTTPS 隧道或反向代理对外 |
| HTTPS | Cloudflare Tunnel（不用开端口，免费版请求体上限 100 MB）/ Caddy（自动证书） | |
| GPU（GEM-X） | Modal（`CEREBEL_GPU_BACKEND=modal`，按秒计费，限定美国机房）/ 本机 NVIDIA GPU + warm worker | Modal 不需要自己维护驱动 |
| 后处理（OpenSim IK、动力学） | 和网页同一台机器，CPU | 需要 conda 装的 `opensim=4.5.2`（`numpy 1.25`），见 `deploy/us/requirements-web.txt` 开头的说明 |
| 语言模型 | Together AI / Fireworks 等美国托管、OpenAI 兼容的服务 | 默认值还是阿里云 DashScope，线上必须改 `MOTION_AGENT_BASE_URL` |
| 付款 | Stripe | 见 `LAUNCH.md` 第 4 步 |
| 邮件 | SpaceMail SMTP | 可选 |

**数据驻留**：同意文本和隐私政策告诉用户"只在美国处理和存储"（列出了 DigitalOcean、Modal、Together AI 等）。换服务商或机房时，同步改 `motion_agent/app/consent.py` 里的处理方列表和隐私页，并提升同意文本的版本号，让用户重新同意。

## GPU 侧

- **Modal**：`deploy/modal_gemx.py` 定义 `cerebel-gemx` 应用。改了它或 `deploy/gemx_overlay/` 后，推到 `main` 会由 `.github/workflows/modal-deploy.yml` 自动部署；第一次部署见 `deploy/modal_setup.sh`。
- **本机 GPU**：
  1. 按 `deploy/gemx_overlay/BASE_COMMIT` 检出 GEM-X 上游，打上 `gemx-local-changes.patch`，再用 `files/` 覆盖。
  2. 下载权重到 `$CEREBEL_GEMX_ROOT/inputs/pretrained/gem_soma.ckpt`，SOMA 资源到 `inputs/soma_assets`。
  3. 用 supervisor 托管 warm worker：`deploy/supervisor/cerebel-gemx-worker.conf`。不托管的话机器重启后 worker 就没了，所有上传排队后失败。
  4. 模型加载约 2 分钟，之后 `curl -s http://127.0.0.1:8092/api/health` 里 `gpu.state` 应为 `ready`。

## 上线前检查

用一个新邮箱把 [`deploy/LAUNCH.md` 第 6 步](../deploy/LAUNCH.md)的清单走一遍，另外：

- [ ] `.env.product` 里 `MOTION_AGENT_HOST=127.0.0.1`，`CEREBEL_OPERATOR_PORT` 已设置，公网访问 `/?dev=1` 看不到操作员功能
- [ ] `MOTION_AGENT_BASE_URL` 指向美国托管的模型，`CEREBEL_LLM_ATTACH_VIDEO=0`
- [ ] `CEREBEL_DEMO_SESSION_ID` 指向一个演示会话，且这个会话不属于任何账号
- [ ] 防火墙没有对外开放 8090、8091、8093、61016、61017
- [ ] 数据保留任务每天运行（Mac：`tech.cerebel.retention.plist`；Linux：`cerebel-retention.timer`；旧服务器：`deploy/supervisor/cerebel-retention.conf`）
- [ ] 备份每天运行，并且试过一次恢复
- [ ] SDK 的真服务器测试在**预发布环境**通过（不要对生产环境跑，它会注册测试账号）：

```bash
CEREBEL_TEST_BASE_URL=https://staging.example.com python -m unittest discover -s sdk/python/tests -t sdk/python
```

## 日常维护

| 想做什么 | Mac mini | DigitalOcean |
|---|---|---|
| 更新代码并重启 | `bash deploy/mac/update-mac.sh` | 推到 `main` 后手动运行 Actions 里的 `deploy`，或 `bash /opt/cerebel/deploy/us/setup-droplet.sh` |
| 看日志 | `tail -f ~/cerebel/logs/web.log` | `journalctl -u cerebel-web -f` |
| 重启 | `launchctl kickstart -k gui/$UID/tech.cerebel.web` | `systemctl restart cerebel-web` |
| 改配置 | 改 `.env.product` 后重启 | 改 `/opt/cerebel/.env.product` 后重启 |
| 数据保留和备份记录 | `tail ~/cerebel/logs/retention.log ~/cerebel/logs/backup.log` | `journalctl -u cerebel-retention -n 50` |

推到 `main` **不会**自动部署网页（`deploy.yml` 只能手动触发），只会自动部署 Modal 函数。

## 出问题时

| 现象 | 先看 |
|---|---|
| 上传全部失败，`failed_at` 是 `gpu_unavailable` | GPU worker 或 Modal。`/api/health` 的 `gpu.state` |
| 上传后一直停在 `queued` | 队列前面的任务卡住了；看网页日志里 GEM-X 的输出 |
| 报告出来了但 `failed_at` 是 `ik` | OpenSim 环境：`CEREBEL_IK_PYTHON` 能不能 `import opensim` |
| 教练回答报错 | `/api/health` 的 `model.credential`；日志里按 `error_id` 搜 |
| 用户说付了钱还是免费版 | Stripe 后台 webhook 的投递记录；`data/product/billing.json` |
