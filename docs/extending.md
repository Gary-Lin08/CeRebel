# 扩展指南

三种最常见的扩展：加一个运动、给 AI 教练加一个工具、扩充知识库。原则只有一条：**给用户看的每个数字都要来自确定性代码**，语言模型只负责组织语言。

## 加一个新运动（Sport Pack）

### 运动包是什么

一个运动包回答三个问题：这段视频是不是这项运动（`detect`）、在产品里叫什么、AI 教练默认回答哪些问题（`build_briefs`）。接口定义在 `motion_agent/sports/schema.py`：

```python
class SportPack(Protocol):
    id: str                 # "golf"，也是 prompt_mode 和知识库目录名
    label: str              # "Golf"
    status: str             # "live" | "beta" | "coming_soon"
    blurb: str
    prompt_mode: str
    example_questions: list[str]
    product_title: str
    product_subtitle: str

    def detect(self, session_dir, *, video_id=None, data_dir=None) -> DetectResult: ...
    def build_briefs(self, *, video_id=None, data_dir=None) -> list[dict]: ...
```

现状：高尔夫（`packs/golf.py`）和通用人体（`packs/human.py`，兜底）是 live；棒球（`packs/baseball.py`）写好了但没开放；网球、篮球、跑步等 10 项在 `packs/coming_soon.py` 里只有目录项和知识库，检测器关闭。

### 识别流程

`finalize_capture` → `sports/registry.py` 的 `resolve_pack_session` → `detect_sport`：对 `LIVE_PACKS` 里每个运动包跑 `detect`，取置信度最高且 `matched` 的；都不匹配就用通用人体包，这时作品库状态记为 `no_swing`，不扣次数。高尔夫的检测器复用了挥杆分段（`segment_golf_swings`），置信度阈值 `CONFIDENCE_MIN = 0.45`。

### 步骤（以网球为例）

1. **知识库先行**：`motion_agent/knowledge/corpus/tennis/` 已经有一套。按 `corpus/_templates/SPORT_PACK_KB_SPEC.txt` 补全阶段模型、常见错误卡片（`fault_<snake_case>`）、提示语和练习，用 `python motion_agent/knowledge/corpus/_templates/check_pack_completeness.py` 检查。
2. **阶段和曲线**：在 `motion_agent/motion/` 写分段逻辑（参考 `golf_phases.py`），在 `sport_analysis.py` 的 `_compose_report` 里加 `if sport == "tennis": return _tennis_report(...)`，产出 `series`、`phases`、`findings`。finding 的字段见[数据格式](data-formats.md#报告里的-finding)，`findings` 要按 `severity` 排序。
3. **运动包类**：新建 `motion_agent/sports/packs/tennis.py`，实现 `detect`（只在有把握时 `matched=True`，宁可漏判也别误判）和 `build_briefs`。
4. **提示词**：在 `motion_agent/llm/prompts.py` 的 `PROMPT_PRESETS` 加 `"tennis"` 预设；`knowledge/sport_sources.py` 已经有 `"tennis": ["tennis/"]`。
5. **注册**：从 `coming_soon.py` 的列表里移除网球，把 `TennisPack()` 加进 `registry.py` 的 `LIVE_PACKS`。上线前可以先设 `status = "beta"` 放在 `STAGED_PACKS`。
6. **测试**：参考 `motion_agent/tests/test_sport_packs.py`、`test_sport_analysis.py`、`test_golf_phases.py`。至少覆盖：检测器对本运动的样例给出 matched、对高尔夫样例不 matched、报告里每条 finding 的数字能从曲线复算出来。
7. **前端**：消费者界面目前按高尔夫写（阶段名、文案），新运动要在 `motion_agent/app/ui/index.html` 里对应调整。`/api/sports` 的目录会自动更新。

### 检测器的坑

- 当前的高尔夫检测器会把仓库自带的篮球上篮样例以 0.71 的置信度认成挥杆。新加运动时，要用其他运动的样例做反例测试。
- 识别失败（`no_swing`）不扣次数，所以误判成"不是"的代价比误判成"是"小。

## 给 AI 教练加一个工具

工具就是一个普通的 Python 函数：第一个参数是 `MotionSequence`（一段视频的全部运动数据），其余是模型可以传的参数，返回一个可以 JSON 序列化的 dict。

1. **写函数**，放在 `motion_agent/motion/`、`dynamics/` 或 `tools/` 合适的模块里：

    ```python
    def get_contact_state(seq: MotionSequence, start_time: float | None = None, end_time: float | None = None) -> dict[str, Any]:
        ...
        return {"intervals": [...], "units": {...}, "estimated": True}
    ```

    - 出错时返回 `motion_agent.storage.errors.tool_error("CODE", "message")`，不要抛异常（`_wrap` 也会兜底，但自己返回的错误更清楚）。
    - 返回值里写清单位、坐标系，估算出来的值标明 `estimated`。
    - 结果要小：模型的上下文是按 token 算钱的，长序列先降采样。
    - 会返回用户视频画面的工具，名字要加进 `llm/schemas.py` 的 `RGB_TOOL_NAMES`：不共享视频时（线上默认）它们不会提供给模型。用测量数据画的图（曲线、力的示意图）不算画面。

2. **注册**：在 `motion_agent/tools/motion_tools.py` 的 `build_tool_map` 里加一行 `"my_tool": _wrap(seq, my_tool)`。
3. **写 schema**：在 `motion_agent/llm/schemas.py` 的列表里加一项（OpenAI function calling 格式）。`description` 是写给模型看的：什么时候该用、什么时候不该用、数值的局限。
4. **测试**：参考 `motion_agent/tests/test_motion_tools.py`、`test_dynamics_tools.py`。另外在 `test_agent.py` 里用 mock 后端确认模型能调用它。

现有工具：

| 工具 | 作用 |
|---|---|
| `get_motion_state` | 某一时刻的姿态、质心、着地 |
| `get_motion_events` | 关节极值、着地、起跳等事件 |
| `segment_golf_swings` | 高尔夫：几次挥杆、各阶段时间 |
| `build_sport_analysis` | 高尔夫 / 棒球：曲线、关键点、findings |
| `get_joint_trajectory` | 关节角度和角速度统计、降采样的时间序列 |
| `get_com_trajectory` | 质心位置、速度、加速度 |
| `get_contact_state` | 左右脚着地区间 |
| `get_dynamics_summary` / `get_joint_dynamics` / `get_ground_reaction_forces` / `get_joint_power` | 估算动力学 |
| `get_rgb_frame` / `get_rgb_clip` | 视频画面。`CEREBEL_LLM_ATTACH_VIDEO=0`（线上）时不提供给模型，强行调用返回 `TOOL_UNAVAILABLE` |
| `compare_motion` / `search_motion` | 同一视频里两段对比、启发式搜索 |
| `render_force_visualization` | 画一张带估算力箭头的骨架图 |
| `search_knowledge_base` / `list_knowledge_sources` | 本地知识库检索 |

## 扩充知识库

- 文本在 `motion_agent/knowledge/corpus/<运动>/` 下，全是 `.txt`。每个运动的目录结构和"错误卡片"格式见 `corpus/_templates/SPORT_PACK_KB_SPEC.txt`。`_meta/` 不参与检索。
- 来源要标权威等级（A+ 联合会规则 … C 社区来源，C 级不能用来生成产品结论）。不要整段粘贴有版权的手册，写摘要并注明出处。
- 每个提示词模式默认只检索自己的目录（`knowledge/sport_sources.py`）。
- 改了文本要重建向量索引：

```bash
PYTHONPATH=. python motion_agent/scripts/rebuild_knowledge_index.py
```

  没有嵌入模型的密钥时加 `--hashing`，用离线的哈希嵌入做冒烟测试。
- 批量导入 PDF / Word：`motion_agent/scripts/ingest_knowledge_zip.py`。

## 给前端加功能

- 消费者界面和操作员界面都在 `motion_agent/app/ui/index.html`，按 `<meta name="cerebel-surface">` 区分。设计规范见 [`DESIGN.md`](../DESIGN.md)：一个页面只突出一条修正，红色（`--flag`）只用于这条修正。
- 新的前端埋点事件要先加进 `product_api.py` 的 `CLIENT_EVENTS`，否则会被服务端丢弃。
- 引用服务端绝对路径时写 `"/api/..."`、`"/static/..."` 这种带引号的形式，服务端在有路径前缀时会自动改写。

## 新的客户端（App、眼镜、第三方）

不要直接读 `data/` 目录，走 HTTP API。用 [SDK](../sdk/README.md)，或者从 [`sdk/openapi.yaml`](../sdk/openapi.yaml) 生成。改接口时同一个 PR 里同步改 `openapi.yaml`、两个 SDK 和它们的模拟服务器。
