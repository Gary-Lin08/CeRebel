# CeRebel 公开测试版上线手册

## 这一版改了什么

| 改动 | 说明 |
|---|---|
| 安全补丁 0001 + 0002 | 操作员密码只认环境变量；匿名用户看不到别人的视频；登录 30 天过期；不再返回报错堆栈；跨域请求只放行白名单 |
| 默认打开消费者界面 | 首页就是消费者版；操作员调试界面改成 `/?dev=1`，Prompt 功能仍需密码 |
| 每月免费额度 | 免费每月 3 次分析，上传失败或识别不到挥杆的不计次；Pro 每月 60 次（合理使用上限） |
| Stripe 订阅 | 创始会员首年 $69（限 200 名）、$99.99/年，月付可选；用 Stripe 托管的结账页和订阅管理页；webhook 校验签名 |
| 注册同意 | 注册必须勾选同意条款、隐私政策和视频处理；可选填教练码，结账时写进 Stripe 元数据，方便以后算分成 |
| 条款和隐私页 | `/terms`、`/privacy`；公司名和联系邮箱从环境变量读取 |
| 埋点 | 服务端：注册、上传、分析完成、提问、触发付费墙、创建结账、订阅变化。前端：打开落地页、查看报告、"这和你的感觉一致吗"、点击升级 |
| 修复上传流程 | 之前网页上传的视频进不了 GPU 队列，处理完也不会识别是什么运动，现在都修好了 |
| 关闭内部端口 | 3D 预览（8090）和 Kinetic Lens（8091）默认只在服务器本机监听；`web_api.py` 默认只监听本机，修了路径穿越漏洞，关掉了列出所有会话的接口 |
| AI 教练 | 默认不把视频帧发给语言模型（`CEREBEL_LLM_ATTACH_VIDEO=0`）；客户端不能再切换模型后端 |
| 测试 | 新增 18 个测试，全部通过；其余测试的结果和改动前一样（1 个原有失败，另有 20 个依赖服务器数据，本地跑不了） |

## 第 1 步：把代码推到 GitHub

这个文件夹是从 GitHub 下载 ZIP 建的，要先接上远程仓库再推送。在终端运行：

```
cd ~/Developer/CeRebel
git remote add origin https://github.com/Gary-Lin08/CeRebel.git
git fetch --depth 1 origin main
git rebase --onto origin/main a919c58 main
git push origin main
```

如果想先审代码，把最后一行换成 `git push origin main:launch-beta`，再在 GitHub 上开 Pull Request。

## 第 2 步：服务器配置

1. 改掉 root 密码：运行 `passwd`。
2. 复制配置模板并填写：
   ```
   cd /root/CerebelServer
   cp deploy/env.product.example .env.product
   nano .env.product
   ```
   必填项：`CEREBEL_PUBLIC_URL`、4 个 Stripe 变量、`CEREBEL_LEGAL_ENTITY`、`CEREBEL_SUPPORT_EMAIL`、`MOTION_AGENT_PROMPT_PASSWORD`。
3. 启动：`bash deploy/run-product.sh start`。日志在 `product_server.log`。
4. GPU worker 交给 supervisor 管（GPU 在本机、没设 `CEREBEL_GPU_BACKEND=modal` 时必做）：
   ```
   cp deploy/supervisor/cerebel-gemx-worker.conf /etc/supervisor/conf.d/
   supervisorctl reread && supervisorctl update
   ```
   不做这一步，机器一重启 worker 就没了，所有上传都会排队 10 分钟后失败（2026-10-24 的 3 次 `pipeline_error` 就是这样）。
   模型加载约 2 分钟，之后 `curl -s http://127.0.0.1:8092/api/health` 里 `gpu.state` 应为 `ready`。

## 第 3 步：HTTPS 公网地址

网页服务只监听服务器本机的 8092 端口，要通过 HTTPS 隧道对外。最快的办法是 Cloudflare Tunnel：

- 临时测试：运行 `cloudflared tunnel --url http://127.0.0.1:8092`，会生成一个随机的 https 地址。
- 正式上线：用你们的域名建一个具名隧道，比如 app.cerebel.tech，再把这个地址填进 `CEREBEL_PUBLIC_URL`。

## 第 4 步：Stripe 正式模式

以下都在 Stripe 后台的正式模式里操作：

1. 新建产品 CeRebel Pro，价格 $99.99/年，月付 $14.99 可选。把价格 ID 填进 `STRIPE_PRICE_ANNUAL`；如果开了月付，把它的 ID 填进 `STRIPE_PRICE_MONTHLY`。
2. 新建优惠券：首年减 $30.99、只用一次。把优惠券 ID 填进 `STRIPE_FOUNDING_COUPON`。
3. 进 Developers → Webhooks，新增端点 `https://<你的域名>/api/stripe/webhook`，勾选下面 4 个事件，再把签名密钥填进 `STRIPE_WEBHOOK_SECRET`：
   - `checkout.session.completed`
   - `customer.subscription.created`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
4. 进 Settings → Billing → Customer portal，开启取消订阅和更新银行卡。
5. 把正式的 Secret key 填进 `STRIPE_SECRET_KEY`。这个密钥只放在服务器上，不要发到任何聊天里。
6. 运行 `bash deploy/run-product.sh restart`。

## 第 5 步：自动部署（可选）

按 `Claude outputs/cerebel-deploy/自动部署设置步骤.md` 操作，三个变量这样填：

| 变量 | 值 |
|---|---|
| `DEPLOY_PATH` | `/root/CerebelServer` |
| `RESTART_CMD` | `bash deploy/run-product.sh restart` |
| `HEALTH_CMD` | `curl -fsS http://127.0.0.1:8092/terms > /dev/null` |

## 第 6 步：上线前自测

用一个新邮箱把完整流程走一遍：

- [ ] 首页能打开，点 "Try the sample" 能看到示例报告
- [ ] 不勾选同意就无法注册；勾选后注册成功
- [ ] `/api/health` 返回 200，`gpu.state` 是 `ready`
- [ ] 上传一段侧面拍的挥杆，几分钟内出报告，额度显示剩 2 次
- [ ] 上传一段不是挥杆的视频，提示没识别到挥杆，额度不减少
- [ ] 3 次用完后再上传，会弹出升级窗口
- [ ] 用真实银行卡买创始会员，回到网站后显示 Pro（测完在 Stripe 后台退款）
- [ ] 点 "Manage subscription" 能打开 Stripe 页面并取消订阅
- [ ] 换另一个账号登录，看不到上一个账号的视频
- [ ] `/terms` 和 `/privacy` 里的公司名和邮箱正确
- [ ] 服务器上 `data/product/events.jsonl` 里能看到埋点记录

## 上线前必须解决（改代码解决不了）

1. **语言模型接口**：`MOTION_AGENT_BASE_URL` 默认是阿里云大陆的接口，要换成美国托管、兼容 OpenAI 接口的模型。
2. **服务器位置**：如果 GPU 服务器在中国大陆，用户的视频就存在那里。尽快确认，必要时迁到美国。
3. **条款**：`/terms` 和 `/privacy` 是初稿，上线前请律师或懂行的人看一遍。条款里承诺了"首次付款 14 天内可退款"，你们要确保做得到。
4. **端口**：确认服务器防火墙和云平台的端口映射没有把 8090、8091、61016 暴露到公网。61016 是 iOS 采集用的 gRPC 端口，目前没加密也没认证。

## 这一版还没做

- 上传前的拍摄指引图示，和上传后的自动画质检查（目前只检查时长和格式）
- 分析完成后的邮件通知
- 教练后台
- 7 天试用（上线时先不开）
