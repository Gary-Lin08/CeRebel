# Qwen3-VL API (this machine)

Verified 2026-09-09 (key rotated). Cloud API only — not local weights.

## Status

| Route | Result |
|--------|--------|
| DashScope **CN** (`dashscope.aliyuncs.com`) | Works with current `~/Qwen_KEY.txt` |
| DashScope **intl** (`dashscope-intl.aliyuncs.com`) | 401 — current key is CN-region |
| OpenAI (`api.openai.com`) | Does **not** work (TLS timeout / reset) |

HTTP proxy (`http://172.17.0.1:1080`) does not block DashScope. Python packages used for the smoke test: `openai`, `dashscope`.

## Auth

Key file: `~/Qwen_KEY.txt`

```
Qwen_API_KEY=sk-...
```

The motion agent prefers this file over a stale `DASHSCOPE_API_KEY` env var.

```bash
export DASHSCOPE_API_KEY="$(sed -n 's/^Qwen_API_KEY=//p' ~/Qwen_KEY.txt)"
```

**Base URL (required for current key):** `https://dashscope.aliyuncs.com/compatible-mode/v1`

Intl keys would instead use `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`.

## Minimal image call

```python
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["DASHSCOPE_API_KEY"],
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
)

r = client.chat.completions.create(
    model="qwen3-vl-plus",  # or qwen3-vl-flash
    messages=[{
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": "https://example.com/image.jpg"}},
            {"type": "text", "text": "Describe this image."},
        ],
    }],
    max_tokens=256,
)
print(r.choices[0].message.content)
```

Local files: encode as a data URI (`data:image/jpeg;base64,...`). The compatible API does **not** accept filesystem paths. For files ≥ ~7–20 MB, upload to DashScope temp OSS and pass `oss://...` with header `X-DashScope-OssResourceResolve: enable`.

## Smoke test (2026-09-09)

Public demo image (woman + Labrador on a beach):

| Model | Latency | Notes |
|--------|---------|--------|
| `qwen3-vl-plus` | ~2.6 s | Correct scene description |
| `qwen3-vl-flash` | ~2.7 s | Same; slightly wordier |

Prompt tokens were ~2524 (mostly image tokens). Both returned 200 with usage.

## Models on this key

Listed via `GET /models` on intl. Prefer aliases (they track the current snapshot):

| ID | Role | Context | Max output | Video |
|----|------|---------|------------|--------|
| `qwen3-vl-plus` | Default quality | 256k | 32k | yes (≤ 1 h) |
| `qwen3-vl-flash` | Faster / cheaper | 256k | 32k | yes (≤ 1 h) |
| `qwen3-vl-235b-a22b-instruct` | Open-source instruct | 128k | 8k | yes |
| `qwen3-vl-235b-a22b-thinking` | Open-source thinking | 128k | 8k | yes |

Pinned snapshots also exist (`qwen3-vl-plus-2025-12-19`, `qwen3-vl-flash-2026-01-22`, …).

The same intl endpoint also lists newer unified VL models (`qwen3.5-*`, `qwen3.6-*`, `qwen3.8-*`). Those are not Qwen3-VL; they have larger context and built-in tools. Use them only if you explicitly want the newer line.

## Capabilities (Qwen3-VL)

- Image Q&A, captioning, classification
- OCR / info extraction (receipts, forms); ~33 languages
- Visual math / science problems
- Screenshot / sketch → HTML/CSS/code
- 2D bounding boxes / points; **3D** localization (`bbox_3d`)
- Document parse: prompts `qwenvl html` or `qwenvl markdown`
- Video: event timeline with timestamps (JSON)
- Multi-image compare (product / multi-page)
- Function calling + JSON structured output (`plus` / `flash` / instruct)
- Thinking: **off** by default for `qwen3-vl-plus` and `qwen3-vl-flash`; always on for `*-thinking` ids

Does **not** understand audio tracks in video.

## Limits (Qwen3-VL series)

**Images**

- Min size: both sides > 10 px; aspect ≤ 200:1
- Prefer ≤ 8K; JPEG/PNG/WebP/BMP/TIFF/HEIC below 4K; JPEG/PNG only from 4K–8K
- Public URL: ≤ 20 MB each; Base64 original file ≤ 20 MB and data URI ≤ 20 MB
- Per request: ≤ 256 images by URL, ≤ 250 by Base64
- Token cost ≈ `h × w / (32 × 32) + 2` per image; optional `extra_body={"vl_high_resolution_images": True}` (up to 16M pixels / 16384 visual tokens)

**Video** (`plus` / `flash` / 235B)

- Duration 2 s – 1 h; public URL ≤ 2 GB; ≤ 64 videos / request
- OpenAI-compatible type is `video_url` (not `image_url`)
- `fps` on the video object: `[0.1, 10]`, default `2.0` (one frame every `1/fps` seconds)
- Frame-list alternative: min 4 / max 2000 images for plus/flash/235B

**Timeouts**

- Server non-stream cap ~300 s
- Use `client.with_options(timeout=1800)` and/or `stream=True` for large media

## Video example

```python
r = client.chat.completions.create(
    model="qwen3-vl-plus",
    messages=[{
        "role": "user",
        "content": [
            {
                "type": "video_url",
                "video_url": {"url": "https://example.com/clip.mp4"},
                "fps": 2,
            },
            {"type": "text", "text": "Summarize the actions with HH:mm:ss timestamps as JSON."},
        ],
    }],
)
```

## curl

```bash
curl -sS https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions \
  -H "Authorization: Bearer $DASHSCOPE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3-vl-plus",
    "messages": [{
      "role": "user",
      "content": [
        {"type": "image_url", "image_url": {"url": "https://dashscope.oss-cn-beijing.aliyuncs.com/images/dog_and_girl.jpeg"}},
        {"type": "text", "text": "What is in this image?"}
      ]
    }]
  }'
```

## Official docs

- Vision guide: https://docs.qwencloud.com/developer-guides/multimodal/vision
- Model table: https://docs.qwencloud.com/developer-guides/getting-started/vision-models
- Key console: https://bailian.console.aliyun.com/ (intl Model Studio)
