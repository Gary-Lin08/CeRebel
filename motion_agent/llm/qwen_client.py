"""Backend-agnostic vision-language model interface + Qwen/DashScope client."""

from __future__ import annotations

import base64
import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LOG = logging.getLogger("motion_agent.qwen")


@dataclass
class VLMMessage:
    role: str
    content: Any


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class VLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: Any = None
    error: str | None = None
    reasoning: str | None = None  # model "thinking" / reasoning_content when present


def _extract_reasoning(message: Any) -> str | None:
    """Pull Qwen/OpenAI-compatible reasoning fields if the backend exposes them."""
    if message is None:
        return None
    for attr in ("reasoning_content", "reasoning", "thinking"):
        val = getattr(message, attr, None)
        if isinstance(val, str) and val.strip():
            return val
    extra = getattr(message, "model_extra", None) or {}
    if isinstance(extra, dict):
        for key in ("reasoning_content", "reasoning", "thinking"):
            val = extra.get(key)
            if isinstance(val, str) and val.strip():
                return val
    return None


class VisionLanguageModel(ABC):
    @abstractmethod
    def generate(self, messages: list[dict[str, Any]], **kwargs: Any) -> VLMResponse:
        raise NotImplementedError

    @abstractmethod
    def generate_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> VLMResponse:
        raise NotImplementedError

    def supports_video(self) -> bool:
        return False

    def supports_tool_calling(self) -> bool:
        return False


def _load_api_key() -> str:
    """Load DashScope/Qwen API key.

    Prefer ``~/Qwen_KEY.txt`` (or ``QWEN_KEY_FILE``) so key-file updates take
    effect without fighting a stale process environment. Fall back to env vars.
    """
    key_file = Path(os.environ.get("QWEN_KEY_FILE", str(Path.home() / "Qwen_KEY.txt")))
    if key_file.is_file():
        for line in key_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("Qwen_API_KEY="):
                return line.split("=", 1)[1].strip()
            if line.startswith("DASHSCOPE_API_KEY="):
                return line.split("=", 1)[1].strip()
            if line.startswith("sk-"):
                return line
    env = os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("QWEN_API_KEY")
    if env:
        return env.strip()
    raise RuntimeError(
        "No DashScope/Qwen API key found. Set DASHSCOPE_API_KEY or create ~/Qwen_KEY.txt"
    )


class QwenDashScopeVLM(VisionLanguageModel):
    """OpenAI-compatible DashScope intl client for Qwen3-VL."""

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_s: float = 180.0,
    ) -> None:
        self.model = model or os.environ.get("MOTION_AGENT_MODEL", "qwen3.7-plus")
        self.base_url = base_url or os.environ.get(
            "MOTION_AGENT_BASE_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        self.api_key = api_key or _load_api_key()
        self.timeout_s = timeout_s
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError(
                    "openai package required for DashScope backend. "
                    "pip install openai"
                ) from exc
            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout_s,
            )
        return self._client

    def supports_video(self) -> bool:
        return True

    def supports_tool_calling(self) -> bool:
        return True

    def generate(self, messages: list[dict[str, Any]], **kwargs: Any) -> VLMResponse:
        return self.generate_with_tools(messages, tools=[], **kwargs)

    def generate_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> VLMResponse:
        client = self._get_client()
        req: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": int(kwargs.get("max_tokens", 2048)),
        }
        if tools:
            req["tools"] = tools
            req["tool_choice"] = kwargs.get("tool_choice", "auto")
        # Ask DashScope for visible intermediate reasoning when the model supports it.
        # If the combo with tools fails, retry without thinking.
        enable_thinking = bool(kwargs.get("enable_thinking", True))
        extra_body = kwargs.get("extra_body")
        if extra_body is None and enable_thinking:
            extra_body = {"enable_thinking": True}

        def _call(body: dict[str, Any] | None) -> Any:
            payload = dict(req)
            if body:
                payload["extra_body"] = body
            return client.chat.completions.create(**payload)

        try:
            resp = _call(extra_body)
        except Exception as first_exc:  # noqa: BLE001
            if extra_body:
                LOG.warning(
                    "Qwen request with thinking failed (%s); retrying without",
                    first_exc,
                )
                try:
                    resp = _call(None)
                except Exception as exc:  # noqa: BLE001
                    LOG.exception("Qwen request failed")
                    return VLMResponse(content=None, error=str(exc), raw=None)
            else:
                LOG.exception("Qwen request failed")
                return VLMResponse(content=None, error=str(first_exc), raw=None)

        choice = resp.choices[0].message
        tool_calls: list[ToolCall] = []
        for tc in choice.tool_calls or []:
            args: dict[str, Any]
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_raw": tc.function.arguments}
            tool_calls.append(
                ToolCall(id=tc.id, name=tc.function.name, arguments=args)
            )
        return VLMResponse(
            content=choice.content,
            tool_calls=tool_calls,
            raw=resp,
            reasoning=_extract_reasoning(choice),
        )


class MockVLM(VisionLanguageModel):
    """Deterministic mock for offline tests — calls get_motion_state once then answers."""

    def supports_tool_calling(self) -> bool:
        return True

    def supports_video(self) -> bool:
        return False

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, messages: list[dict[str, Any]], **kwargs: Any) -> VLMResponse:
        return self.generate_with_tools(messages, tools=[], **kwargs)

    def generate_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> VLMResponse:
        self.calls += 1
        # If last message is a tool result, produce final answer
        if messages and messages[-1].get("role") == "tool":
            return VLMResponse(
                content=(
                    "Based on the motion tools, the subject posture around the "
                    "requested time is described by the returned joint angles and "
                    "contact state (mock VLM)."
                )
            )
        return VLMResponse(
            content=None,
            tool_calls=[
                ToolCall(
                    id="call_mock_1",
                    name="get_motion_state",
                    arguments={"timestamp": 3.2},
                )
            ],
            reasoning="I should query get_motion_state for a precise posture reading.",
        )


class LocalUnavailableVLM(VisionLanguageModel):
    """Placeholder local backend when weights/vLLM are not installed."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def supports_video(self) -> bool:
        return False

    def supports_tool_calling(self) -> bool:
        return False

    def generate(self, messages: list[dict[str, Any]], **kwargs: Any) -> VLMResponse:
        return VLMResponse(content=None, error=self.reason)

    def generate_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> VLMResponse:
        return VLMResponse(content=None, error=self.reason)


def _local_backend() -> VisionLanguageModel:
    model_path = os.environ.get("MOTION_AGENT_LOCAL_MODEL") or os.environ.get("MODEL_NAME")
    if not model_path:
        return LocalUnavailableVLM(
            "Local Qwen3-VL weights are not configured (set MOTION_AGENT_LOCAL_MODEL). "
            "Default demo backend is DashScope. GPU is free but no VL weights are on disk."
        )
    path = Path(model_path)
    if not path.exists():
        return LocalUnavailableVLM(f"Local model path does not exist: {model_path}")
    return LocalUnavailableVLM(
        f"Local serving stack is not wired for {model_path} yet. Use MOTION_AGENT_BACKEND=dashscope."
    )


def build_vlm(backend: str | None = None) -> VisionLanguageModel:
    backend = (backend or os.environ.get("MOTION_AGENT_BACKEND", "dashscope")).lower()
    if backend in ("mock", "test"):
        return MockVLM()
    if backend in ("dashscope", "openai_compatible", "qwen"):
        return QwenDashScopeVLM()
    if backend in ("local", "vllm", "sglang", "transformers"):
        return _local_backend()
    raise ValueError(f"Unknown INFERENCE_BACKEND/MOTION_AGENT_BACKEND: {backend}")


def file_to_data_uri(path: Path, mime: str) -> str:
    raw = path.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def jpeg_b64_to_data_uri(b64: str) -> str:
    return f"data:image/jpeg;base64,{b64}"
