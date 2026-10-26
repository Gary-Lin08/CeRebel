# 快速上手：在自己电脑上跑起来

目标：不需要 GPU、不需要语言模型密钥，在 macOS 或 Linux 上把网页服务跑起来，看到示例报告，能和 AI 教练对话（教练用 mock 模型回答）。下面每一步都在 2026-10-26 的 `main` 上实际跑通过。

## 1. 准备

| 需要 | 版本 | 说明 |
|---|---|---|
| Git（含 Git LFS） | 任意 | 仓库里有模型和视频资源 |
| Python | **3.10 – 3.12** | 服务端用了 `cgi` 模块，Python 3.13 已经删掉它，启动后每个请求都会报错 |
| Node.js | 20+ | 只有改 `kinetic-lens/` 或 TypeScript SDK 时需要 |
| ffmpeg | 任意 | 上传质检和截取视频片段用；只看报告可以不装 |

```bash
git clone https://github.com/Gary-Lin08/CeRebel.git
```

```bash
cd CeRebel && python3.10 -m venv .venv && source .venv/bin/activate
```

网页服务本身只需要这几个包（完整的线上依赖在 `deploy/us/requirements-web.txt`，包含 torch 和 OpenSim 相关的包，本地看报告用不到）：

```bash
pip install "numpy>=1.24,<2" "openai>=1.40" "scipy>=1.10,<1.15" "matplotlib>=3.7,<3.10"
```

## 2. 放一份示例数据

服务从 `CEREBEL_DATA_DIR`（默认是仓库下的 `data/`，已被 `.gitignore` 忽略）读取会话。仓库自带一段已经处理好的样例（`kinetic-lens/public/assets/`），把它复制成一个会话：

```bash
mkdir -p data/sessions/DEMO-LAYUP/ik && cp kinetic-lens/public/assets/model/opensim-motion.json kinetic-lens/public/assets/model/estimated-dynamics.json data/sessions/DEMO-LAYUP/ik/ && cp kinetic-lens/public/assets/layup-original.mp4 data/sessions/DEMO-LAYUP/ik/session.mp4
```

这段样例其实是篮球上篮，但当前的高尔夫检测器会以 0.71 的置信度把它认成挥杆，所以能看到一份完整的高尔夫报告。这是检测器的一个已知问题，正好可以用来开发。

## 3. 启动

```bash
CEREBEL_DATA_DIR=data CEREBEL_DEMO_SESSION_ID=DEMO-LAYUP MOTION_AGENT_BACKEND=mock MOTION_AGENT_HOST=127.0.0.1 PYTHONPATH=. python -m motion_agent.app.chat_server
```

- 打开 http://127.0.0.1:8092 ：消费者界面，未登录时显示示例报告。
- 打开 http://127.0.0.1:8092/?dev=1 ：操作员界面（Playground），能看教练调用了哪些工具。只有没设 `CEREBEL_OPERATOR_PORT` 时才在同一个端口上。
- `MOTION_AGENT_BACKEND=mock`：教练不调用真模型，按固定流程调用工具后给出占位回答。工具的输出是真实计算的。

检查服务状态：

```bash
curl -s http://127.0.0.1:8092/api/health
```

本地会返回 503，`gpu.state` 是 `down`。这是对的：本机没有 GPU worker，所以上传会被拒绝（返回 503，不占用次数），其他功能都能用。

## 4. 用 SDK 试一下

```bash
pip install -e sdk/python
```

```bash
CEREBEL_TEST_BASE_URL=http://127.0.0.1:8092 python -m unittest discover -s sdk/python/tests -t sdk/python
```

这会注册一个临时账号、读示例报告、和教练对话，最后删除账号。也可以用命令行：

```bash
cerebel --base-url http://127.0.0.1:8092 fix DEMO-LAYUP
```

## 5. 想测上传流程

本地没有 GPU，有两个办法：

1. **模拟服务器**（推荐给做前端、App、眼镜的人）：`cerebel mock api --port 8099` 起一个和真服务接口一致的假服务，上传后会走完"排队 → 重建 → 出报告"的全过程。文件名里带 `noswing` 会得到"没识别到挥杆"，带 `fail` 会失败，带 `short` 会被质检拒绝。
2. **接真的 GPU**：设置 `CEREBEL_GPU_BACKEND=modal` 和 Modal 凭据，用线上的 `cerebel-gemx` 函数；或者在有 NVIDIA GPU 的机器上装 GEM-X 和 OpenSim，见[部署](deployment.md)。

## 6. 跑现有测试

```bash
PYTHONPATH=. MOTION_AGENT_BACKEND=mock python -m unittest discover -s motion_agent/tests -t .
```

2026-10-26 的结果：263 个测试，238 个通过，10 个跳过，15 个失败或报错。失败的基本都是因为测试写死了旧服务器上的会话 `/root/CerebelServer/data/sessions/C991F337-…`，本地没有这份数据；另有 1 个已知失败 `test_golf_kb_scope_defaults`。改代码前先跑一遍记下基线，改完再跑，只要不新增失败就行。详见[贡献规范](contributing.md#测试)。

## 7. 改前端

- 网页是不需要构建的静态文件：`motion_agent/app/ui/index.html`、`playground-*.css`、`*.js`。改完刷新浏览器就能看到。
- 3D 查看器 `kinetic-lens/` 是 React + Vite 项目：

```bash
cd kinetic-lens && npm install && npm run dev
```

## 常见问题

| 现象 | 原因 |
|---|---|
| 每个请求都报 `ModuleNotFoundError: No module named 'cgi'` | 用了 Python 3.13+，换成 3.10–3.12 |
| `Address already in use` | 8092 被占用，换 `MOTION_AGENT_PORT` |
| 首页显示 "No demo capture is available yet" | `CEREBEL_DEMO_SESSION_ID` 没设，或者对应目录里没有 `ik/opensim-motion.json` |
| 上传返回 503 | 本机没有 GPU worker，见第 5 步 |
| 教练回答总是一样 | 用的是 `mock` 后端；要接真模型，按[配置参考](configuration.md#ai-教练和语言模型)设置 `MOTION_AGENT_BASE_URL` 等变量 |
