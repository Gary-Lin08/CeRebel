# 旧 GPU 服务器：补做隐私清理，并启用每日 retention

旧机（onethingai，`/root/CerebelServer`）上没有 cron 和 systemd，只有 supervisord，所以
`motion_agent.app.retention` 从来没有运行过。另外，2026-10 隐私修复之前删除的账号还留着
GEM-X 输出、库记录和埋点，所有 `hpe_results.pt` 也都没有脱敏。

本次新增：

| 文件 | 作用 |
|---|---|
| `motion_agent/app/retention_schedule.py` | 每日循环：到 `CEREBEL_RETENTION_AT`（UTC，默认 07:41）时，在子进程里跑一次 retention。开机后会补跑错过的那次；如果从没跑过，就等到当天的设定时间再跑。失败后 1 小时重试 |
| `deploy/supervisor/cerebel-retention.conf` | 用 supervisor 托管上面的循环 |
| `motion_agent/app/privacy_backfill.py` | 一次性清理：已删账号的残留数据，以及所有 `hpe_results.pt` 的补脱敏。默认只出报告，加 `--apply` 才会真正执行 |

这些都是新文件，不改动任何正在运行的模块，所以部署后不需要重启 cerebel-product。
所有输出只包含 id 和数量，不含邮箱。

## 2026-10-26 只读核实结果（UTC 18:12）

- 服务器代码与 d2f7c46 完全一致；没有 `data/product/retention.jsonl`，说明 retention 从未运行过。
- `retention --dry-run` 会处理：
  - 72 个会话的原始视频，共 5.55 GB，处理时间在 08-21 到 09-25 之间。这 72 个会话全都没有库记录，是旧版或内部采集的数据；派生结果会保留。
  - 1 个颈部残留目录：55FEE713。
  - 不会删除账号，也没有无主会话、孤儿 GEM-X 目录或过期埋点。
- `privacy_backfill`（只出报告）会处理：
  - 2 个已删账号 61371a68…、62342a0c…：55FEE713 的 GEM-X 目录和颈部目录、2 行库记录、8 行埋点；另有 2 行 `account_deleted` 会保留，但去掉其中的 user_id。
  - demo 会话 5032CEE6 不会被清理：已删用户问 demo 的那一行会删掉，匿名访客的提问会保留。
  - 共 70 个 `hpe_results.pt`，都带 net_outputs，都没有脱敏，全部需要处理。

## 要在服务器上执行的命令（需要先经用户批准）

第 0 步，在 Mac 上执行：把 3 个新文件传到服务器（只新增文件，不覆盖已有文件）。

```bash
cd /Users/kaijunlin/Developer/CeRebel/.claude/worktrees/awesome-lamport-adcb3e && COPYFILE_DISABLE=1 tar czf - motion_agent/app/retention_schedule.py motion_agent/app/privacy_backfill.py deploy/supervisor/cerebel-retention.conf | ssh -p 46474 root@proxy-ai.onethingai.com 'tar xzf - --no-same-owner -C /root/CerebelServer && cd /root/CerebelServer && sha256sum motion_agent/app/retention_schedule.py motion_agent/app/privacy_backfill.py deploy/supervisor/cerebel-retention.conf'
```

下面各步都在服务器上执行。SSH 断开后，需要重新执行第一行，再继续后面的命令。产品用的 Python 是
`/root/miniconda3/bin/python3`，这个 Python 带 torch；非登录 shell 里的 `python3` 指向 `/usr/bin/python3`，那个没有 torch。

```bash
cd /root/CerebelServer && set -a && . ./.env.product && set +a && PY=/root/miniconda3/bin/python3
```

第 1 步，只读检查：确认最近没有正在处理的上传，并预览清理内容。

```bash
tail -n 3 data/product/events.jsonl | cut -c1-90; $PY -m motion_agent.app.privacy_backfill
```

第 2 步，执行一次性清理。先把两个小文件备份一份，确认清理结果没问题后再删掉备份。

```bash
mkdir -m 700 -p /root/privacy-backup-20261026 && cp -p data/product/events.jsonl data/product/library.json /root/privacy-backup-20261026/ && $PY -m motion_agent.app.privacy_backfill --apply && $PY -m motion_agent.app.privacy_backfill
```

复查时应该看到：`0 deleted accounts`，`70 already clean`。确认之后删除备份：

```bash
rm -rf /root/privacy-backup-20261026
```

第 3 步，第一次正式运行 retention。这一步会删除 72 个会话的原始视频（5.55 GB），删除后无法恢复。

```bash
$PY -m motion_agent.app.retention --dry-run && $PY -m motion_agent.app.retention
```

第 4 步，安装每日任务。`update cerebel-retention` 只影响这一个程序，不会动 cerebel-product 和 cloudflared。

```bash
cp deploy/supervisor/cerebel-retention.conf /etc/supervisor/conf.d/ && supervisorctl reread && supervisorctl update cerebel-retention && supervisorctl status && $PY -m motion_agent.app.retention_schedule --next
```

如果暂时不想删除那 72 个内部会话的视频，可以跳过第 3 步。但要注意：装上第 4 步的每日任务后，它会在下一个 07:41 UTC
自动执行同样的删除。所以如果要保留视频，第 4 步也要一起推迟，或者先把要保留的视频拷走。

## 之后怎么查看

- 每次运行的记录：`data/product/retention.jsonl`（只有 id 和数量），程序日志在 `retention.log`。
- 一次性清理的记录：`data/product/privacy_backfill.jsonl`。
- 停用每日任务：`supervisorctl stop cerebel-retention`，再删除 `/etc/supervisor/conf.d/cerebel-retention.conf`，然后执行 `supervisorctl update cerebel-retention`。

## 执行记录（2026-10-26 UTC，用户批准后执行）

- 18:17 第 0 步：3 个新文件上传完成，sha256 与本地一致。
- 18:39 第 1 步：预检时没有正在运行的流水线进程。18:13 的上传 AE9CB2D7 在 18:23 报了 pipeline_error，原因是 GPU worker 没有运行，与本次清理无关。
- 18:39 第 2 步：执行 `privacy_backfill --apply`，退出码 0。
  - 清掉 2 个已删账号的残留：1 个 GEM-X 目录、1 个颈部目录、2 行库记录、8 行 events；另有 2 行 account_deleted 去掉了 user_id。
  - 69 个 hpe_results.pt 完成脱敏（55FEE713 的那份已随目录删除）。
  - 复查：报告显示 0 个已删账号、69 个已脱敏；events 从 85 行变为 77 行，库记录从 5 行变为 3 行。备份已删除。
- 18:40 第 3 步：retention 正式运行，退出码 0，删除了 72 个会话的原始视频（5.55 GB），磁盘可用空间从 22G 增加到 28G。派生结果和 demo 都还在，复跑 dry-run 结果为 0。
- 18:40 第 4 步：`cerebel-retention` 进入 RUNNING 状态，下次运行时间是 2026-10-27 07:41 UTC。
- 说明：18:28 有别的会话部署了 claude/sweet-sinoussi-f47a6c 的 product_api.py、library.py、chat_server.py，并在 18:36 重启了 cerebel-product，这不是本次操作做的。那个分支没有改动本次用到的函数。
