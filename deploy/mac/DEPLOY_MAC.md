# CeRebel 上线手册：波士顿 Mac mini + Cloudflare Tunnel

这份手册给创始人自己操作。账号由你本人登录，密钥由你本人填写。不要把任何密钥、密码或 token 发到聊天里，也不要提交到 GitHub。

## 0. 整体结构

| 部分 | 放在哪里 | 做什么 |
|---|---|---|
| 网页和 API（playground.cerebel.tech） | 波士顿的 Mac mini，只监听本机 8092 端口 | 登录、上传、报告、AI 教练、Stripe 付款 |
| 对外的 HTTPS 地址 | Cloudflare Tunnel（免费），Mac mini 主动连出去 | 不用开路由器端口，不用固定 IP |
| 3D 人体重建（GEM-X，需要 GPU） | Modal，限定美国机房 | 已部署（`cerebel-gemx`），没有任务时不花钱 |
| 后处理（OpenSim、负荷估算、运动识别） | 同一台 Mac mini，用 CPU 跑 | 不需要 GPU |
| AI 教练的语言模型 | Together AI（美国） | 回答用户问题 |
| 付款 | Stripe | 不变 |

代价：Mac mini 断电、断网或睡眠时网站打不开。用户多了再搬到云服务器（`deploy/us/`）。

上传上限是 95 MB：Cloudflare 免费版最多只能转发 100 MB 的请求。

---

## 第 1 步：Mac mini 基础设置（只做一次）

1. 用一个会一直登录的 macOS 用户操作（网站以这个用户的身份运行，不用 root）。
2. 系统设置 → 用户与群组 → **自动登录** 选这个用户，这样停电重启后网站能自己起来。如果开了 FileVault，系统不允许自动登录，停电后需要有人输一次密码。
3. 打开"终端"，安装 Xcode 命令行工具和 Homebrew（会问你的 Mac 密码）：
   ```
   xcode-select --install
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   ```
   Homebrew 装完会提示运行两行 `eval ...` 命令，照着运行。

## 第 2 步：不让 Mac mini 睡眠

```
sudo pmset -a sleep 0 disksleep 0 autorestart 1
```

意思是：从不睡眠，硬盘不休眠，停电恢复后自动开机。显示器可以照常关闭，不影响网站。

## 第 3 步：下载代码并运行安装脚本

```
mkdir -p ~/Developer && cd ~/Developer
git clone https://github.com/Gary-Lin08/CeRebel.git
cd CeRebel
bash deploy/mac/setup-mac.sh
```

- `git clone` 是私有仓库，会要你登录 GitHub。最简单的办法是先 `brew install gh`，再 `gh auth login`，按提示在浏览器里登录。
- 安装脚本大约 15–30 分钟，可以重复运行。它会：装 ffmpeg、git-lfs、cloudflared；装 Python 3.10 + OpenSim 4.5.2 环境（`~/miniforge3`）；下载 SOMA 人体模型和 OpenSim 骨骼模型（`~/cerebel`）；建好 `.env.product`；装三个开机自动运行的后台任务：
  - `tech.cerebel.web`：网站，崩溃会自动重启
  - `tech.cerebel.retention`：每天 03:41 删除过期视频和不活跃账户（隐私政策的承诺）
  - `tech.cerebel.backup`：每天 04:17 备份账户和订阅数据到 `~/cerebel/backups`，只保留 14 天
- 最后会打印自检结果。`.env.product` 还没填时，网站会起不来，这是正常的，填完第 4 步再看。

## 第 4 步：填写 .env.product（只在 Mac mini 上）

```
open -e ~/Developer/CeRebel/.env.product
```

用"文本编辑"改等号后面的内容，保存。值里有空格或 `<` `>` 时要用单引号括起来，比如 `CEREBEL_LEGAL_ENTITY='Cerebel, Inc.'`。

| 变量 | 填什么 | 从哪里拿 |
|---|---|---|
| `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` | 给这台 Mac mini 单独建的 token | modal.com → Settings → API Tokens → New Token |
| `MOTION_AGENT_API_KEY` | Together 的 API key | api.together.ai → Settings → API Keys |
| `MOTION_AGENT_MODEL` | Together 上 Qwen 模型的完整 ID | Together → Models，搜 Qwen |
| `MOTION_AGENT_KB_EMBED_MODEL` | Together 上的一个 Embedding 模型 ID | Together → Models，筛选 Embeddings |
| `STRIPE_SECRET_KEY` | 正式模式的 Secret key（`sk_live_` 开头） | Stripe → Developers → API keys |
| `STRIPE_WEBHOOK_SECRET` | webhook 签名密钥（`whsec_` 开头） | Stripe → Developers → Webhooks → 端点 → Signing secret |
| `STRIPE_PRICE_ANNUAL` / `STRIPE_PRICE_MONTHLY` | 价格 ID（`price_` 开头） | Stripe → Products → CeRebel Pro |
| `STRIPE_FOUNDING_COUPON` | 创始会员优惠券 ID | Stripe → Products → Coupons |
| `CEREBEL_SUPPORT_EMAIL` | `garylin@cerebel.tech` | — |
| `MOTION_AGENT_PROMPT_PASSWORD` | 一串很长的随机字符 | 终端运行 `openssl rand -base64 32` |
| `CEREBEL_SMTP_*` | SpaceMail 的发信设置 | SpaceMail 后台 |
| `CEREBEL_DEMO_SESSION_ID` | 示例报告的 session ID | 示例数据搬过来后填 |

`CEREBEL_PUBLIC_URL`、`CEREBEL_GPU_BACKEND=modal`、`MOTION_AGENT_BASE_URL`、上传上限，脚本已经填好。

保存后重启网站并看日志：

```
launchctl kickstart -k gui/$UID/tech.cerebel.web
tail -50 ~/cerebel/logs/web.log
curl -s http://127.0.0.1:8092/terms | head -5
```

## 第 5 步：Cloudflare Tunnel 和 DNS

### 5.1 把域名的名称服务器换到 Cloudflare

1. 登录 Cloudflare，确认 `cerebel.tech` 这个站点里已经有这些记录（Codex 之前建好了），没有就补上：主站和 `www` 指向 Vercel（灰色云朵，仅 DNS）、SpaceMail 的 MX、SPF、DKIM。**换之前一定要核对**，否则官网和邮箱会断。
2. Cloudflare 会给出两个名称服务器地址（类似 `xxx.ns.cloudflare.com`）。
3. 登录 Spaceship → 域名 `cerebel.tech` → Nameservers，改成 Cloudflare 给的那两个。DNSSEC 之前已经关掉了。
4. 通常几分钟到几小时生效。在终端运行 `dig NS cerebel.tech +short`，看到 `cloudflare.com` 就好了。之后在 Cloudflare 重新开启 DNSSEC。

### 5.2 建隧道，连到 Mac mini

1. Cloudflare → Zero Trust → Networks → Tunnels → **Create a tunnel** → Cloudflared，名字写 `cerebel-macmini`。
2. 选 macOS，页面会给一行 `sudo cloudflared service install eyJ...`。在 Mac mini 的终端里运行这一行。它包含隧道的密钥，不要发给任何人。
3. Public Hostname：子域名 `playground`，域名 `cerebel.tech`，Type 选 `HTTP`，URL 填 `localhost:8092`。
4. 旧隧道 "CeRebel Playground" 连的是上海的服务器：进去删掉它的 playground.cerebel.tech 路由，或者整个删掉这条隧道。
5. 在任意电脑打开 `https://playground.cerebel.tech/terms`，能看到条款页就通了。

## 第 6 步：Stripe webhook

域名没变，所以 webhook 地址不用改：`https://playground.cerebel.tech/api/stripe/webhook`。

隧道通了以后：Stripe → Developers → Webhooks → 点这个端点 → **Send test event**（选 `customer.subscription.updated`），看到 200 就说明 Mac mini 收到了。

## 第 7 步：上线前自测

在 Mac mini 上：

- [ ] `launchctl print gui/$UID/tech.cerebel.web | grep state` 显示 `running`
- [ ] `launchctl list | grep cerebel` 能看到 web、retention、backup 三个任务
- [ ] `launchctl kickstart gui/$UID/tech.cerebel.backup` 之后 `ls ~/cerebel/backups` 有当天的备份
- [ ] `bash deploy/mac/start-mac.sh -m motion_agent.app.retention --dry-run` 正常结束
- [ ] `tail -100 ~/cerebel/logs/web.log` 没有报错

在浏览器里（用一个新邮箱）：

- [ ] `https://playground.cerebel.tech` 能打开，地址栏有锁
- [ ] `/terms` 和 `/privacy` 里的公司名、邮箱正确
- [ ] 不勾选同意就无法注册；勾选后注册成功
- [ ] 上传一段侧面拍的挥杆，几分钟内出报告，有关节角度和负荷
- [ ] 问 AI 教练一个问题，几秒内开始回答
- [ ] 上传一段不是挥杆的视频，提示没识别到挥杆，额度不减少
- [ ] 用真实银行卡买创始会员，回到网站显示 Pro（测完在 Stripe 后台退款）
- [ ] "Manage subscription" 能打开 Stripe 页面
- [ ] 换一个账号登录，看不到上一个账号的视频
- [ ] 在 Library 里删除一个挥杆、删除测试账号，都能成功

## 日常维护速查

| 想做什么 | 命令（在 Mac mini 终端里） |
|---|---|
| 更新到最新代码并重启 | `bash ~/Developer/CeRebel/deploy/mac/update-mac.sh` |
| 依赖或安装脚本改了 | `bash ~/Developer/CeRebel/deploy/mac/setup-mac.sh` |
| 看网站日志 | `tail -f ~/cerebel/logs/web.log`（`Ctrl+C` 退出） |
| 重启网站 | `launchctl kickstart -k gui/$UID/tech.cerebel.web` |
| 停掉网站 | `launchctl bootout gui/$UID/tech.cerebel.web` |
| 改配置 | `open -e ~/Developer/CeRebel/.env.product`，然后重启网站 |
| 看数据删除和备份记录 | `tail ~/cerebel/logs/retention.log ~/cerebel/logs/backup.log` |
| 看隧道状态 | Cloudflare → Zero Trust → Networks → Tunnels |

推送到 GitHub 的 main 不会再自动部署（`.github/workflows/deploy.yml` 只能手动运行），Mac mini 用上面的 `update-mac.sh` 更新。
