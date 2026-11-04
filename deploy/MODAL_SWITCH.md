# 把 GPU 从本机切到 Modal：操作手册

这份手册给创始人在 Modal 额度到账后自己操作。所有命令都需要你本人执行；不要把 token、密码发到聊天里，也不要提交到 GitHub。

切换只改配置，不用改代码。前提是服务器上跑的代码已经包含 `modal-ready` 分支的改动（见第 6 步）。

标了 **【未验证】** 的地方，是没有 Modal 账号时没法验证的，第一次操作时要特别留意输出。

---

## 0. 切换前后的区别

| | 现在（local） | 切换后（modal） |
|---|---|---|
| 3D 人体重建（GEM-X） | 本机 GPU，`cerebel-gemx-worker`（127.0.0.1:61017） | Modal 上的 `cerebel-gemx` 应用，GPU 只在美国机房 |
| 同时处理几个视频 | 1 个 | 默认 10 个（`CEREBEL_QUEUE_WORKERS`，不超过 Modal 的 `max_containers`） |
| 后处理（IK、隐私脱敏） | 本机 | 仍在本机，同时最多 4 个（`CEREBEL_POSTPROCESS_WORKERS`） |
| `/api/health` 的 `gpu.state` | `ready` / `loading` / `down` | `remote`（能连上 Modal 上已部署的应用）或 `down` |
| 什么时候拒绝上传 | 本机 worker 连不上 | 没有 token、没装 `modal`、应用没部署、连不上 Modal |
| 费用 | 租机器的固定费用 | 按秒计费，没有任务时不花钱（`min_containers=0` 时） |

本机的 `/root/GEM-X` 不能删：IK 和隐私脱敏还在本机用 `/root/GEM-X/.venv/bin/python` 和里面的 SOMA 资源，结果也还写到 `/root/GEM-X/outputs/cerebel/`。

---

## 1. Modal 账号、付款方式和额度

1. 登录 modal.com。`deploy/us/DEPLOY_US.md` 里写着你已经有账号；没有的话就用公司邮箱注册一个。
2. Settings → Billing：绑定付款方式。Modal 要求有付款方式才能部署 GPU 函数，即使有免费额度也一样。
3. NVIDIA Inception 的额度到账后，确认它加在了你要用的这个 workspace 上，并记下额度金额和到期时间。**【未验证】** 额度怎么兑换、能不能抵扣区域加价，以 Modal 后台为准。
4. 建议在 Billing 里设一个每月花费上限（spend limit），防止配置错误烧钱。

## 2. API token：三台机器各用各的

在 modal.com → Settings → API Tokens 建 token。每个地方单独一个，出问题时可以只吊销那一个：

| 用在哪里 | 怎么建 | 放在哪里 |
|---|---|---|
| 你的 Mac（部署、prefetch、smoke） | `modal token new`（`deploy/modal_setup.sh` 会自动运行，在浏览器里点同意） | `~/.modal.toml`，脚本自动写好 |
| GitHub 自动部署（可选） | 后台 New Token | 仓库 Settings → Secrets → `MODAL_TOKEN_ID`、`MODAL_TOKEN_SECRET` |
| 产品服务器 | 后台 New Token，命名如 `cerebel-server` | `/root/CerebelServer/.env.product` 的 `MODAL_TOKEN_ID`、`MODAL_TOKEN_SECRET` |

## 3. 部署 GPU 应用（在 Mac 上做，不在中国服务器上做）

中国服务器连不上 github.com，构建镜像也需要从 GitHub 和 Hugging Face 下载东西，所以只在 Mac 上部署。镜像在 Modal 的美国云端构建，不会从你的 Mac 上传大文件。

一定要从包含 `modal-ready` 改动的代码部署：这些改动让 GPU 容器启动时不再访问 github.com（见第 4 步），并且会拒绝美国以外的区域。

**方法 A：脚本（推荐第一次用）**

```bash
cd ~/Developer/CeRebel        # 切到包含 modal-ready 改动的分支
bash deploy/modal_setup.sh    # 或者双击 deploy/modal_setup_mac.command
```

脚本依次做：安装 Modal 客户端 → 登录 → `modal deploy`（第一次构建 15～30 分钟）→ `prefetch` → 在 L40S 和 A100-40GB 上各跑一次 smoke。

**方法 B：GitHub 自动部署**

先把第 2 步的 GitHub token 填进仓库 Secrets，然后在 Actions → modal-deploy → Run workflow 手动运行；之后改动 `deploy/modal_gemx.py` 或 `deploy/gemx_overlay/` 并推到 main，就会自动部署。注意：在 Secrets 填好之前，把这些文件的改动合进 main 会触发一次失败的 modal-deploy（红色，会发邮件），不影响网站。方法 B 不会运行 prefetch，第一次仍要在 Mac 上跑第 4 步。

部署时可以用的环境变量（在 Mac 上 `export`，或者在 GitHub 的仓库 Variables 里设）：见第 9 步。

## 4. prefetch：把运行时要用的东西全部放进 Volume

```bash
python3 -m modal run deploy/modal_gemx.py::prefetch
```

它把权重从 Hugging Face 下载到 Volume `cerebel-gemx-inputs`，把 YOLOX 权重和 DINOv3 的 torch.hub 代码放进 Volume `cerebel-gemx-cache`。可以重复运行，已经下载过的会跳过。

**看输出最后的 `PREFETCH RESULT`：**

- `runtime_ready: True`：GPU 容器冷启动时不需要下载任何东西，也不访问 github.com。
- `missing: []`，`extra` 里有 `yolox` 和 `dinov3_hub`（值是 `/cache/torch/hub/facebookresearch_dinov3_main`）。
- 如果出现 `runtime_ready: False`、`dinov3_error` 或 `yolox_error`，就再跑一次 prefetch；还不行就把这段输出交给开发助手。

为什么要这样：GEM-X 加载 ViTPose 和 SAM-3D-Body 时会调用 `torch.hub.load("facebookresearch/dinov3", ..., source="github")`。即使代码已经缓存，torch.hub 每次也会先去 github.com 查默认分支。2026-11-02 本机 worker 反复崩溃就是这个原因。现在 `deploy/modal_gemx.py` 在加载模型前把这两个调用改成读 Volume 里的缓存。

## 5. smoke：真实跑一次（会产生少量费用）

`modal_setup.sh` 已经跑过 smoke。单独跑：

```bash
python3 -m modal run deploy/modal_gemx.py::smoke --video kinetic-lens/public/assets/layup-original.mp4
```

每一行输出 `SMOKE gpu=... run=0 wall=...s archive=...B {...}`。要检查：

- `ok: True`，`archive` 大于 0。
- `hub` 显示 `{'facebookresearch/dinov3': '/cache/torch/hub/facebookresearch_dinov3_main'}`。如果显示 `missing: run prefetch`，说明这次冷启动访问了 github.com，回到第 4 步。
- `run=0` 是冷启动，`run=1` 是热启动。记下两次的 `wall` 和 `model_load_s`，后面算费用和设 `CEREBEL_AVG_JOB_SECONDS` 要用。**【未验证】** 冷启动有多慢（本机加载模型约 103 秒，Modal 上还要加上拉镜像的时间）。
- Modal 后台 → Apps → cerebel-gemx → 这次调用的容器所在区域是美国。

一次 smoke 大约用掉几分钟的 GPU 时间，按第 9 步的价格算不到 1 美元。

## 6. 服务器准备（还不切换，网站照常用本机 GPU）

### 6.1 代码

线上 `cerebel-product` 跑的是 `/root/CeRebel`（`admin-dashboard-live`，f0ac968）。`modal-ready` 分支在它之上改了这些文件，部署时需要一起带上：

- `gemx_pipeline.py`、`upload_server.py`（仓库根目录）
- `deploy/modal_gemx.py`、`deploy/env.product.example`、`.github/workflows/modal-deploy.yml`（服务器上不运行，保持一致即可）
- 测试：`motion_agent/tests/test_modal_backend.py`、`motion_agent/tests/test_gpu_worker.py`

部署代码需要你单独批准。这些改动在 `CEREBEL_GPU_BACKEND=local` 时行为不变，只修了几个并发下才会出现的问题，所以可以先上线代码、晚些再切换。

### 6.2 服务器能不能连上 Modal（最大的风险）

中国服务器连不上 github.com，Modal 的接口（`api.modal.com`）也可能被墙或很慢。切换前必须先测：

```bash
curl -sS -m 15 -o /dev/null -w 'modal api: HTTP %{http_code} in %{time_total}s\n' https://api.modal.com/
```

能返回 HTTP 状态码（数字是多少不重要）就说明能连上；超时或 `Could not resolve host` 就说明连不上。连不上就停下，不要切换。可能的办法：给服务器配一个出境代理，或者把网站搬到美国（`deploy/us/DEPLOY_US.md`）。

另外，上传的视频大于 2 MiB 时，Modal 客户端会先把它上传到 Modal 在美国东部的对象存储。**【未验证】** 这条路径从中国服务器上传是否稳定、速度如何，只有第 7 步真实上传时才知道。

### 6.3 安装 Modal 客户端

产品用的是登录 shell 里的 `python3`（`/root/miniconda3/bin/python3`）：

```bash
bash -lc 'python3 -m pip install "modal>=1.0,<2"'
# PyPI 慢的话加国内镜像：  -i https://pypi.tuna.tsinghua.edu.cn/simple
bash -lc 'python3 -c "import modal; print(modal.__version__)"'
```

### 6.4 填 token 并试连（不启动 GPU，不花钱）

先备份配置文件：

```bash
cp -p /root/CerebelServer/.env.product /root/CerebelServer/.env.product.bak-$(date +%Y%m%d)
nano /root/CerebelServer/.env.product
```

只加两行 token，`CEREBEL_GPU_BACKEND` 先保持 `local`：

```
MODAL_TOKEN_ID=<服务器专用 token id>
MODAL_TOKEN_SECRET=<服务器专用 token secret>
```

试连（只是查一下已部署的应用，不会启动 GPU）：

```bash
cd /root/CeRebel && bash -lc 'set -a; . /root/CerebelServer/.env.product; set +a; python3 -c "import modal; modal.Cls.from_name(\"cerebel-gemx\", \"Gemx\").hydrate(); print(\"modal ok\")"'
```

看到 `modal ok` 才继续。报错时常见原因：token 填错（AuthError）、应用没部署或名字不对（NotFoundError）、网络不通（超时）。

## 7. 切换

1. 确认现在没有正在处理的上传（重启会中断正在处理的任务）：
   ```bash
   tail -n 5 /root/CerebelServer/data/product/events.jsonl | cut -c1-120
   ```
   最近几分钟没有 `upload_saved`，或者都已经有对应的 `capture_finalized` / `pipeline_error`，就可以继续。
2. 改配置：`nano /root/CerebelServer/.env.product`
   ```
   CEREBEL_GPU_BACKEND=modal
   CEREBEL_QUEUE_WORKERS=10          # 不要超过部署时的 CEREBEL_MODAL_MAX_CONTAINERS
   CEREBEL_AVG_JOB_SECONDS=240       # 先按冷启动估计，用第 8 步的真实耗时再改
   ```
   可选项（不写就用默认值）：`CEREBEL_MODAL_APP=cerebel-gemx`、`CEREBEL_MODAL_CALL_TIMEOUT_S=1500`、`CEREBEL_MODAL_STATUS_TTL_S=300`、`CEREBEL_POSTPROCESS_WORKERS=4`。
3. 停掉本机 GPU worker，并且让它重启机器后也不自动起来：
   ```bash
   supervisorctl stop cerebel-gemx-worker
   sed -i 's/^autostart=true/autostart=false/' /etc/supervisor/conf.d/cerebel-gemx-worker.conf
   supervisorctl reread && supervisorctl update
   ```
4. 重启网站：
   ```bash
   supervisorctl restart cerebel-product
   supervisorctl status
   ```
   应该看到 `cerebel-product RUNNING`，`cerebel-gemx-worker STOPPED`。

## 8. 验证

1. 本机健康检查：
   ```bash
   curl -s http://127.0.0.1:8092/api/health; echo
   ```
   `"gpu": {"ok": true, "state": "remote"}`，HTTP 200。如果是 `down`，看日志里 `Modal GPU backend down: ...` 那一行写的原因：
   ```bash
   grep "Modal GPU backend" /root/CeRebel/product_server.log | tail -n 3
   ```
2. 公网：`curl -s https://cerebel.tech/playground/api/health`，同样是 `remote`。然后在 GitHub → Actions → uptime → Run workflow 手动跑一次，应该是绿色。
3. 真实上传：用你自己的账号传一段侧面挥杆视频。日志里应该依次出现：
   ```bash
   grep -E "Modal GEM-X call|\(modal\)|GEM-X ready|GEM-X failed" /root/CeRebel/product_server.log | tail -n 5
   ```
   - `Modal GEM-X call session=<id> id=fc-...`（任务已经交给 Modal）
   - `warm GEM-X done ... (modal)`（结果已经取回）
   - `GEM-X ready session=<id> ... ik=ok`（本机后处理完成）
   - 该 session 的 `gemx.json` 里 `"mode": "modal"`。
   页面上能打开报告，有关节角度。
4. Modal 后台 → Apps → cerebel-gemx：能看到这次调用，容器在美国；记下它的耗时。
5. 用实测耗时更新 `CEREBEL_AVG_JOB_SECONDS`（页面上"大约 N 分钟"的估计用这个数），然后 `supervisorctl restart cerebel-product`。

## 9. 回滚到本机 GPU

任何时候 Modal 出问题，都可以 5 分钟内回到本机：

```bash
nano /root/CerebelServer/.env.product          # CEREBEL_GPU_BACKEND=local；CEREBEL_QUEUE_WORKERS= 留空（本机只能 1 个）
sed -i 's/^autostart=false/autostart=true/' /etc/supervisor/conf.d/cerebel-gemx-worker.conf
supervisorctl reread && supervisorctl update
supervisorctl start cerebel-gemx-worker        # 模型加载约 2 分钟
supervisorctl restart cerebel-product
curl -s http://127.0.0.1:8092/api/health; echo # 先是 loading，之后变成 ready
```

或者直接用备份：`cp -p /root/CerebelServer/.env.product.bak-<日期> /root/CerebelServer/.env.product`，再执行上面后四行。

Modal 上的应用可以保持部署状态。`min_containers=0` 时没有任务就不花钱，下次切换时不用重新部署。

## 10. 费用旋钮

这些值在**部署时**读取（Mac 上 `export` 后再跑 `modal deploy deploy/modal_gemx.py`，或者设 GitHub 仓库 Variables 后运行 modal-deploy）。改了要重新部署才生效。

| 变量 | 默认 | 作用 |
|---|---|---|
| `CEREBEL_MODAL_GPU` | `L40S` | GPU 型号。也可以试 `A100-40GB`、`A10`、`L4`（便宜但可能显存不够或更慢）。**【未验证】** 哪种最划算，用 smoke 的耗时比较 |
| `CEREBEL_MODAL_MIN_CONTAINERS` | `0` | 一直保持热着的 GPU 数量。0 = 没任务不花钱，但每次空闲后的第一个上传要等冷启动；1 = 永远不冷启动，但 24 小时都在计费 |
| `CEREBEL_MODAL_SCALEDOWN_S` | `300` | 任务做完后容器再空闲多久才关。调小省钱，但接连上传时冷启动更多 |
| `CEREBEL_MODAL_MAX_CONTAINERS` | `10` | 最多同时开几个 GPU。Starter 套餐上限就是 10；网站上的 `CEREBEL_QUEUE_WORKERS` 不要超过它 |
| `CEREBEL_MODAL_REGION` | `us` | 只能是美国区域，代码会拒绝其他区域（见第 11 步） |

**按秒计费的注意事项：** Modal 计费的是容器活着的整个时间，不只是推理那几十秒。包括冷启动（拉镜像 + 加载模型，本机加载约 103 秒）、推理、以及做完后空闲等待的 `scaledown` 时间。CPU 和内存另外按用量计费。

价格（2026-11 Modal 价格页）：L40S 基础价 $0.000542/秒；限定 `us` 区域要乘 1.15，约 $0.000623/秒，约 $2.24/小时。A100-40GB 基础价 $0.000583/秒，A10 $0.000306/秒，L4 $0.000222/秒。Starter 套餐每月送 $30。以 Modal 后台为准。

粗略估算（L40S，`us`）：

- 一个孤立的上传（之前空闲）：冷启动约 100～150 秒 + 推理约 60 秒 + 空闲 300 秒，约 460～510 秒，约 $0.29～0.32。**【未验证】** 冷启动和推理的真实时长。
- 接连多个上传：共享一个热容器，每个只多算推理时间，约 $0.04。
- `min_containers=1`：24 × 30 × $2.24，约 $1,600/月。上线初期不建议。

## 11. 区域必须在美国（隐私政策）

隐私政策写着视频由 Modal 在美国处理。`deploy/modal_gemx.py` 现在会拒绝 `CEREBEL_MODAL_REGION` 里任何不是 `us` 或 `us-...` 的值，部署直接报错；GitHub 工作流也有同样的检查。不要改成其他区域。

还有几点需要你决定，或者请律师看一下隐私政策的写法：

- Modal 文档说，所有调用都经过它在弗吉尼亚（美国东部）的服务器；大于 2 MiB 的输入和输出会存到美国东部的对象存储。都在美国，和政策一致。
- 隐私政策写的是视频在"任务结束时丢弃的临时工作区"里处理。GPU 容器里的临时目录确实在任务结束时删除。但 Modal 文档说，`spawn` 调用的结果最多保留 7 天，结果里有人体重建数据，不含视频。**【未验证】** 作为输入上传的视频在 Modal 对象存储里保留多久。如果政策要写得严格准确，可能需要改措辞，或者向 Modal 确认数据保留策略。
- 网站和数据仍在中国服务器上。切换后，视频会从中国传到美国处理。

## 12. 还没验证的事（需要真实 Modal 账号）

- 新版 `deploy/modal_gemx.py` 能否顺利 `modal deploy`（单元测试只验证了它在模拟的 `modal` 下能导入，以及 torch.hub 的重定向逻辑）。
- prefetch 的 `runtime_ready: True`，以及冷容器真的不访问 github.com（看 smoke 输出里的 `hub`）。
- 服务器端用的 `spawn()` + `get(timeout=...)` + `cancel()`：按 Modal 文档写的，测试用的是假的 `modal`。
- 中国服务器到 `api.modal.com` 和 Modal 对象存储的连通性和速度。
- 冷启动时长、单个视频的真实费用、哪种 GPU 最划算。
- Inception 额度的兑换方式和适用范围。

## 13. 已知限制

- 只有网页上传的视频能走 Modal。iPhone App 实时采集（`server.py`，只有 JPEG 帧、没有视频文件）的 session 在 modal 模式下会失败，并提示 "Modal backend needs the uploaded video file"。现在产品不用这条路径。
- 旧的 `web_api.py` 不是产品的一部分，它会检查本机的 GEM-X python，和 Modal 无关。
- 切换后，同一时间最多 10 个视频在 Modal 上跑，本机同时最多 4 个后处理，其余在本机排队。服务器 CPU 或内存紧张时，把 `CEREBEL_POSTPROCESS_WORKERS` 调小。
