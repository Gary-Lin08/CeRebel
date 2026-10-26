# 贡献规范

## 流程

1. 从最新的 `main` 拉一个分支，名字说清楚做什么：`fix-upload-timeout`、`sport-pack-tennis`。
2. 改动前先跑一遍测试，记下基线（见下面）。
3. 一个 PR 只做一件事。改接口的 PR 里同时改 `sdk/openapi.yaml`、两个 SDK 和它们的模拟服务器。
4. 提 PR 到 `main`，写清楚：改了什么、为什么、怎么验证的、对用户有什么影响。按 [安全审查清单](security.md#代码审查清单) 自查。
5. 合并后，网页需要手动部署（见[部署](deployment.md#日常维护)）；Modal 函数推到 `main` 会自动部署。

## 提交信息

沿用仓库已有的写法：英文，第一行写**用户或系统能看到的效果**，不写实现细节，不加 `feat:` 之类的前缀。

```text
Pre-upload check: stop flagging good swings as "whole body not in frame"
GPU worker down: refuse uploads at once, survive worker restarts, label where analyses fail
```

需要解释原因时空一行写正文。

## 代码风格

- Python 3.10。文件开头 `from __future__ import annotations`，函数写类型注解。
- 能用标准库就用标准库：计费模块直接调 Stripe 的 HTTP 接口，SDK 零依赖。加依赖要说明理由，并写进 `deploy/us/requirements-web.txt`（注意 `numpy` 被 OpenSim 锁在 1.25）。
- 注释写"为什么"，不写"做了什么"。代码和注释用英文，面向团队的文档用中文。
- 前端是不需要构建的原生 HTML/CSS/JS。遵守 [`DESIGN.md`](../DESIGN.md)。
- `data/product/` 下的文件读写用 `file_lock.locked()`，写入先写临时文件再替换。
- 给用户看的文案：英文、句首大写、不用全大写标签；出错时说清楚发生了什么、这次算不算次数、用户下一步怎么做。

## 测试

### 服务端

```bash
PYTHONPATH=. MOTION_AGENT_BACKEND=mock python -m unittest discover -s motion_agent/tests -t .
```

2026-10-26 在 Python 3.10 上的基线：263 个，238 个通过，10 个跳过，3 个失败，12 个报错。失败和报错基本都因为测试写死了旧服务器上的会话 `/root/CerebelServer/data/sessions/C991F337-…`，另有 1 个已知失败 `test_golf_kb_scope_defaults`。规则是**不新增失败**。把这些测试改成使用仓库自带的样例数据（`kinetic-lens/public/assets/model/`）是一个值得做的独立任务。

### SDK

```bash
cd sdk/python && python -m unittest discover -s tests -t .
```

```bash
cd sdk/typescript && npm install && npm test && npm run typecheck
```

两套 SDK 都带一组真服务器测试，设置 `CEREBEL_TEST_BASE_URL` 后才运行，会注册并删除一个临时账号。只对本地或预发布环境运行。

GitHub Actions 的 `sdk.yml` 会在改动 `sdk/` 时自动跑上面两套测试。

### 前端

改了界面，用浏览器在电脑和手机两种宽度下走一遍：首页 → 示例报告 → 注册（不勾选同意应该无法注册）→ 上传被拒绝的情况。本地没有 GPU，上传流程用 `cerebel mock api`，或者在预发布环境测。

## 文档

- 改了行为就改对应文档：接口 → `docs/api.md` 和 `sdk/openapi.yaml`；环境变量 → `docs/configuration.md` 和 `deploy/env.product.example`；数据文件 → `docs/data-formats.md`。
- 文档里写的命令要实际跑过。写不确定的东西时直接说不确定，给出依据。

## 版本

SDK 遵循语义化版本，版本号在 `sdk/python/cerebel/_version.py` 和 `sdk/typescript/package.json`（以及 `src/client.ts` 的 `SDK_VERSION`），两边保持一致。接口有不兼容的改动时升主版本，并在 PR 里说明迁移方法。
