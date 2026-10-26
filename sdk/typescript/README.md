# @cerebel/sdk（TypeScript SDK）

CeRebel 动作分析 API 的 TypeScript 客户端，适合网页、React Native 手机伴侣 App 和 Node 服务。没有运行时依赖，用标准 `fetch`、`FormData`、`ReadableStream`。另含 Cerebel AI 眼镜 9080 链路的编解码和 HUD 卡片。

## 安装

还没有发布到 npm。在仓库里构建后按路径引用：

```bash
cd sdk/typescript && npm install && npm run build
```

然后在你的项目里 `npm install ../CeRebel/sdk/typescript`（路径按实际调整）。运行环境：Node 20.3+、现代浏览器，或提供了 `fetch` 的 React Native（也可以通过 `fetch` 选项传入）。

## 用法

```ts
import { CerebelClient, ConsentRequired, mainFix } from "@cerebel/sdk";

const client = new CerebelClient({
  baseUrl: "https://cerebel.tech/playground",
  uploadBaseUrl: UPLOAD_ORIGIN, // 线上入口在 Vercel 代理后面，上传要发到应用服务器自己的地址
});
await client.login("you@example.com", password);

const me = await client.me();
if (me.video_consent?.required) {
  // 把 me.video_consent.text 展示给用户，用户同意后：
  await client.acceptVideoConsent(me.video_consent.version);
}

// 浏览器：<input type="file"> 的 File；Node：await fs.openAsBlob("swing.mp4")
const result = await client.analyze(file, {
  onProgress: (p) => console.log(p.stage, p.percent, p.queue_message ?? ""),
});
console.log(result.main_fix?.headline, result.main_fix?.metric?.value, result.main_fix?.cue);

for await (const event of client.askStream("Walk me through my downswing.", result.session_id)) {
  if (event.type === "done") console.log(event.answer);
}
```

### 错误

都继承自 `CerebelError`（`status`、`payload`、`errorId`）：`AuthError` 401、`PaymentRequired` 402（`quota`）、`Forbidden` 403、`NotFound` 404、`Gone` 410、`UploadRejected` 422、`ConsentRequired` 428（`consent`）、`RateLimited` 429、`ServiceUnavailable` 503、`AnalysisFailed`（`stage` 为 `no_swing` / `error`）。`message` 可以直接展示给用户。

```ts
try {
  await client.upload(file);
} catch (err) {
  if (err instanceof ConsentRequired) showConsent(err.consent["text"] as string);
  else throw err;
}
```

### 方法

`health()`、`sports()`、`plans()`、`register({email, password, acceptTerms})`、`login()`、`logout()`、`me()`、`library()`、`acceptVideoConsent()`、`deleteAccount()`、`upload(blob, {trim})`、`progress(id)`、`waitForAnalysis(id, {onProgress, pollIntervalMs, signal})`、`analyze(blob)`、`session(id)`、`sportAnalysis(id)`、`golfPhases(id)`、`coachBriefs(id)`、`mainFix(id)`、`compare(before, after)`、`logPractice(id, findingId)`、`deleteCapture(id)`、`mediaUrl(path)`、`ask(q, id)`、`askStream(q, id)`、`checkout(plan)`、`billingPortal()`、`track(event, props)`。

返回值的类型在 `src/types.ts`；完整接口见 [`../openapi.yaml`](../openapi.yaml)。

## 眼镜链路

`glasses` 只负责编解码，不绑定具体的 socket 库，React Native（例如 `react-native-tcp-socket`）或 Node 的 `net` 都能用：

```ts
import { glasses } from "@cerebel/sdk";
import { connect } from "node:net";

const socket = connect(9080, "192.168.1.50");
const decoder = new glasses.FrameDecoder();
socket.on("data", (chunk) => {
  for (const msg of decoder.feed(new Uint8Array(chunk))) {
    if (msg.type === "key" && msg["action"] === "rec_stop") startAnalysis();
  }
});

socket.write(glasses.encodeFrame(glasses.progressCard({ session_id: "…", stage: "queued", percent: 22 }, "zh")));
socket.write(glasses.encodeFrame(glasses.fixCard(result.main_fix, "zh")));
```

协议和 HUD 设计见 [AR 眼镜接入](../../docs/ar-glasses.md)。

## 开发

```bash
npm install && npm test && npm run typecheck && npm run build
```

测试用注入的假 `fetch`，不需要服务器。对真服务器（只用本地或预发布环境，会注册并删除临时账号）：

```bash
CEREBEL_TEST_BASE_URL=http://127.0.0.1:8092 npm test
```

源码只用可擦除的 TypeScript 语法（`erasableSyntaxOnly`），Node 22.18+ 可以直接运行 `.ts` 测试，不需要先编译。
