# CeRebel 开发文档

CeRebel 是一个动作分析产品：用户拍一段运动视频（目前主打高尔夫挥杆），系统重建 3D 肌骨模型（OpenSim），算出关节角度和估算负荷，找出最该改的一个动作，再由 AI 教练解释。教练回答里的每个数字都来自确定性工具，不由语言模型生成。

这套文档写给三类人。按你的情况选一条路线读：

## 我要接手开发这个仓库（朋友、新同事）

1. [快速上手](getting-started.md)：不用 GPU、不用模型密钥，30 分钟在自己电脑上把服务跑起来。
2. [系统架构](architecture.md)：各个进程、端口、一段视频从上传到出报告经过的每一步。
3. [数据格式](data-formats.md)：会话目录里每个文件是什么，`opensim-motion.json` 等结果文件的结构。
4. [扩展指南](extending.md)：加一个新运动、给 AI 教练加一个工具、扩充知识库。
5. [配置参考](configuration.md)：所有环境变量。
6. [贡献规范](contributing.md)：分支、测试、提交、代码审查。
7. [安全与隐私](security.md)：必须遵守的规则和目前已知的风险。
8. [部署](deployment.md)：线上环境怎么搭、怎么更新。

## 我要在 CeRebel 之上做应用（外部开发者、未来用户）

1. [API 指南](api.md)：认证、上传、进度、报告、教练对话、错误处理。
2. [SDK 总览](../sdk/README.md)：Python 和 TypeScript 客户端，以及 [OpenAPI 规范](../sdk/openapi.yaml)。
3. 想先离线试：`cerebel mock api` 起一个模拟服务器，见 [Python SDK](../sdk/python/README.md)。

## 我要把它接到 Cerebel AI 眼镜上

1. [AR 眼镜接入](ar-glasses.md)：为什么计算不在眼镜上、三种部署方式、9080 链路协议、HUD 设计、桥接程序。
2. [Python SDK 的眼镜模块](../sdk/python/README.md#眼镜桥接)：`GlassesBridge`、模拟眼镜。
3. 眼镜端固件和真机脚本在私有仓库 `Gary-Lin08/cerebel-ar-glasses`。

## 其他已有文档

| 文档 | 内容 |
|---|---|
| [`PRODUCT_STATUS.md`](PRODUCT_STATUS.md) | 产品现状全记录：功能、用户流程、开发进度（截至 2026-10-26） |
| [`DESIGN.md`](../DESIGN.md) | Playground 设计系统（颜色、字体、组件） |
| [`deploy/us/DEPLOY_US.md`](../deploy/us/DEPLOY_US.md) | 美国服务器（DigitalOcean + Modal）上线手册 |
| [`deploy/mac/DEPLOY_MAC.md`](../deploy/mac/DEPLOY_MAC.md) | Mac mini + Cloudflare Tunnel 上线手册（手册写的当前方案，实际部署先向负责人确认） |
| [`deploy/LAUNCH.md`](../deploy/LAUNCH.md) | 公开测试版上线清单 |
| [`deploy/supervisor/PRIVACY_CLEANUP.md`](../deploy/supervisor/PRIVACY_CLEANUP.md) | 旧 GPU 服务器的隐私清理记录 |
| [`motion_agent/README.md`](../motion_agent/README.md) | 教练智能体和动作工具（英文） |
| [`kinetic-lens/docs/visualization-contract.md`](../kinetic-lens/docs/visualization-contract.md) | 3D 可视化的约定（坐标系、颜色含义） |
| [`HANDOFF.md`](../HANDOFF.md) | 2026-10-24 的交接快照。部分内容已过时（例如仓库现在是公开的），以本目录为准 |
