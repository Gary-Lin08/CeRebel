# cerebel（Python SDK）

CeRebel 动作分析 API 的 Python 客户端，带 Cerebel AI 眼镜桥接和命令行工具 `cerebel`。只用标准库，Python 3.9+。

## 安装

在仓库根目录：

```bash
pip install -e sdk/python
```

## 用法

```python
from cerebel import CerebelClient, ConsentRequired, main_fix

client = CerebelClient("https://cerebel.tech/playground", upload_base_url=UPLOAD_ORIGIN)
client.login("you@example.com", "…")            # 或 CerebelClient(token=...)

me = client.me()
if me["video_consent"]["required"]:
    print(me["video_consent"]["text"])           # 先给用户看，用户同意后：
    client.accept_video_consent(me["video_consent"]["version"])

result = client.analyze("swing.mp4", on_progress=lambda p: print(p["stage"], p.get("percent")))
fix = result["main_fix"]
print(fix["headline"], fix["metric"]["value"], fix["cue"])

for event in client.ask_stream("Walk me through my downswing.", result["session_id"]):
    if event["type"] == "done":
        print(event["answer"])
```

`UPLOAD_ORIGIN` 是应用服务器自己的地址：线上入口在 Vercel 代理后面，代理只能转发约 4.5 MB，所以上传要绕开它。向运维要这个地址；本地开发不需要设。

### 错误

所有错误都是 `CerebelError` 的子类，`message` 可以直接给用户看，`status` 是 HTTP 状态码（网络不通时为 0），`payload` 是完整的响应。

| 异常 | 状态码 | 怎么处理 |
|---|---|---|
| `AuthError` | 401 | 重新登录（令牌 30 天过期） |
| `PaymentRequired` | 402 | 本月次数用完，`exc.quota` 有详情 |
| `Forbidden` | 403 | 不是自己的会话 |
| `NotFound` / `Gone` | 404 / 410 | 会话不存在 / 已删除 |
| `UploadRejected` | 422 | 视频不合格（时长、帧率、分辨率），不计次 |
| `ConsentRequired` | 428 | 先同意视频处理，`exc.consent["text"]` 是要展示的文本 |
| `RateLimited` | 429 | 今天的提问次数用完 |
| `ServiceUnavailable` | 503 | GPU 离线，什么都没上传，稍后重试 |
| `AnalysisFailed` | — | 上传成功但分析结果是 `no_swing` 或 `error`，`exc.stage` 区分 |

### 主要方法

| 方法 | 说明 |
|---|---|
| `health()`、`sports()`、`plans()` | 服务状态、运动目录、套餐 |
| `register(email, password, accept_terms=True)`、`login()`、`logout()` | 账号；成功后自动保存令牌 |
| `me()`、`library()`、`accept_video_consent()`、`delete_account()` | 作品库、额度、同意 |
| `upload(path, trim=None)`、`progress(id)`、`wait_for_analysis(id)` | 上传和等待 |
| `analyze(path)` | 上传 + 等待 + 取报告，返回 `session_id`、`progress`、`analysis`、`main_fix` |
| `session(id)`、`sport_analysis(id)`、`golf_phases(id)`、`coach_briefs(id)`、`main_fix(id)` | 报告 |
| `compare(before, after)`、`log_practice(id, finding_id)`、`delete_capture(id)` | 进步对比、练习打卡、删除 |
| `media_url(path)`、`download_media(path, dest)` | 视频等文件 |
| `ask(question, id)`、`ask_stream(question, id)` | AI 教练 |
| `checkout(plan)`、`billing_portal()`、`track(event)` | 付费和埋点 |

## 命令行

```bash
cerebel login --email you@example.com
```

```bash
cerebel analyze swing.mp4
```

| 命令 | 作用 |
|---|---|
| `cerebel login` / `logout` / `whoami` | 登录（令牌存在 `~/.config/cerebel/credentials.json`，权限 600） |
| `cerebel consent [--accept]` | 查看 / 同意视频处理文本 |
| `cerebel analyze VIDEO [--trim 0.5 3.2] [--json]` | 分析一段视频，打印主推修正 |
| `cerebel fix SESSION_ID` | 打印已有会话的主推修正 |
| `cerebel ask "问题" [--session ID] [-v]` | 问 AI 教练 |
| `cerebel health` | 服务状态（没准备好时退出码为 1） |
| `cerebel glasses bridge --watch DIR \| --clip FILE [--glasses HOST:PORT \| --hud-command …]` | 眼镜桥接 |
| `cerebel glasses hud --glasses HOST --status … --reply …` | 给眼镜发一张卡片 |
| `cerebel mock api [--port 8099]` | 模拟 API 服务器 |
| `cerebel mock glasses [--port 9080]` | 模拟眼镜，打印收到的卡片；输入 `rec_stop` 回车模拟按键 |

全局参数：`--base-url`（环境变量 `CEREBEL_BASE_URL`，默认 `https://cerebel.tech/playground`）、`--upload-base-url`（`CEREBEL_UPLOAD_BASE_URL`）、`--token`（`CEREBEL_TOKEN`）。

## 眼镜桥接

眼镜只负责拍摄和显示，桥接程序在电脑或手机上运行：拿到录像 → 上传 → 等结果 → 把"一条要改的地方"发到镜片上。背景和协议见 [AR 眼镜接入](../../docs/ar-glasses.md)。

```python
from cerebel import CerebelClient
from cerebel.glasses import GlassesBridge, GlassesLink, LinkHudSink, ConsoleHudSink, CommandHudSink

client = CerebelClient(token=TOKEN, upload_base_url=UPLOAD_ORIGIN)

# 显示到哪里，三选一：
sink = LinkHudSink(GlassesLink("192.168.1.50", 9080).connect())        # 眼镜的 9080 链路
sink = CommandHudSink(["powershell", "-File", r"adb\show_text.ps1", "-Text", "{text}"])  # ADB 脚本
sink = ConsoleHudSink()                                                # 只打印，开发用

bridge = GlassesBridge(client, sink, lang="zh", fps=30)
bridge.process_clip("factory_20260815_122121.h264")   # 处理一段（.h264 裸流会自动封装成 MP4）
bridge.watch("./recordings")                          # 或者持续监听一个文件夹
```

| 组件 | 作用 |
|---|---|
| `GlassesLink` | 连接眼镜的 9080 端口；`send_hud()`；回调 `on_key`（photo / rec_start / rec_stop）、`on_pcm`、`on_hello` |
| `fix_card()`、`progress_card()`、`error_card()` | 生成 HUD 卡片（中文或英文状态行） |
| `prepare_clip(path, fps=30)` | 眼镜录的 `.h264` / `.h265` 裸流封装成 MP4（需要 ffmpeg） |
| `FolderWatcher` | 发现文件夹里新出现、且已经写完的视频 |
| `GlassesBridge` | 把上面这些串起来；出错时在 HUD 上显示原因再抛出异常 |
| `MockGlasses` | 模拟眼镜端，测试用 |

## 不连服务器开发

```python
from cerebel import CerebelClient
from cerebel.testing import MockCerebelServer

with MockCerebelServer(stage_seconds=0.5) as mock:
    client = CerebelClient(mock.url)
    client.register("dev@example.com", "password123", accept_terms=True)
    client.accept_video_consent()
    print(client.analyze("swing.mp4", poll_interval=0.2)["main_fix"]["headline"])
```

`swing.mp4` 可以是任何存在的文件，模拟服务器不看内容。它的接口和返回结构与真服务一致，上传会完整走一遍排队和处理阶段。文件名里带 `noswing`、`fail`、`short` 分别得到"没识别到挥杆"、"处理失败"、"质检不合格"。

## 测试

```bash
cd sdk/python && python -m unittest discover -s tests -t .
```

对真服务器（只用本地或预发布环境；会注册并删除一个临时账号）：

```bash
CEREBEL_TEST_BASE_URL=http://127.0.0.1:8092 python -m unittest tests.test_live_server -v
```

示例脚本在 [`examples/`](examples/)。
