# 开发者后台：部署记录

后台地址：`https://cerebel.tech/playground/admin`。

登录要同时满足：
1. 用 `CEREBEL_ADMIN_EMAILS` 里的邮箱，以普通用户身份登录（账号要先在网站上注册）；
2. 输入 `CEREBEL_ADMIN_PASSWORD`。

这两个环境变量只要有一个没设，`/admin` 就返回 404。

## 服务器上的目录

| 路径 | 作用 |
|---|---|
| `/root/CeRebel` | 线上产品代码，`cerebel-product` 和 `cerebel-retention` 从这里运行 |
| `/root/CerebelServer` | `.env.product`、共享数据 `data/`、GPU 进程 |

部署前先看 `/etc/supervisor/conf.d/cerebel-product.conf` 的 `directory=`，确认线上跑的是哪份代码。

## 新增的数据文件（在 `data/product/`，自动创建）

- `site_events.jsonl`：网站访问和点击，保留 `CEREBEL_SITE_EVENT_RETENTION_DAYS` 天（默认 90）。不存 IP 和完整 User-Agent。
- `backers.json`：只在没有配置 Supabase 时使用。
- `activity.json`、`questions.jsonl`、`password_resets.json`、`admin_audit.jsonl`。

## 预售名单（Supabase 项目 `cerebel-site`，美国东部）

主站直接调用公开函数 `join_backer_list` 写入，所以产品服务器停机不影响报名。表 `public.backers` 不允许直接读写。后台用 `CEREBEL_BACKERS_ADMIN_TOKEN` 调用 `admin_list_backers` 和 `admin_remove_backer`，数据库 `private.settings` 里只存这个令牌的 SHA-256。

## 主站接入

主站（`Gary-Lin08/cerebel-website`）通过 Vercel 的 `/playground` 转发上报：

- 访问和心跳默认发到 `/playground/api/site/ping`；
- backer 表单的生产构建默认发到 Supabase，`VITE_BACKER_ENDPOINT` 和 `VITE_BACKER_API_KEY` 可以覆盖。

## 已知限制

- 后台服务停机时，主站访问不计数，也打不开后台，但 backer 报名照常写入 Supabase。
- Supabase 免费项目长期没有访问可能被暂停，暂停期间报名会失败。后台每次打开都会访问它。
- 主站接入之前的 backer 报名在 hello@cerebel.tech 的收件箱里，不会自动进入名单。
- 匿名访客只有随机 ID，登录后才显示邮箱。同一个人换浏览器算两个访客。
- 「正在线上」、每分钟曲线、限流计数和后台登录状态都在内存里，重启后清零，管理员要重新登录后台。
- 提问原文只存 2026-10-26 及以后同意条款的账号，见 `questions.STORE_FROM_TERMS`。
