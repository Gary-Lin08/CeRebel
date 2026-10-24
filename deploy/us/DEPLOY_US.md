# CeRebel 美国服务器上线手册（DigitalOcean 纽约）

这份手册给创始人自己操作。所有账号都由你本人注册，所有密钥都由你本人填写。不要把任何密钥、密码或 token 发到聊天里，也不要提交到 GitHub。

## 0. 整体结构

| 部分 | 放在哪里 | 做什么 |
|---|---|---|
| 网页和 API（playground.cerebel.tech） | DigitalOcean 纽约的一台服务器（4 核 / 8 GB，Ubuntu 24.04） | 登录、上传、报告、AI 教练、Stripe 付款 |
| 3D 人体重建（GEM-X，需要 GPU） | Modal，限定在美国机房 | 每段视频按秒计费，没有任务时不花钱 |
| 后处理（OpenSim 关节角度、估算负荷、运动识别） | 同一台纽约服务器，用 CPU 跑 | 不需要 GPU |
| AI 教练的语言模型 | Together AI（美国） | 回答用户问题 |
| 付款 | Stripe | 不变 |

全程只用美国的服务，和隐私政策里"只在美国存储和处理"的说法一致。

需要你准备的账号：DigitalOcean、Modal（已有）、Together AI、Stripe（已有）、Cloudflare 或你现在管理域名的地方、GitHub（已有）。

---

## 第 1 步：Modal 改成只在美国运行（上线前必须做）

`deploy/modal_gemx.py` 已经改成 GPU 只在美国机房运行（`region="us"`）。要重新部署一次才生效，旧的部署可能跑在别的国家。

在 Mac 的终端里运行：

```
cd ~/Developer/CeRebel
bash deploy/modal_setup.sh
```

（或者把代码推到 GitHub，让 `modal-deploy` 自动部署，见第 8 步。）

费用：限定美国机房后，Modal 的 GPU 价格是基础价的 1.15 倍。L40S 基础价约 $0.000542/秒，也就是约 $1.95/小时；乘 1.15 后约 $2.24/小时。按一段视频用 1–3 分钟 GPU 估算（含冷启动），GPU 费用约 $0.04–0.11，另有少量 CPU 和内存费用。实际用量以 Modal 后台的账单为准。

如果部署时报错说你的套餐不能选机房，去 Modal 后台看一下套餐。Modal 现在的价格页写着所有套餐都能选机房；如果仍然报错，就升级到 Team 套餐或联系 Modal 客服开通。

---

## 第 2 步：注册 DigitalOcean 并创建服务器

### 2.1 先在 Mac 上准备 SSH 钥匙（只做一次）

打开"终端"，运行：

```
ls ~/.ssh/id_ed25519.pub || ssh-keygen -t ed25519 -C "gary-mac"
cat ~/.ssh/id_ed25519.pub
```

`ssh-keygen` 问问题时一路按回车。第二行会打印出一行以 `ssh-ed25519` 开头的文字，这是公钥，下一步要粘贴到 DigitalOcean。公钥可以公开，私钥（没有 `.pub` 的那个文件）绝不能给任何人。

### 2.2 注册并创建服务器（Droplet）

1. 打开 digitalocean.com 注册，绑定信用卡，建议开启两步验证（Settings → Security）。
2. 点 **Create → Droplets**，按下面选：
   - Region：**New York**（NYC1 或 NYC3 都可以）
   - Image：**Ubuntu 24.04 (LTS) x64**
   - Size：**Basic → Regular → 4 vCPU / 8 GB / 160 GB SSD**（约 $48/月，以页面显示为准）
   - Authentication：选 **SSH Key → New SSH Key**，把 2.1 打印的那行公钥粘进去，名字写 `gary-mac`
   - Hostname：`cerebel-web-nyc`
   - **不要勾选 Backups（自动备份）**。DigitalOcean 的备份会保留好几周，而隐私政策写的是备份只保留 14 天。我们的安装脚本会自己做 14 天的每日备份。以后如果要做快照（Snapshot），用完 14 天内要删掉。
   - Monitoring 可以勾选（免费）。
3. 点 **Create Droplet**。一分钟后会看到服务器的 IP 地址（比如 `164.90.x.x`），记下来，下面写作 `<服务器IP>`。

### 2.3 第一次登录试一下

```
ssh root@<服务器IP>
```

第一次会问 "Are you sure you want to continue connecting"，输入 `yes`。看到 `root@cerebel-web-nyc:~#` 就成功了。输入 `exit` 退出。

---

## 第 3 步：把代码放上去并运行安装脚本

有两种办法，选一种就行。**推荐办法 A。**

### 办法 A：从 Mac 直接复制代码（推荐）

在 Mac 终端运行（整段复制，`<服务器IP>` 换成你的 IP）：

```
rsync -az --exclude .git --exclude data/ --exclude '.env*' --exclude '__pycache__' \
  --exclude .venv --exclude .venv-ik --exclude kinetic-lens/node_modules \
  --exclude kinetic-lens/retargeting_neck_v2/outputs --exclude .DS_Store --exclude '*.log' \
  ~/Developer/CeRebel/ root@<服务器IP>:/opt/cerebel/

ssh root@<服务器IP> bash /opt/cerebel/deploy/us/setup-droplet.sh
```

### 办法 B：服务器从 GitHub 拉代码（只读部署钥匙）

先把所有改动提交并推送到 GitHub（包括 `deploy/us/`、`motion_agent/app/retention.py`、`motion_agent/app/consent.py`）。然后：

```
scp ~/Developer/CeRebel/deploy/us/setup-droplet.sh root@<服务器IP>:
ssh root@<服务器IP> CODE_SOURCE=git bash setup-droplet.sh
```

第一次运行会打印一行以 `ssh-ed25519` 开头的公钥然后停下。打开 GitHub → Gary-Lin08/CeRebel → Settings → Deploy keys → Add deploy key，粘贴进去，**不要勾选 Allow write access**，保存。然后再运行一次同样的 `ssh ... CODE_SOURCE=git bash setup-droplet.sh`。

### 安装脚本做了什么

大约 20–30 分钟，可以重复运行，不会重复安装：

- 建一个专门运行网站的用户 `cerebel`（网站不用 root 运行）
- 安装 ffmpeg、git、Caddy（自动申请 HTTPS 证书的网页服务器）、Miniforge（Python 环境）
- 建 Python 3.10 环境，装 OpenSim 4.5.2 和 `deploy/us/requirements-web.txt` 里的包
- 从 GitHub 下载 GEM-X 的 SOMA 人体模型代码和资源，和 OpenSim 的骨骼模型文件
- 安装网站服务 `cerebel-web`，开机自动启动，崩溃自动重启
- 配置 Caddy：playground.cerebel.tech，自动 HTTPS，上传上限 200 MB，加安全响应头
- 防火墙只开 22（SSH）、80、443
- 每天 07:41（UTC）运行数据保留任务：原始视频处理后 30 天删除，24 个月不活跃的账户整体删除（隐私政策的承诺）
- 每天 08:17（UTC）备份账户和订阅数据到 `/var/backups/cerebel`，只保留 14 天
- 自动安装安全更新，需要时在 09:00 UTC（纽约凌晨 4–5 点）自动重启

最后会打印自检结果。第一次运行时 `.env.product` 还没填，个别检查失败是正常的，填完第 4 步再看。

---

## 第 4 步：填写 .env.product（只在服务器上）

```
ssh root@<服务器IP>
nano /opt/cerebel/.env.product
```

在 nano 里用方向键移动，直接改等号后面的内容。改完按 `Ctrl+O`、回车保存，再按 `Ctrl+X` 退出。值里有空格或 `<` `>` 时要用单引号括起来，比如 `CEREBEL_LEGAL_ENTITY='Cerebel, Inc.'`。

| 变量 | 填什么 | 从哪里拿 |
|---|---|---|
| `CEREBEL_GPU_BACKEND` | `modal`（脚本已填好） | — |
| `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` | 给这台服务器单独建的 token | modal.com → Settings → API Tokens → New Token |
| `CEREBEL_PUBLIC_URL` | `https://playground.cerebel.tech`（脚本已填好） | — |
| `MOTION_AGENT_BASE_URL` | `https://api.together.xyz/v1`（脚本已填好） | — |
| `MOTION_AGENT_API_KEY` | Together 的 API key | api.together.ai → Settings → API Keys |
| `MOTION_AGENT_MODEL` | Together 上 Qwen 模型的完整 ID | Together → Models，搜 Qwen，复制 Model ID |
| `MOTION_AGENT_KB_EMBED_MODEL` | Together 上的一个 Embedding 模型 ID | Together → Models，筛选 Embeddings |
| `STRIPE_SECRET_KEY` | 正式模式的 Secret key（`sk_live_` 开头） | Stripe → Developers → API keys |
| `STRIPE_WEBHOOK_SECRET` | webhook 签名密钥（`whsec_` 开头） | Stripe → Developers → Webhooks → 你的端点 → Signing secret |
| `STRIPE_PRICE_ANNUAL` / `STRIPE_PRICE_MONTHLY` | 价格 ID（`price_` 开头） | Stripe → Products → CeRebel Pro |
| `STRIPE_FOUNDING_COUPON` | 创始会员优惠券 ID | Stripe → Products → Coupons |
| `CEREBEL_RAW_VIDEO_RETENTION_DAYS` | `30` | 隐私政策：原始视频处理后 30 天删除 |
| `CEREBEL_INACTIVE_DELETE_DAYS` | `730` | 隐私政策：24 个月不活跃删除账户 |
| `CEREBEL_LEGAL_ENTITY` | `'Cerebel, Inc.'` | — |
| `CEREBEL_SUPPORT_EMAIL` | `garylin@cerebel.tech` | — |
| `MOTION_AGENT_PROMPT_PASSWORD` | 一串很长的随机字符 | Mac 终端运行 `openssl rand -base64 32` |
| `CEREBEL_SMTP_*` | SpaceMail 的发信设置 | SpaceMail 后台 |
| `CEREBEL_DEMO_SESSION_ID` | 示例报告的 session ID | 见第 7 步 |

旧服务器上的 Stripe 正式密钥和 webhook 签名密钥，可以在 Stripe 后台重新查看后再填，不需要从旧服务器复制文件。

保存后重启并查看日志：

```
systemctl restart cerebel-web
journalctl -u cerebel-web -n 50 --no-pager
```

（可选）提前建好 AI 教练的知识库索引，这样第一个问题不用等：

```
sudo -u cerebel bash /opt/cerebel/deploy/us/start-web.sh /opt/cerebel/motion_agent/scripts/rebuild_knowledge_index.py
```

---

## 第 5 步：把域名指向新服务器（DNS）

现在 `playground.cerebel.tech` 是一条 CNAME，指向 Cloudflare 隧道，隧道连到上海的旧服务器。要改成指向纽约服务器。

**先确认 DNS 实际在哪里管理。** 交接文档说名称服务器还没切到 Cloudflare。在 Mac 终端运行 `dig NS cerebel.tech +short`：

- 结果里有 `cloudflare.com`：在 Cloudflare 里改。
- 结果是 Spaceship 的服务器：在 Spaceship 的 DNS 设置里改（步骤一样，只是没有"小云朵"这个开关）。

### 推荐：方案 A，A 记录直接指向服务器

1. 删掉 `playground` 那条 CNAME 记录。
2. 新建一条记录：类型 `A`，名称 `playground`，内容 `<服务器IP>`，TTL 选 Auto。
3. 在 Cloudflare 里，代理状态点成**灰色云朵（DNS only）**，不要用橙色。原因：
   - Cloudflare 免费版的代理最多只能上传 100 MB，4K 60 帧的 30 秒视频会超过；
   - 服务器上的 Caddy 要直接和 Let's Encrypt 通信才能自动申请 HTTPS 证书。
4. 等几分钟，在 Mac 上运行 `dig +short playground.cerebel.tech`，看到 `<服务器IP>` 就生效了。Caddy 会在第一次访问时自动申请证书，大约 1 分钟。
5. 关掉旧隧道：Cloudflare → Zero Trust → Networks → Tunnels → "CeRebel Playground" → Public Hostnames，删掉 playground.cerebel.tech。然后在上海服务器上停掉 cloudflared 和旧的 8092 服务（`bash deploy/run-product.sh stop`）。

### 备选：方案 B，在纽约服务器上跑 Cloudflare 隧道

适合不想公开服务器 IP 的情况。缺点：上传同样受 100 MB 限制，排查问题也更麻烦。

1. 名称服务器必须已经切到 Cloudflare。
2. Cloudflare → Zero Trust → Networks → Tunnels → Create a tunnel，名字写 `cerebel-nyc`，按页面提示在服务器上安装 cloudflared（复制它给的 `sudo cloudflared service install ...` 那一行到服务器运行）。
3. Public Hostname：`playground.cerebel.tech` → `HTTP` → `127.0.0.1:8092`。
4. 删掉旧隧道 "CeRebel Playground" 里的同名路由。
5. 这种方案下 Caddy 用不上，可以运行 `systemctl disable --now caddy`，再用 `ufw delete allow 80/tcp` 和 `ufw delete allow 443` 关掉 80 和 443。

---

## 第 6 步：Stripe webhook

如果域名还是 `playground.cerebel.tech`，**webhook 地址不用改**：仍然是 `https://playground.cerebel.tech/api/stripe/webhook`，签名密钥也不变。

切换 DNS 之后：Stripe → Developers → Webhooks → 点这个端点 → **Send test event**（选 `customer.subscription.updated`），看到 200 就说明新服务器收到了。

只有换了域名，才需要新增端点。新端点要勾选这 4 个事件：`checkout.session.completed`、`customer.subscription.created`、`customer.subscription.updated`、`customer.subscription.deleted`。然后把新的签名密钥填进 `STRIPE_WEBHOOK_SECRET`，重启 `cerebel-web`，最后删除旧端点。

---

## 第 7 步：从上海服务器搬数据（如果需要）

模型权重和代码都不用搬：GPU 权重在 Modal 上，代码和 OpenSim 模型都在仓库里，SOMA 资源从 GitHub 下载。只有下面这些数据存在上海服务器上：

| 上海服务器路径 | 是什么 | 要不要搬 |
|---|---|---|
| `/root/CerebelServer/data/product/` | 账户、登录、订阅状态、额度、埋点（accounts.json、tokens.json、billing.json、library.json、question_counts.json、events.jsonl） | 已经有真实用户或付费用户就要搬；还没开放就不用 |
| `/root/CerebelServer/data/sessions/<示例session>/` | 示例报告（`CEREBEL_DEMO_SESSION_ID`）的数据 | 要搬，否则 "Try the sample" 打不开 |
| `/root/GEM-X/outputs/cerebel/<示例session>/` | 示例报告的 GEM-X 结果（至少要 `hpe_results.pt`） | 要搬，放到新服务器的 `/opt/cerebel/data/gemx_outputs/<示例session>/` |
| `/root/CerebelServer/kinetic-lens/retargeting_neck_v2/outputs/<示例session>/` | 颈部 IK 的缓存 | 可选 |
| `/root/CerebelServer/data/sessions/` 里的其他文件夹 | 测试上传 | 一般不用搬 |
| `/root/CerebelServer/.env.product` | 旧配置，里面有密钥 | 不要复制文件，按第 4 步重新填 |

搬的办法：先从上海服务器下载到 Mac，再从 Mac 上传到纽约服务器（用你平时连上海服务器的方式下载）。上传示例：

```
rsync -az ~/cerebel-migrate/product/ root@<服务器IP>:/opt/cerebel/data/product/
rsync -az ~/cerebel-migrate/sessions/<示例session>/ root@<服务器IP>:/opt/cerebel/data/sessions/<示例session>/
rsync -az ~/cerebel-migrate/gemx/<示例session>/ root@<服务器IP>:/opt/cerebel/data/gemx_outputs/<示例session>/
ssh root@<服务器IP> "chown -R cerebel:cerebel /opt/cerebel/data && systemctl restart cerebel-web"
```

示例 session 的 `gemx.json` 里写的是上海的旧路径（`/root/GEM-X/...`）。如果搬过来后示例报告打不开，告诉开发助手，让它改成新路径。

搬完之后删掉 Mac 上的临时副本（`~/cerebel-migrate`）。

---

## 第 8 步：GitHub 自动部署（可选，推荐）

### 8.1 网站：推送到 main 后自动更新纽约服务器（`.github/workflows/deploy.yml`）

1. 在 Mac 上给 GitHub 单独建一对钥匙：
   ```
   ssh-keygen -t ed25519 -f ~/.ssh/cerebel_actions -N "" -C "github-actions"
   ssh-keyscan <服务器IP>
   ```
2. 把公钥放到服务器的 cerebel 用户下：
   ```
   cat ~/.ssh/cerebel_actions.pub | ssh root@<服务器IP> "cat >> /home/cerebel/.ssh/authorized_keys"
   ```
3. GitHub → 仓库 → Settings → Secrets and variables → Actions → **Secrets**，新建或修改：
   - `SSH_HOST` = `<服务器IP>`
   - `SSH_PORT` = `22`
   - `SSH_USER` = `cerebel`
   - `SSH_PRIVATE_KEY` = `cat ~/.ssh/cerebel_actions` 的全部内容（包括 BEGIN/END 两行）
   - `SSH_KNOWN_HOSTS` = 第 1 步 `ssh-keyscan` 打印的全部内容
4. 同一页的 **Variables**：如果以前为上海服务器设置过 `DEPLOY_PATH`、`RESTART_CMD`、`HEALTH_CMD`，删掉它们（会自动用纽约服务器的默认值），或者改成：
   - `DEPLOY_PATH` = `/opt/cerebel`
   - `RESTART_CMD` = `sudo systemctl restart cerebel-web`
   - `HEALTH_CMD` = `curl -fsS http://127.0.0.1:8092/terms > /dev/null`

注意：
- 自动部署只更新代码，不安装新的 Python 包。`deploy/us/requirements-web.txt` 改了之后，要在服务器上用 root 重新运行一次 `bash /opt/cerebel/deploy/us/setup-droplet.sh`。
- 如果第 3 步用的是办法 B（git），开了自动部署之后就不要再在服务器上 `git pull`。

### 8.2 GPU 函数：改了 Modal 代码后自动部署（`.github/workflows/modal-deploy.yml`）

1. modal.com → Settings → API Tokens → New Token，**专门给 GitHub 建一个**，不要和服务器共用。
2. GitHub Secrets 新建 `MODAL_TOKEN_ID` 和 `MODAL_TOKEN_SECRET`。
3. 之后只要 `deploy/modal_gemx.py` 或 `deploy/gemx_overlay/` 有改动推到 main，就会自动部署。也可以在 Actions → modal-deploy → Run workflow 手动运行。
4. 可选的 Variables：`CEREBEL_MODAL_GPU`（默认 L40S）、`CEREBEL_MODAL_MAX_CONTAINERS`（默认 10）、`CEREBEL_MODAL_REGION`（默认 `us`，只能填美国机房，填别的会报错）。

---

## 第 9 步：上线前自测清单

在服务器上：

- [ ] `systemctl is-active cerebel-web caddy` 两个都显示 `active`
- [ ] `systemctl list-timers | grep cerebel` 能看到 `cerebel-retention.timer` 和 `cerebel-backup.timer`
- [ ] `sudo -u cerebel bash /opt/cerebel/deploy/us/start-web.sh -m motion_agent.app.retention --dry-run` 正常结束，打印 "retention would delete: ..."
- [ ] `systemctl start cerebel-backup && ls -l /var/backups/cerebel` 能看到当天的备份文件
- [ ] `ufw status` 只放行 22、80、443
- [ ] `journalctl -u cerebel-web -n 100 --no-pager` 里没有报错

在浏览器里（用一个新邮箱）：

- [ ] `https://playground.cerebel.tech` 能打开，地址栏有锁，证书签发方是 Let's Encrypt
- [ ] 点 "Try the sample" 能看到示例报告
- [ ] `/terms` 和 `/privacy` 里的公司名、邮箱正确，隐私政策写的是"30 天删除原始视频"和"24 个月"
- [ ] 不勾选同意就无法注册；勾选后注册成功
- [ ] 上传一段侧面拍的挥杆。日志里能看到 `(modal)` 字样，几分钟内出报告，有关节角度和负荷
- [ ] Modal 后台 → Apps → cerebel-gemx 能看到这次调用，而且这次部署是第 1 步之后做的（限定美国机房）
- [ ] 问 AI 教练一个问题，几秒内开始回答
- [ ] 上传一段不是挥杆的视频，提示没识别到挥杆，额度不减少
- [ ] 3 次用完后再上传，会弹出升级窗口
- [ ] 用真实银行卡买创始会员，回到网站显示 Pro（测完在 Stripe 后台退款）
- [ ] "Manage subscription" 能打开 Stripe 页面
- [ ] Stripe webhook 的 Send test event 返回 200
- [ ] 换一个账号登录，看不到上一个账号的视频
- [ ] 收到"分析完成"邮件（如果配置了 SMTP）
- [ ] 在 Library 里删除一个挥杆、删除测试账号，都能成功

---

## 日常维护速查

| 想做什么 | 命令（先 `ssh root@<服务器IP>`） |
|---|---|
| 看网站日志 | `journalctl -u cerebel-web -f`（`Ctrl+C` 退出） |
| 重启网站 | `systemctl restart cerebel-web` |
| 改配置 | `nano /opt/cerebel/.env.product`，然后重启网站 |
| 看数据删除记录 | `journalctl -u cerebel-retention -n 50` |
| 手动备份 | `systemctl start cerebel-backup` |
| 看备份 | `ls -l /var/backups/cerebel` |
| 更新系统和 Python 包 | `bash /opt/cerebel/deploy/us/setup-droplet.sh` |
| 看 HTTPS 证书和访问日志 | `journalctl -u caddy -n 50`、`tail /var/log/caddy/cerebel-access.log` |

上线后不要把 root 密码登录打开。DigitalOcean 默认只允许用 SSH 钥匙登录，保持这样就好。
