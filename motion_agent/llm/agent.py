"""Multi-step tool-calling agent loop."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from motion_agent.knowledge import source_matches
from motion_agent.knowledge.sport_sources import (
    SPORT_KB_SOURCES,
    default_kb_sources as _default_kb_sources_map,
)
from motion_agent.llm.prompts import (
    compose_system_prompt,
    get_preset_prompt,
    user_context_preamble,
)
from motion_agent.llm.qwen_client import VisionLanguageModel, build_vlm, file_to_data_uri
from motion_agent.llm.schemas import RGB_TOOL_NAMES, openai_tools
from motion_agent.motion.adapter import adapt_session
from motion_agent.motion.sequence import MotionSequence
from motion_agent.storage.errors import tool_error
from motion_agent.storage.motion_store import newest_ready_session_dir, session_summary
from motion_agent.tools.motion_tools import execute_tool

try:
    from cerebel_config import fake_egocentric_enabled
except ImportError:  # pragma: no cover

    def fake_egocentric_enabled() -> bool:
        return os.environ.get("CEREBEL_FAKE_EGOCENTRIC", "0").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

LOG = logging.getLogger("motion_agent.agent")

IMAGE_TOOL_NAMES = {
    "get_rgb_frame",
    "render_force_visualization",
    "get_joint_trajectory",
    "get_com_trajectory",
}

# First clip frame can also be shown in the UI.
CLIP_TOOL_NAMES = {"get_rgb_clip"}


@dataclass
class TraceStep:
    kind: str  # thinking | assistant | tool | final
    name: str | None = None
    arguments: dict[str, Any] | None = None
    result_summary: Any = None
    content: str | None = None
    image_base64: str | None = None
    mime_type: str | None = None
    interim: bool = True


@dataclass
class AgentResult:
    answer: str
    trace: list[TraceStep] = field(default_factory=list)
    session_id: str = ""
    tool_calls_used: int = 0
    error: str | None = None


OnEvent = Callable[[dict[str, Any]], None]


def _summarize_tool_result(name: str, result: dict[str, Any]) -> dict[str, Any]:
    """Shrink bulky payloads (esp. base64 images) before logging / UI."""
    if not isinstance(result, dict):
        return {"raw": str(result)[:500]}
    if result.get("error"):
        return {"error": True, "type": result.get("type"), "message": result.get("message")}
    # Golf phase cards need the full swings/phases structure in the UI.
    if name == "build_sport_analysis" and result.get("ui_hint") == "sport_motion_board":
        # The board and the model both need the samples, not a length-only summary.
        return {
            k: result.get(k)
            for k in (
                "error",
                "ui_hint",
                "tool",
                "sport",
                "title",
                "badge",
                "stage_label",
                "trail_label",
                "handedness",
                "lead_side",
                "trail_side",
                "start_time",
                "end_time",
                "impact_time",
                "phases",
                "series",
                "landmarks",
                "findings",
                "highlights",
                "disclosure",
                "note_for_model",
            )
            if k in result
        }
    if name == "segment_golf_swings" and result.get("ui_hint") == "golf_phase_cards":
        keep = {
            k: result.get(k)
            for k in (
                "error",
                "ui_hint",
                "sport",
                "tool",
                "handedness",
                "lead_side",
                "trail_side",
                "start_time",
                "end_time",
                "swing_count",
                "swings",
                "phase_order",
                "note",
                "note_for_model",
            )
            if k in result
        }
        return keep
    summary = {k: v for k, v in result.items() if k not in ("image_base64", "frames", "series")}
    if "image_base64" in result:
        summary["image_base64_len"] = len(result["image_base64"])
        summary["mime_type"] = result.get("mime_type")
        summary["frame_index"] = result.get("frame_index")
        summary["timestamp_actual"] = result.get("timestamp_actual")
    if "frames" in result:
        summary["frame_count"] = result.get("frame_count")
        summary["frames_preview"] = [
            {
                "timestamp": f.get("timestamp"),
                "frame_index": f.get("frame_index"),
                "image_base64_len": len(f.get("image_base64") or ""),
            }
            for f in (result.get("frames") or [])[:4]
        ]
    if "series" in result:
        series_meta = {}
        for sk, sv in (result.get("series") or {}).items():
            if isinstance(sv, dict) and "values" in sv:
                series_meta[sk] = {
                    "n": len(sv["values"]),
                    "times_span": [sv["times"][0], sv["times"][-1]] if sv.get("times") else None,
                }
            else:
                series_meta[sk] = sv
        summary["series"] = series_meta
    # Cap nested size
    text = json.dumps(summary, default=str)
    if len(text) > 12000:
        summary = {"truncated": True, "preview": text[:12000]}
    return summary


def _tool_message_content(name: str, result: dict[str, Any]) -> str:
    """Content returned to the model (may include one small image data URI note)."""
    if name == "build_sport_analysis" and not result.get("error"):
        # Full samples: the summary path is what the UI draws, and it is also
        # the complete numeric record the model may cite.
        payload = _summarize_tool_result(name, result)
        return json.dumps(payload, default=str)
    payload = _summarize_tool_result(name, result)
    # For RGB tools, include a short note that image bytes were retrieved
    if name in ("get_rgb_frame", "render_force_visualization") and not result.get("error"):
        payload["note_for_model"] = (
            "An image was retrieved and attached on the multimodal channel. "
            "Do not treat the image as the source of truth for numerical joint angles or forces."
        )
    return json.dumps(payload, default=str)


def _build_user_content(
    question: str,
    seq: MotionSequence,
    attach_video: bool,
) -> Any:
    text = question
    if not attach_video or not seq.video_path or not seq.video_path.is_file():
        return text
    # Full video can be large; for demo attach poster-ish short approach:
    # Prefer attaching a few keyframes if file > ~8MB, else data URI video.
    size = seq.video_path.stat().st_size
    max_bytes = int(os.environ.get("MOTION_AGENT_MAX_VIDEO_BYTES", str(6 * 1024 * 1024)))
    if size <= max_bytes:
        try:
            uri = file_to_data_uri(seq.video_path, "video/mp4")
            return [
                {"type": "video_url", "video_url": {"url": uri}, "fps": 1.0},
                {"type": "text", "text": text},
            ]
        except Exception:  # noqa: BLE001
            LOG.warning("failed to attach video data URI", exc_info=True)
            return text
    # Fallback: attach three stills
    content: list[dict[str, Any]] = []
    for t in (0.5, seq.duration_s * 0.4, max(seq.duration_s - 0.5, 0.0)):
        fr = execute_tool(seq, "get_rgb_frame", {"timestamp": t})
        if fr.get("error"):
            continue
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{fr['image_base64']}",
                },
            }
        )
    content.append(
        {
            "type": "text",
            "text": text
            + "\n(Note: full video exceeded size limit; representative frames attached.)",
        }
    )
    return content


GOLF_FIRST_QUESTION = "Divide this video into golf swings and phases."

# Default RAG source filters when the model omits sources= (prefix / glob).
# Canonical map: motion_agent.knowledge.sport_sources.SPORT_KB_SOURCES
GOLF_KB_SOURCES = list(SPORT_KB_SOURCES["golf"])
SNOWBOARD_KB_SOURCES = list(SPORT_KB_SOURCES["snowboarding"])
BASEBALL_KB_SOURCES = list(SPORT_KB_SOURCES["baseball"])
HUMAN_KB_SOURCES = list(SPORT_KB_SOURCES["general"])

# Heuristic: questions that should draw from the golf knowledge pack, not phases alone.
_GOLF_KB_HINTS = (
    "rules of golf",
    "penalty area",
    "relief",
    "handicap",
    "whs",
    "world handicap",
    "usga",
    "r&a",
    "r and a",
    "equipment rule",
    "conforming",
    "knowledge pack",
    "etiquette",
    "local rule",
    "biomechanics knowledge",
    "taxonomy",
    "what does the rule",
    "rulebook",
    "definition of",
)


def _is_golf_mode(prompt_mode: str | None) -> bool:
    return (prompt_mode or "").strip().lower() == "golf"


def _is_baseball_mode(prompt_mode: str | None) -> bool:
    return (prompt_mode or "").strip().lower() == "baseball"


def _is_snowboard_mode(prompt_mode: str | None) -> bool:
    return (prompt_mode or "").strip().lower() == "snowboarding"


def _default_kb_sources(prompt_mode: str | None) -> list[str] | None:
    return _default_kb_sources_map(prompt_mode)


def _golf_question_needs_kb(question: str) -> bool:
    q = (question or "").strip().lower()
    if not q:
        return False
    return any(hint in q for hint in _GOLF_KB_HINTS)


def _with_kb_scope(
    prompt_mode: str | None,
    name: str,
    arguments: dict[str, Any] | None,
) -> dict[str, Any]:
    """Apply sport-default knowledge filters when the model omits sources=."""
    args = dict(arguments or {})
    if name != "search_knowledge_base":
        return args
    raw = args.get("sources")
    if isinstance(raw, list) and any(str(s).strip() for s in raw):
        return args
    defaults = _default_kb_sources(prompt_mode)
    if defaults:
        args["sources"] = defaults
    return args


def _scope_kb_tool_result(
    prompt_mode: str | None,
    name: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Filter list_knowledge_sources to the active sport corpus when possible."""
    if name != "list_knowledge_sources" or result.get("error"):
        return result
    defaults = _default_kb_sources(prompt_mode)
    if not defaults:
        return result
    sources = result.get("sources")
    if not isinstance(sources, list):
        return result
    filtered = [
        row
        for row in sources
        if isinstance(row, dict)
        and source_matches(str(row.get("source") or ""), defaults)
    ]
    out = dict(result)
    out["sources"] = filtered
    out["file_count"] = len(filtered)
    out["chunk_count"] = sum(int(row.get("chunks") or 0) for row in filtered)
    out["note"] = (
        f"{result.get('note') or ''} "
        f"(filtered to active sport sources: {defaults})"
    ).strip()
    return out


def _bootstrap_golf_phases(
    seq: MotionSequence,
    messages: list[dict[str, Any]],
    push: Callable[[TraceStep], None],
    on_event: OnEvent | None,
) -> int:
    """Pre-run swing/phase segmentation in golf mode for UI phase cards.

    Injects a synthetic tool call + result so the UI gets phase cards and the
    model can ground motion answers in the timeline. Does not block RAG.
    """
    args: dict[str, Any] = {"max_swings": 5, "handedness": "right"}
    _emit(
        on_event,
        {
            "type": "status",
            "message": f"Running tool segment_golf_swings… ({GOLF_FIRST_QUESTION})",
            "name": "segment_golf_swings",
            "arguments": args,
            "interim": True,
        },
    )
    result = execute_tool(seq, "segment_golf_swings", args)
    if not result.get("error"):
        result = dict(result)
        result["note_for_model"] = (
            "Swing/phase windows are available for motion grounding. "
            "Only re-call segment_golf_swings with swing_count if you must "
            "correct the count. For Rules / WHS / equipment / official wording, "
            "call search_knowledge_base — do not answer those from windows alone."
        )
    summary = _summarize_tool_result("segment_golf_swings", result)
    push(
        TraceStep(
            kind="tool",
            name="segment_golf_swings",
            arguments=args,
            result_summary=summary,
            interim=True,
        )
    )
    tool_call_id = "golf_bootstrap_segment_1"
    messages.append(
        {
            "role": "assistant",
            "content": (
                "Dividing this video into golf swings and phases "
                "(pre-run for motion grounding / phase cards)."
            ),
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": "segment_golf_swings",
                        "arguments": json.dumps(args),
                    },
                }
            ],
        }
    )
    messages.append(
        {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": _tool_message_content("segment_golf_swings", result),
        }
    )
    return 1


def _bootstrap_sport_analysis(
    seq: MotionSequence,
    messages: list[dict[str, Any]],
    push: Callable[[TraceStep], None],
    on_event: OnEvent | None,
    *,
    sport: str,
) -> int:
    """Inject the labeled figure pack so the UI and the model share one record."""
    args: dict[str, Any] = {"sport": sport, "handedness": "right"}
    _emit(
        on_event,
        {
            "type": "status",
            "message": f"Running tool build_sport_analysis… ({sport})",
            "name": "build_sport_analysis",
            "arguments": args,
            "interim": True,
        },
    )
    result = execute_tool(seq, "build_sport_analysis", args)
    summary = _summarize_tool_result("build_sport_analysis", result)
    push(
        TraceStep(
            kind="tool",
            name="build_sport_analysis",
            arguments=args,
            result_summary=summary,
            interim=True,
        )
    )
    tool_call_id = f"{sport}_bootstrap_analysis_1"
    messages.append(
        {
            "role": "assistant",
            "content": (
                f"Measuring {sport} phases and labeled traces for the motion board."
            ),
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": "build_sport_analysis",
                        "arguments": json.dumps(args),
                    },
                }
            ],
        }
    )
    messages.append(
        {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": _tool_message_content("build_sport_analysis", result),
        }
    )
    return 1


def _sport_analysis_nudge(sport: str) -> dict[str, Any]:
    title = "pitch" if sport == "baseball" else "swing"
    return {
        "role": "user",
        "content": (
            f"A {sport} motion board was precomputed above (ui_hint sport_motion_board). "
            f"It is the complete numeric record for this {title}: phases, labeled "
            "series samples, and landmarks. Ground every angle, distance, speed, and "
            "time in that payload. Do not estimate those quantities from the video. "
            "The chat draws the figures from that payload; your cerebel-analysis fence "
            "supplies the finding titles, captions, and coaching labels."
        ),
    }


def _bootstrap_golf_kb(
    seq: MotionSequence,
    question: str,
    messages: list[dict[str, Any]],
    push: Callable[[TraceStep], None],
    on_event: OnEvent | None,
) -> int:
    """Pre-run golf corpus RAG when the question clearly needs the knowledge pack."""
    args: dict[str, Any] = {
        "query": (question or "").strip(),
        "sources": list(GOLF_KB_SOURCES),
        "top_k": 4,
    }
    _emit(
        on_event,
        {
            "type": "status",
            "message": "Running tool search_knowledge_base… (golf KB bootstrap)",
            "name": "search_knowledge_base",
            "arguments": args,
            "interim": True,
        },
    )
    result = execute_tool(seq, "search_knowledge_base", args)
    if not result.get("error"):
        result = dict(result)
        result["note_for_model"] = (
            "Golf knowledge-pack hits for this question are above. Ground your "
            "answer in cited hit text; call search_knowledge_base again with a "
            "tighter query/sources= if you need more specific passages."
        )
    summary = _summarize_tool_result("search_knowledge_base", result)
    push(
        TraceStep(
            kind="tool",
            name="search_knowledge_base",
            arguments=args,
            result_summary=summary,
            interim=True,
        )
    )
    tool_call_id = "golf_bootstrap_kb_1"
    messages.append(
        {
            "role": "assistant",
            "content": (
                "Searching the golf knowledge pack for Rules / definitions / "
                "official wording relevant to the user question."
            ),
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": "search_knowledge_base",
                        "arguments": json.dumps(args),
                    },
                }
            ],
        }
    )
    messages.append(
        {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": _tool_message_content("search_knowledge_base", result),
        }
    )
    return 1


def _golf_post_bootstrap_nudge(*, kb_bootstrapped: bool) -> dict[str, Any]:
    if kb_bootstrapped:
        content = (
            "Swing/phase windows (if present above) are for motion grounding only. "
            "Knowledge-pack hits were retrieved above — answer using those cited "
            "passages for Rules / WHS / equipment / official wording. Call "
            "search_knowledge_base again if you need a tighter query. Use motion "
            "tools only when the question also needs video evidence."
        )
    else:
        content = (
            "Swing/phase windows are available above for motion grounding. "
            "Answer the user question with the right evidence: motion tools + "
            "phase windows for technique/timing; search_knowledge_base for Rules / "
            "WHS / equipment / technique vocabulary / biomechanics claims. Do not "
            "answer knowledge-pack questions from phase windows alone. Only re-call "
            "segment_golf_swings with swing_count if you must correct the swing count."
        )
    return {"role": "user", "content": content}


def _resolve_max_tool_calls(explicit: int | None) -> int | None:
    """Return a positive tool budget, or None for unlimited.

    Default is unlimited. Set MOTION_AGENT_MAX_TOOL_CALLS to a positive integer
    only if you want an explicit cap.
    """
    if explicit is not None:
        return explicit if explicit > 0 else None
    raw = os.environ.get("MOTION_AGENT_MAX_TOOL_CALLS", "").strip().lower()
    if raw in ("", "0", "none", "unlimited", "inf"):
        return None
    try:
        n = int(raw)
    except ValueError:
        return None
    return n if n > 0 else None


def _step_event(step: TraceStep) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": step.kind,
        "kind": step.kind,
        "interim": step.interim,
        "name": step.name,
        "arguments": step.arguments,
        "result_summary": step.result_summary,
        "content": step.content,
    }
    if step.image_base64:
        event["mime_type"] = step.mime_type or "image/png"
        event["image_base64"] = step.image_base64
    return event


def _emit(on_event: OnEvent | None, event: dict[str, Any]) -> None:
    if on_event is None:
        return
    try:
        on_event(event)
    except Exception:  # noqa: BLE001
        LOG.exception("on_event callback failed")


class MotionAgent:
    def __init__(
        self,
        seq: MotionSequence,
        vlm: VisionLanguageModel | None = None,
        max_tool_calls: int | None = None,
        attach_video: bool = False,
        system_prompt: str | None = None,
        prompt_mode: str | None = None,
    ) -> None:
        self.seq = seq
        self.vlm = vlm or build_vlm()
        # None => unlimited tool rounds / calls until the model returns a final answer.
        self.max_tool_calls = _resolve_max_tool_calls(max_tool_calls)
        self.attach_video = attach_video
        self.prompt_mode = prompt_mode
        base = system_prompt if system_prompt is not None else get_preset_prompt(prompt_mode)
        self.system_prompt = compose_system_prompt(base, attach_video=attach_video)

    def answer(
        self,
        question: str,
        on_event: OnEvent | None = None,
    ) -> AgentResult:
        # Without the video attached, the model is not offered the RGB frame/clip tools.
        tools = (
            openai_tools(include_rgb=self.attach_video)
            if self.vlm.supports_tool_calling()
            else []
        )
        preamble = user_context_preamble(
            self.seq.video_id,
            self.seq.duration_s,
            self.seq.fps,
            self.seq.has_dynamics(),
            prompt_mode=self.prompt_mode,
        )
        user_block = preamble + "\nUser question:\n" + question
        if _is_baseball_mode(self.prompt_mode):
            user_block += (
                "\n\n[Baseball policy] Pitch phases and labeled traces are pre-run "
                "as build_sport_analysis. Cite only those samples and landmarks. "
                "End a technique answer with the cerebel-analysis JSON fence."
            )
        if _is_golf_mode(self.prompt_mode):
            if _golf_question_needs_kb(question):
                user_block += (
                    "\n\n[Golf policy] This question needs the golf knowledge pack. "
                    "Use search_knowledge_base (hits may already be pre-injected). "
                    "Do not answer from swing/phase windows alone."
                )
            else:
                user_block += (
                    "\n\n[Golf policy] Swing/phase windows are usually pre-run for "
                    "motion grounding. For Rules / WHS / equipment / technique "
                    "vocabulary / biomechanics claims, also call search_knowledge_base."
                )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": _build_user_content(
                    user_block,
                    self.seq,
                    attach_video=self.attach_video and self.vlm.supports_video(),
                ),
            },
        ]
        trace: list[TraceStep] = []
        used = 0

        def push(step: TraceStep) -> None:
            trace.append(step)
            _emit(on_event, _step_event(step))

        _emit(
            on_event,
            {
                "type": "status",
                "message": "Calling model…",
                "session_id": self.seq.video_id,
                "interim": True,
            },
        )

        if _is_golf_mode(self.prompt_mode):
            used += _bootstrap_golf_phases(self.seq, messages, push, on_event)
            kb_bootstrapped = False
            if _golf_question_needs_kb(question):
                used += _bootstrap_golf_kb(
                    self.seq, question, messages, push, on_event
                )
                kb_bootstrapped = True
            else:
                used += _bootstrap_sport_analysis(
                    self.seq, messages, push, on_event, sport="golf"
                )
                messages.append(_sport_analysis_nudge("golf"))
            messages.append(_golf_post_bootstrap_nudge(kb_bootstrapped=kb_bootstrapped))
        elif _is_baseball_mode(self.prompt_mode):
            used += _bootstrap_sport_analysis(
                self.seq, messages, push, on_event, sport="baseball"
            )
            messages.append(_sport_analysis_nudge("baseball"))

        while True:
            _emit(
                on_event,
                {
                    "type": "status",
                    "message": "Waiting on model response…",
                    "tool_calls_used": used,
                    "interim": True,
                },
            )
            resp = self.vlm.generate_with_tools(
                messages,
                tools=tools,
                enable_thinking=True,
                max_tokens=4096,
            )
            if resp.error:
                _emit(
                    on_event,
                    {
                        "type": "error",
                        "message": resp.error,
                        "interim": False,
                    },
                )
                return AgentResult(
                    answer=f"Model unavailable: {resp.error}",
                    trace=trace,
                    session_id=self.seq.video_id,
                    tool_calls_used=used,
                    error=resp.error,
                )

            if resp.reasoning:
                push(
                    TraceStep(
                        kind="thinking",
                        content=resp.reasoning,
                        interim=True,
                    )
                )

            if resp.tool_calls:
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": resp.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for tc in resp.tool_calls
                    ],
                }
                messages.append(assistant_msg)
                if resp.content:
                    push(
                        TraceStep(
                            kind="assistant",
                            content=resp.content,
                            interim=True,
                        )
                    )

                for tc in resp.tool_calls:
                    if self.max_tool_calls is not None and used >= self.max_tool_calls:
                        msg = (
                            "Stopped after reaching the configured tool-call budget "
                            f"({self.max_tool_calls}) without a final answer."
                        )
                        _emit(
                            on_event,
                            {"type": "error", "message": msg, "interim": False},
                        )
                        return AgentResult(
                            answer=msg,
                            trace=trace,
                            session_id=self.seq.video_id,
                            tool_calls_used=used,
                            error="MAX_TOOL_CALLS",
                        )
                    _emit(
                        on_event,
                        {
                            "type": "status",
                            "message": f"Running tool {tc.name}…",
                            "name": tc.name,
                            "arguments": tc.arguments,
                            "interim": True,
                        },
                    )
                    if tc.name in RGB_TOOL_NAMES and not self.attach_video:
                        # The user's video is not shared with the model: never read its
                        # frames, even when the model calls a tool it was not offered.
                        result = tool_error(
                            "TOOL_UNAVAILABLE",
                            f"{tc.name} is not available: the video is not shared with "
                            "you. Answer from the motion tools.",
                        )
                    else:
                        result = execute_tool(
                            self.seq,
                            tc.name,
                            _with_kb_scope(self.prompt_mode, tc.name, tc.arguments),
                        )
                        result = _scope_kb_tool_result(self.prompt_mode, tc.name, result)
                    used += 1
                    summary = _summarize_tool_result(tc.name, result)
                    image_b64 = None
                    mime = None
                    if not result.get("error"):
                        if result.get("image_base64") and tc.name in IMAGE_TOOL_NAMES:
                            image_b64 = result["image_base64"]
                            mime = result.get("mime_type") or "image/png"
                        elif tc.name in CLIP_TOOL_NAMES and result.get("frames"):
                            first = result["frames"][0]
                            image_b64 = first.get("image_base64")
                            mime = first.get("mime_type") or "image/jpeg"
                    push(
                        TraceStep(
                            kind="tool",
                            name=tc.name,
                            arguments=tc.arguments,
                            result_summary=summary,
                            image_base64=image_b64,
                            mime_type=mime,
                            interim=True,
                        )
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": _tool_message_content(tc.name, result),
                        }
                    )
                    if image_b64:
                        messages.append(
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "image_url",
                                        "image_url": {
                                            "url": f"data:{mime};base64,{image_b64}"
                                        },
                                    },
                                    {
                                        "type": "text",
                                        "text": (
                                            f"[tool image] {tc.name} "
                                            f"args={json.dumps(tc.arguments, default=str)}"
                                        ),
                                    },
                                ],
                            }
                        )
                continue

            answer = (resp.content or "").strip() or "(empty model response)"
            push(TraceStep(kind="final", content=answer, interim=False))
            return AgentResult(
                answer=answer,
                trace=trace,
                session_id=self.seq.video_id,
                tool_calls_used=used,
            )


def answer_question(
    question: str,
    video_id: str | None = None,
    data_dir: Path | None = None,
    vlm: VisionLanguageModel | None = None,
    attach_video: bool = False,
    system_prompt: str | None = None,
    prompt_mode: str | None = None,
    on_event: OnEvent | None = None,
) -> AgentResult:
    data_dir = data_dir or Path(os.environ.get("CEREBEL_DATA_DIR", "/root/CerebelServer/data"))
    if video_id:
        session_dir = data_dir / "sessions" / video_id
    else:
        session_dir = newest_ready_session_dir(data_dir)
    if session_dir is None or not session_dir.is_dir():
        err = AgentResult(
            answer="No preprocessed video session found.",
            error="NO_SESSION",
        )
        _emit(on_event, {"type": "error", "message": err.answer, "interim": False})
        _emit(
            on_event,
            {
                "type": "done",
                "answer": err.answer,
                "session_id": "",
                "tool_calls_used": 0,
                "error": err.error,
                "interim": False,
            },
        )
        return err
    seq = adapt_session(session_dir)
    agent = MotionAgent(
        seq,
        vlm=vlm,
        attach_video=attach_video,
        system_prompt=system_prompt,
        prompt_mode=prompt_mode,
    )
    return agent.answer(question, on_event=on_event)


def get_sport_analysis(
    sport: str,
    video_id: str | None = None,
    data_dir: Path | None = None,
    *,
    handedness: str = "right",
) -> dict[str, Any]:
    """Run build_sport_analysis without the LLM — for the motion board."""
    data_dir = data_dir or Path(os.environ.get("CEREBEL_DATA_DIR", "/root/CerebelServer/data"))
    if video_id:
        session_dir = data_dir / "sessions" / video_id
    else:
        session_dir = newest_ready_session_dir(data_dir)
    if session_dir is None or not session_dir.is_dir():
        return {"error": True, "type": "NO_SESSION", "message": "No preprocessed video session found."}
    key = (sport or "golf").strip().lower()
    # Refuse to dress a golf swing as a pitch (consumer trust).
    if key == "baseball":
        try:
            from motion_agent.app.product_session import session_looks_like_golf

            is_golf, conf, _ = session_looks_like_golf(
                session_dir, video_id=session_dir.name, data_dir=data_dir
            )
            if is_golf:
                return {
                    "error": True,
                    "type": "SPORT_MISMATCH",
                    "message": (
                        "This capture looks like a golf swing "
                        f"(confidence {conf:.2f}), not a baseball pitch."
                    ),
                    "detected_sport": "golf",
                    "sport_confidence": round(conf, 3),
                }
        except Exception:  # noqa: BLE001
            LOG.exception("sport mismatch check failed")
    try:
        seq = adapt_session(session_dir)
        result = execute_tool(
            seq,
            "build_sport_analysis",
            {"sport": key, "handedness": handedness},
        )
    except Exception as exc:  # noqa: BLE001
        LOG.exception("sport analysis prefetch failed")
        return {"error": True, "type": "ANALYSIS_FAILED", "message": str(exc)}
    if result.get("error"):
        return result
    return _summarize_tool_result("build_sport_analysis", result)


def get_golf_phase_cards(
    video_id: str | None = None,
    data_dir: Path | None = None,
    *,
    max_swings: int = 5,
) -> dict[str, Any]:
    """Run segment_golf_swings without the LLM — for immediate Golf UI cards."""
    data_dir = data_dir or Path(os.environ.get("CEREBEL_DATA_DIR", "/root/CerebelServer/data"))
    if video_id:
        session_dir = data_dir / "sessions" / video_id
    else:
        session_dir = newest_ready_session_dir(data_dir)
    if session_dir is None or not session_dir.is_dir():
        return {"error": True, "type": "NO_SESSION", "message": "No preprocessed video session found."}
    try:
        seq = adapt_session(session_dir)
        result = execute_tool(
            seq,
            "segment_golf_swings",
            {"max_swings": int(max_swings), "handedness": "right"},
        )
    except Exception as exc:  # noqa: BLE001
        LOG.exception("golf phase prefetch failed")
        return {"error": True, "type": "SEGMENT_FAILED", "message": str(exc)}
    if result.get("error"):
        return {
            "error": True,
            "type": result.get("type") or "SEGMENT_FAILED",
            "message": result.get("message") or "segment_golf_swings failed",
            "session_id": session_dir.name,
        }
    summary = _summarize_tool_result("segment_golf_swings", result)
    if isinstance(summary, dict):
        summary["session_id"] = session_dir.name
        summary["ok"] = True
    return summary


def get_current_session_info(
    data_dir: Path | None = None,
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    data_dir = data_dir or Path(os.environ.get("CEREBEL_DATA_DIR", "/root/CerebelServer/data"))
    if session_id:
        session_dir = data_dir / "sessions" / str(session_id)
        if not session_dir.is_dir():
            return {"error": True, "type": "NO_SESSION", "message": "Capture not found", "session_id": session_id}
    else:
        session_dir = newest_ready_session_dir(data_dir)
    if session_dir is None:
        return {"error": True, "type": "NO_SESSION", "message": "No ready session"}
    summary = session_summary(session_dir)
    try:
        seq = adapt_session(session_dir)
        fake_ego = fake_egocentric_enabled()
        summary.update(
            {
                "duration_s": seq.duration_s,
                "frame_count": seq.frame_count,
                "fps": seq.fps,
                "has_dynamics": seq.has_dynamics(),
                "fake_egocentric": fake_ego,
                "hide_source_video": fake_ego,
            }
        )
        if fake_ego:
            summary["soma_mesh_url_path"] = f"/media/{seq.video_id}/soma-mesh.bin"
            # Keep video path out of the client so the raw capture stays hidden.
            summary["video_url_path"] = None
        elif seq.video_path and seq.video_path.is_file():
            summary["video_url_path"] = f"/media/{seq.video_id}/session.mp4"
        else:
            # The source video can be deleted on the retention schedule; the analysis stays.
            summary["video_url_path"] = None
            summary["source_video_removed"] = (session_dir / "retention.json").is_file()
    except Exception as exc:  # noqa: BLE001
        summary["load_error"] = str(exc)
    return summary
