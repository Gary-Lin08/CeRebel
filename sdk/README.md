# CeRebel SDK

在 CeRebel 动作分析之上做应用用的客户端：手机 App、网页、教练后台、Cerebel AI 眼镜桥接、批量脚本。

| 目录 | 语言 | 适合 | 依赖 |
|---|---|---|---|
| [`python/`](python/README.md) | Python 3.9+ | 脚本、服务端集成、**眼镜桥接**（电脑或树莓派）、命令行 `cerebel` | 无（只用标准库） |
| [`typescript/`](typescript/README.md) | TypeScript / JavaScript | 网页、React Native 手机伴侣 App、Node 服务 | 无（用标准 `fetch`） |
| [`openapi.yaml`](openapi.yaml) | OpenAPI 3.1 | 生成其他语言的客户端（Swift、Kotlin…），导入 Postman | — |

两个 SDK 的功能一一对应：

| 功能 | Python | TypeScript |
|---|---|---|
| 注册 / 登录 | `register()`、`login()` | `register()`、`login()` |
| 视频处理同意 | `accept_video_consent()` | `acceptVideoConsent()` |
| 上传 → 等待 → 报告，一步完成 | `analyze(path)` | `analyze(blob)` |
| 只上传 / 只等待 | `upload()`、`wait_for_analysis()` | `upload()`、`waitForAnalysis()` |
| 报告、阶段、对比 | `sport_analysis()`、`golf_phases()`、`compare()` | `sportAnalysis()`、`golfPhases()`、`compare()` |
| 主推的那条修正 | `main_fix()` | `mainFix()` |
| AI 教练（流式） | `ask_stream()` | `askStream()` |
| 眼镜 9080 协议、HUD 卡片 | `cerebel.glasses` | `glasses` |
| 眼镜桥接、文件夹监听、模拟眼镜 | `GlassesBridge`、`FolderWatcher`、`MockGlasses` | — |
| 模拟 API 服务器 | `cerebel.testing.MockCerebelServer`、`cerebel mock api` | 用注入的 `fetch` |

两个 SDK 都替你处理了几件容易出错的事：

- 每种错误状态码对应一个异常类（`ConsentRequired`、`PaymentRequired`、`UploadRejected`、`ServiceUnavailable`…），不用自己判断状态码。
- 等待分析时，服务端会先写 `ready`、过几秒运动识别才写入结果（可能改成 `no_swing`），SDK 会等到识别完成才返回。
- 线上入口在 Vercel 代理后面，可以把上传单独发到应用服务器的地址（`upload_base_url` / `uploadBaseUrl`）。
- 服务器装在路径前缀下（`https://cerebel.tech/playground`）时，接口地址和 `/media/` 链接都能正确拼接。

文档：[API 指南](../docs/api.md) · [AR 眼镜接入](../docs/ar-glasses.md) · [全部开发文档](../docs/README.md)

版本 0.1.0，对应服务端 2026-10-26 的 `main`。改服务端接口时，在同一个 PR 里同步更新 `openapi.yaml`、两个 SDK 和模拟服务器。
