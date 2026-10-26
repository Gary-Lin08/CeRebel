# 数据格式

所有数据都在 `CEREBEL_DATA_DIR` 下（线上 `/opt/cerebel/data` 或 Mac mini 上的对应目录，本地默认是仓库的 `data/`）。没有数据库。

```text
data/
├── latest_mesh.json             最近一次流水线的指针（旧查看器用）
├── sessions/<SESSION_ID>/       每段视频一个目录，ID 是大写 UUID
│   ├── upload/source_video.mp4  用户上传的原视频（裁剪后只剩挥杆片段）        30 天后删除
│   ├── jpegs/000000.jpg …       抽出的帧，GEM-X 的输入                          30 天后删除
│   ├── header.json              帧率、分辨率、来源
│   ├── upload_progress.json     进度（/api/progress 读的就是它）
│   ├── gemx.json                GEM-X 任务记录：输出目录、帧率、IK 是否成功
│   ├── ik.json                  IK 产物清单
│   ├── retention.json           原视频被按期删除后留下的标记
│   └── ik/
│       ├── opensim-motion.json  ★ OpenSim 逆运动学结果：每帧的骨骼位姿、关节角、肌肉路径
│       ├── estimated-dynamics.json  估算地面反力、关节力和力矩
│       ├── pose3d.json          3D 关键点（SOMA 解码后，世界坐标）
│       ├── motion-data.json     旧格式的占位文件，不要用
│       ├── session.mp4          由帧重新编码的视频，报告页播放它                30 天后删除
│       ├── poster.jpg           封面帧                                          30 天后删除
│       ├── model/               3D 查看器资源（rajagopal-bones.glb 等）
│       └── opensim-work/        IK 中间文件（.trc 标记点、.mot 关节角、缩放后的 .osim）
└── product/
    ├── accounts.json            账号（邮箱为键；密码是 PBKDF2-SHA256 12 万轮加盐哈希）
    ├── tokens.json              登录令牌和过期时间
    ├── library.json             作品库：每次上传一行（属于谁、状态、识别结果）
    ├── billing.json             订阅状态（来自 Stripe webhook）、创始会员名额
    ├── events.jsonl             埋点，一行一个事件
    ├── question_counts.json     当天的提问次数（匿名访客的 IP 只存 HMAC）
    ├── media_url.key            签名媒体链接的密钥（没设 CEREBEL_MEDIA_URL_SECRET 时自动生成）
    └── retention.jsonl          每次数据保留任务的记录
```

GEM-X 的原始输出在 GPU 那一侧：`CEREBEL_GEMX_OUTPUT_ROOT/<SESSION_ID>/hpe_results.pt`（默认 `/root/GEM-X/outputs/cerebel/`）。处理完会被精简，只留 `body_params_global`、`body_params_incam`、`K_fullimg`，头部形状系数清零。

`★` 标出的 `opensim-motion.json` 是"这段视频处理成功"的判断依据：几乎所有接口都先检查它在不在。

## 写文件的规矩

- `data/product/` 下的文件会被网页进程、数据保留任务、隐私回填脚本同时读写。读改写一律用 `motion_agent/app/file_lock.py` 的 `locked(path, thread_lock)`：它同时持有线程锁和 `fcntl` 文件锁。
- 写 JSON 先写 `.tmp` 再 `replace`，避免写到一半被读到。
- 新增一个 `data/product/` 文件时，同时在 `retention.py`、`privacy_backfill.py` 和账号删除（`product_api.remove_account`）里考虑它：用户删账号时，和他有关的行都要删。

## upload_progress.json

```json
{"session_id": "…", "stage": "gemx", "percent": 45, "message": "Rebuilding your body from the video…",
 "stage_started_at": 1793064990.1, "updated_at": 1793065012.4}
```

| `stage` | 含义 |
|---|---|
| `uploading` → `extracting` → `queued` → `gemx` → `ik` | 处理中 |
| `ready` | 流水线完成。**还要看 `sport` 字段**：有它才是运动识别也完成了 |
| `no_swing` | 重建成功但没识别到挥杆，不计次 |
| `error` | 失败，`failed_at` 说明在哪一步：`input`、`gemx`、`gpu_unavailable`、`ik`、`start`、`timeout` |
| `rejected` | 上传质检没通过 |

`/api/progress` 返回时会去掉内部的 `error` 原文，处理中的还会加上 `jobs_ahead`、`eta_minutes`、`queue_message`。

## library.json

```json
[{"user_id": "f72d62aaa75c51be", "session_id": "…", "filename": "IMG_0412.MOV",
  "status": "ready", "created_at": "2026-10-26T19:36:22+00:00", "deleted": false,
  "sport": "golf", "sport_label": "Golf", "sport_confidence": 0.82, "consumer_ready": true,
  "practice": [{"finding_id": "trail-knee", "at": "2026-10-27T08:00:00+00:00"}]}]
```

`practice` 是练习打卡记录，最多保留最近 100 条，跟着这一行一起删除。

`status` 为 `error`、`no_swing`、`deleted`、`rejected` 的行不计入每月额度。

## opensim-motion.json

OpenSim 4.6，Rajagopal-Lai-Uhlrich 2023 模型（加颈部），22 个刚体、80 条肌肉。

```json
{
  "metadata": {"modelName": "…", "subjectHeightCm": 185, "subjectMassKg": 93,
               "meanMarkerRmseM": 0.047, "warning": "Monocular 3D OpenSim IK preview — not calibrated optical motion capture"},
  "bodyNames": ["pelvis", "femur_r", "tibia_r", "patella_r", "talus_r", "calcn_r", "toes_r", "femur_l", "…", "torso",
                "humerus_r", "ulna_r", "radius_r", "hand_r", "humerus_l", "ulna_l", "radius_l", "hand_l"],
  "frames": [{
    "time": 0.0,
    "pelvis": [0.068, 0.899, 0.061],
    "centerOfMass": [0.05, 0.95, 0.06],
    "bodies": {"pelvis": {"p": [0.06761, 0.898527, 0.061293], "q": [-0.0337, -0.4291, -0.3094, 0.8479]}, "…": {}},
    "muscles": [{"name": "addbrev_r", "points": [[…], […]]}],
    "coordinates": {"pelvis_tilt": -0.6018, "hip_flexion_r": 1.4222, "knee_angle_r": 1.4452, "lumbar_rotation": -0.2819,
                    "arm_flex_r": 0.3513, "elbow_flex_r": 0.2589, "…": 0},
    "markerRmseM": 0.0176
  }]
}
```

| 字段 | 单位和约定 |
|---|---|
| `bodies.*.p` | 米，OpenSim 地面坐标系：X 向前、Y 向上、Z 向右 |
| `bodies.*.q` | 四元数，顺序是 **x, y, z, w**（和 three.js 一致） |
| `coordinates` | OpenSim 广义坐标。转动自由度是**弧度**，`pelvis_tx/ty/tz` 是米 |
| `markerRmseM` | 这一帧虚拟标记点的拟合误差 |

做空翻这类大角度动作时，`pelvis_tilt` 在 ±180° 附近会卡住，要用 `motion_agent/README.md` 里说的世界坐标欧拉角或身体内在坐标的累计转角。

## estimated-dynamics.json

```json
{
  "metadata": {"status": "ESTIMATED GRF", "subjectMassKg": 93, "bodyWeightN": 912.0, "confidenceCeiling": 0.61,
               "warning": "Estimated from monocular video without force plates … Not valid for clinical or performance decisions."},
  "jointIds": ["hip_r", "knee_r", "ankle_r", "hip_l", "knee_l", "ankle_l"],
  "frames": [{
    "time": 0.333,
    "contact": {"phase": "right support", "left": false, "right": true},
    "grf": {"total": {"force": [103.1, 900.2, -36.0], "magnitudeN": 906.8, "bodyWeight": 0.99},
            "left": {"active": false, "weight": 0.001, "force": […], "point": […]},
            "right": {"active": true, "weight": 0.999, "force": […], "point": […]}},
    "joints": {"knee_r": {"position": […], "force": […], "magnitudeN": 928.2, "bodyWeight": 1.02,
                          "reactionMoment": […], "momentMagnitudeNm": 52.6, "flexionMomentNm": -52.2,
                          "angleRad": 0.998, "angularVelocityRadS": -1.904, "confidence": 0.54}}
  }]
}
```

力的单位是牛，力矩是牛·米，`bodyWeight` 是体重倍数。展示时必须标"估算"，不能说成测量值。功率 = `flexionMomentNm × angularVelocityRadS`。

## pose3d.json

```json
{"metadata": {"fps": 30.0, "frameCount": 91, "coordinateSystem": "…metres", "warning": "Monocular 3D estimate…"},
 "landmarkNames": ["nose", "left_eye_inner", "…"],
 "frames": [{"time": 0.0, "world": [[x, y, z], …], "image": [[u, v, z], …], "visibility": [1.0, …]}]}
```

## 报告里的 finding

`/api/sport_analysis` 返回的一条 finding 就是一个"要改的地方"或"做得好的地方"：

| 字段 | 例子 | 用途 |
|---|---|---|
| `id` | `trail-knee` | 前端和练习打卡用的键 |
| `headline` | Your back knee locks too early | 一句话结论，HUD 和通知用它 |
| `metric.value` / `metric.caption` | `66° → 0°` / back-knee bend, address → top | 测出来的数字 |
| `why` | When the back knee locks, the hips stop turning… | 为什么要改 |
| `cue` | Keep a soft back knee to the top | 动作时的感觉提示 |
| `drill` | Feet-together backswings… | 练习方法 |
| `check` | Trail knee flex at the top stays near 66°… | 下次拍完怎么判断改好了 |
| `start_time` / `end_time` | 0.3 / 1.8 | 在视频上标出的时间段 |
| `focus` / `overlay` | 骨骼、视角、角度弧线 | 3D 特写和视频叠加 |
| `strength` | false | true 表示是优点 |
| `severity` | 70 | 排序依据；数组已按它从大到小排好 |
| `fault_id` | `fault_setup_locked_legs` | 对应知识库里的错误卡片 |

文案目前只有英文，由 `sport_analysis.py` 的规则生成。
