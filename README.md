# CeRebel

**Motion analysis from a phone video.** Upload a short clip of a movement (golf swing first). CeRebel rebuilds a 3D musculoskeletal model (OpenSim), measures joint angles and estimated loads, names the one thing to fix, and an AI coach answers follow-up questions. Every number comes from deterministic motion tools, never from the language model.

用手机拍一段动作视频（目前主打高尔夫挥杆），CeRebel 重建 3D 肌骨模型，算出关节角度和估算负荷，指出最该改的一个动作，再由 AI 教练回答追问。教练回答里的每个数字都来自确定性工具，不由语言模型生成。

```text
视频 ─► GEM-X（GPU，3D 人体重建）─► OpenSim 逆运动学 ─► 运动包：识别、分阶段、找问题 ─► 报告 + AI 教练
```

## 从哪里开始

| 你是 | 读这个 |
|---|---|
| 接手开发的人 | [快速上手](docs/getting-started.md)：不需要 GPU 和模型密钥，30 分钟在本机跑起来 |
| 在 CeRebel 之上做应用 | [API 指南](docs/api.md)、[SDK](sdk/README.md)（Python、TypeScript、OpenAPI） |
| 接 Cerebel AI 眼镜 | [AR 眼镜接入](docs/ar-glasses.md) |
| 部署和运维 | [部署](docs/deployment.md)、[安全与隐私](docs/security.md) |

全部文档：[`docs/`](docs/README.md)

## 试一下

```bash
pip install -e sdk/python
```

用内置的模拟服务器把"上传 → 排队 → 重建 → 报告 → 追问教练"走一遍（不连任何真服务器）：

```bash
python sdk/python/examples/analyze_swing.py
```

## 仓库结构

| 路径 | 内容 |
|---|---|
| `motion_agent/` | 网页服务和 API、动作计算、AI 教练、运动包、知识库、测试 |
| `motion_agent/app/ui/` | 前端（静态文件，不需要构建） |
| `gemx_pipeline.py`、`ik_pipeline.py`、`upload_server.py` | GPU 队列和 SOMA → OpenSim 流水线 |
| `kinetic-lens/` | 3D 查看器、OpenSim 模型、颈部重定向 |
| `deploy/` | 部署脚本和手册（Mac mini、DigitalOcean、Modal） |
| `sdk/` | Python / TypeScript SDK、OpenAPI 规范 |
| `docs/` | 开发文档（中文） |

## 状态

- 线上：`https://cerebel.tech/playground`。高尔夫已开放，其他运动在 `motion_agent/sports/` 里排队。
- 服务端要求 Python 3.10–3.12（3.13 删掉了服务端用到的 `cgi` 模块）。
- 安全规则和报告方式见 [安全与隐私](docs/security.md)。

## 许可

仓库目前没有许可证文件。在加入 LICENSE 之前，代码默认保留所有权利，使用前请联系 Cerebel, Inc.（garylin@cerebel.tech）。第三方组件（OpenSim 模型、MoBL-ARMS、MediaPipe 等）各自的许可见其目录里的说明；MoBL-ARMS 仅限非商业使用。
